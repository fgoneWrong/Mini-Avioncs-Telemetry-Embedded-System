"""Run with: python -m unittest discover -s dashboard -p test_server.py -v"""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import BridgeState, DashboardServer, saved_device, validate_device_host, validate_telemetry


def sample():
    return {
        "device_id": "ESP32-S3 Avionics", "boot_id": "a1b2c3", "session_id": 2,
        "sequence": 17, "uptime_ms": 10000, "wifi_connected": True,
        "wifi_rssi_dbm": -54, "ip": "192.168.1.24", "xplane_connected": True,
        "data_fresh": True, "sim_paused": False, "age_ms": 30,
        "telemetry": {"airspeed_kt": 112.5, "altitude_ft": 3456.0,
                      "heading_deg": 127.0, "pitch_deg": 2.5, "roll_deg": -12.0,
                      "vertical_speed_fpm": 240.0, "sim_time_s": 730.0},
    }


class ValidationTests(unittest.TestCase):
    def test_destination_allowlist(self):
        for value in ("10.0.0.1", "172.16.0.1", "172.31.255.254", "192.168.1.24", "esp32-avionics.local"):
            self.assertEqual(validate_device_host(value), value)
        for value in ("127.0.0.1", "169.254.1.1", "8.8.8.8", "172.32.0.1", "localhost",
                      "https://192.168.1.24", "192.168.1.24:80", "x.local/path",
                      "user@x.local", "x.local?hi", "x.local#hi", "a.b.local", "::1", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_device_host(value)

    def test_invalid_reading_rejected(self):
        for invalid in (float("nan"), float("inf"), True, "112", {}):
            value = sample()
            value["telemetry"]["airspeed_kt"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_telemetry(value)

    def test_null_startup_is_never_fresh(self):
        value = sample()
        value["age_ms"] = None
        value["telemetry"] = {key: None for key in value["telemetry"]}
        self.assertFalse(validate_telemetry(value)["data_fresh"])
        value["age_ms"] = 0
        value["xplane_connected"] = False
        self.assertFalse(validate_telemetry(value)["data_fresh"])


class StateTests(unittest.TestCase):
    def test_stalled_upstream_does_not_leave_fresh_snapshot(self):
        state = BridgeState(fetcher=lambda host: sample())
        with patch("server.time.monotonic", return_value=100.0):
            state.poll_once()
            self.assertTrue(state.snapshot()["data_fresh"])
        with patch("server.time.monotonic", return_value=104.0):
            value = state.snapshot()
        self.assertTrue(value["device_online"])
        self.assertFalse(value["data_fresh"])
        self.assertEqual(value["age_ms"], 4030)

    def test_startup_has_no_invented_values(self):
        state = BridgeState()
        value = state.snapshot()
        self.assertFalse(value["device_online"])
        self.assertFalse(value["data_fresh"])
        self.assertTrue(all(reading is None for reading in value["telemetry"].values()))

    def test_failure_retains_readings_but_marks_stale_then_recovers(self):
        results = iter((sample(), OSError("offline"), sample()))
        def fetch(host):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result
        state = BridgeState(fetcher=fetch)
        state.poll_once()
        self.assertTrue(state.snapshot()["device_online"])
        state.poll_once()
        value = state.snapshot()
        self.assertFalse(value["device_online"])
        self.assertFalse(value["data_fresh"])
        self.assertEqual(value["telemetry"]["altitude_ft"], 3456.0)
        self.assertIsNotNone(value["error"])
        state.poll_once()
        self.assertTrue(state.snapshot()["data_fresh"])
        self.assertIsNone(state.snapshot()["error"])

    def test_malformed_response_is_recoverable(self):
        values = iter(({"broken": True}, sample()))
        state = BridgeState(fetcher=lambda host: next(values))
        state.poll_once()
        self.assertFalse(state.snapshot()["device_online"])
        state.poll_once()
        self.assertTrue(state.snapshot()["device_online"])

    def test_config_change_discards_inflight_old_response(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(host):
            entered.set()
            self.assertTrue(release.wait(2))
            return sample()
        state = BridgeState(fetcher=fetch)
        thread = threading.Thread(target=state.poll_once)
        thread.start()
        self.assertTrue(entered.wait(2))
        state.set_device("192.168.1.25")
        release.set()
        thread.join(2)
        value = state.snapshot()
        self.assertEqual(value["device_host"], "192.168.1.25")
        self.assertFalse(value["device_online"])
        self.assertIsNone(value["telemetry"]["airspeed_kt"])

    def test_snapshots_do_not_allow_mutating_shared_readings(self):
        state = BridgeState(fetcher=lambda host: sample())
        state.poll_once()
        state.snapshot()["telemetry"]["altitude_ft"] = 0
        self.assertEqual(state.snapshot()["telemetry"]["altitude_ft"], 3456.0)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.static = self.directory / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><title>Avionics</title>")
        self.state = BridgeState(config_path=self.directory / "device.local.json", fetcher=lambda host: sample())
        self.server = DashboardServer(("127.0.0.1", 0), self.state, self.static)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method="GET", path="/api/telemetry", body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        client.request(method, path, body=body, headers=headers or {})
        result = client.getresponse()
        response = (result.status, result.read(), dict(result.getheaders()))
        client.close()
        return response

    def test_live_endpoint(self):
        self.state.poll_once()
        code, body, headers = self.request()
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["device_online"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_config_validates_and_persists(self):
        code, body, _ = self.request("POST", "/api/config", json.dumps({"device_host": "192.168.1.25"}),
                                     {"Content-Type": "application/json", "Origin": "http://127.0.0.1:" + str(self.port)})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["device_host"], "192.168.1.25")
        self.assertEqual(saved_device(self.directory / "device.local.json"), "192.168.1.25")
        for host in ("8.8.8.8", "192.168.1.24:8080", "http://192.168.1.24"):
            code, _, _ = self.request("POST", "/api/config", json.dumps({"device_host": host}), {"Content-Type": "application/json"})
            self.assertEqual(code, 400)
        self.assertEqual(self.state.config()["device_host"], "192.168.1.25")

    def test_host_and_origin_guards(self):
        self.assertEqual(self.request(headers={"Host": "evil.example:" + str(self.port)})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "http://localhost:1"})[0], 403)

    def test_static_allowlist_never_serves_configuration(self):
        self.assertEqual(self.request(path="/")[0], 200)
        for path in ("/device.local.json", "/server.py", "/../server.py", "/static/../server.py", "/%2e%2e/server.py"):
            self.assertEqual(self.request(path=path)[0], 404)

    def test_oversized_body_and_wrong_content_type(self):
        self.assertEqual(self.request("POST", "/api/config", "x" * 1025, {"Content-Type": "application/json"})[0], 413)
        self.assertEqual(self.request("POST", "/api/config", "{}", {"Content-Type": "text/plain"})[0], 415)


if __name__ == "__main__":
    unittest.main()
