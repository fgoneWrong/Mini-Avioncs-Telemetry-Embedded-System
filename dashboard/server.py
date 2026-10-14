#!/usr/bin/env python3
"""Local dashboard server. Uses only Python's standard library.

ESP32 -> Wi-Fi HTTP telemetry -> this loopback-only server -> browser.
The ESP32's native USB remains exclusively owned by the XPLDirect plugin.
"""

import argparse
import copy
import ipaddress
import json
import math
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_DEVICE = "esp32-avionics.local"
DEFAULT_PORT = 8000
POLL_SECONDS = 0.2
FETCH_TIMEOUT_SECONDS = 1.0
MAX_DATA_AGE_MS = 3000
MAX_RESPONSE_BYTES = 32768
MAX_REQUEST_BYTES = 1024
TELEMETRY_FIELDS = (
    "airspeed_kt", "altitude_ft", "heading_deg", "pitch_deg", "roll_deg",
    "vertical_speed_fpm", "sim_time_s",
)
LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
LOCAL_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local\Z")


def validate_device_host(value):
    """Accept a private IPv4 address or a single mDNS name, never a URL."""
    if not isinstance(value, str):
        raise ValueError("Device address must be text.")
    value = value.strip().lower()
    if LOCAL_NAME.fullmatch(value):
        return value
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        address = None
    if address is not None and any(address in network for network in LAN_NETWORKS):
        return str(address)
    raise ValueError("Use a private LAN IPv4 address or a name such as esp32-avionics.local; omit http://, ports and paths.")


def _integer(value, name, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError("Invalid " + name)
    return value


def validate_telemetry(payload):
    """Copy only the documented fields; reject malformed/non-finite readings."""
    if not isinstance(payload, dict):
        raise ValueError("Telemetry must be a JSON object")
    result = {}
    for key in ("device_id", "boot_id", "ip"):
        value = payload.get(key)
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError("Invalid " + key)
        result[key] = value
    for key in ("session_id", "sequence", "uptime_ms"):
        result[key] = _integer(payload.get(key), key)
    if "age_ms" not in payload:
        raise ValueError("Missing age_ms")
    result["age_ms"] = _integer(payload["age_ms"], "age_ms", nullable=True)
    for key in ("wifi_connected", "xplane_connected", "data_fresh", "sim_paused"):
        if type(payload.get(key)) is not bool:
            raise ValueError("Invalid " + key)
        result[key] = payload[key]
    rssi = payload.get("wifi_rssi_dbm")
    if rssi is not None and (type(rssi) is not int or not -200 <= rssi <= 0):
        raise ValueError("Invalid wifi_rssi_dbm")
    result["wifi_rssi_dbm"] = rssi
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict):
        raise ValueError("Missing telemetry object")
    result["telemetry"] = {}
    for key in TELEMETRY_FIELDS:
        if key not in telemetry:
            raise ValueError("Missing telemetry field " + key)
        value = telemetry[key]
        if value is not None and (
            type(value) not in (float, int) or not math.isfinite(value)
        ):
            raise ValueError("Invalid telemetry field " + key)
        result["telemetry"][key] = value
    # A source cannot be fresh before it has received a sample or after losing XP.
    if not result["xplane_connected"] or result["age_ms"] is None:
        result["data_fresh"] = False
    return result


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch_device(device_host):
    # Disable OS/environment proxies: this request must stay on the local network.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    request = Request(
        "http://" + validate_device_host(device_host) + "/api/telemetry",
        headers={"Accept": "application/json", "User-Agent": "AvionicsDashboard/1.0"},
    )
    with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError("Unexpected HTTP status")
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Telemetry response is too large")
    return json.loads(data.decode("utf-8"))


def empty_payload():
    return {
        "device_id": "", "boot_id": "", "session_id": 0, "sequence": 0,
        "uptime_ms": 0, "wifi_connected": False, "wifi_rssi_dbm": None,
        "ip": "", "xplane_connected": False, "data_fresh": False,
        "sim_paused": False, "age_ms": None,
        "telemetry": {key: None for key in TELEMETRY_FIELDS},
    }


class BridgeState:
    def __init__(self, device_host=DEFAULT_DEVICE, config_path=None, fetcher=None):
        self._host = validate_device_host(device_host)
        self._config_path = Path(config_path) if config_path else None
        self._fetcher = fetcher or fetch_device
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._generation = 0
        self._payload = empty_payload()
        self._online = False
        self._error = "Waiting for the ESP32."
        self._received_at = None

    def config(self):
        with self._lock:
            return {"device_host": self._host}

    def set_device(self, host):
        host = validate_device_host(host)
        with self._lock:
            if self._config_path:
                temporary = self._config_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"device_host": host}, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self._config_path)
            self._host = host
            self._generation += 1
            # A new source must not inherit old aircraft readings or identity.
            self._payload = empty_payload()
            self._online = False
            self._received_at = None
            self._error = "Waiting for the ESP32."
        self._wake.set()

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._payload)
            result.update(device_online=self._online, device_host=self._host, error=self._error)
            if self._received_at is not None and result["age_ms"] is not None:
                result["age_ms"] += max(0, int((time.monotonic() - self._received_at) * 1000))
            if not self._online or (result["age_ms"] is not None and result["age_ms"] > MAX_DATA_AGE_MS):
                result["data_fresh"] = False
            return result

    def poll_once(self):
        with self._lock:
            host, generation = self._host, self._generation
        try:
            payload = validate_telemetry(self._fetcher(host))
            error = None
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
            payload, error = None, "ESP32 returned invalid telemetry. Check that its firmware is current."
        except HTTPError as exc:
            payload, error = None, "ESP32 HTTP error " + str(exc.code) + "."
        except (URLError, OSError, TimeoutError):
            payload, error = None, "Cannot reach ESP32. Check Wi-Fi and its address; try its LAN IP if .local does not resolve."
        except Exception:
            # One unexpected upstream failure must not silently kill the poller.
            payload, error = None, "Could not read ESP32 telemetry."
        with self._lock:
            if generation != self._generation:
                return  # Configuration changed while the old request was running.
            self._online = payload is not None
            self._error = error
            if payload is not None:
                self._payload = payload
                self._received_at = time.monotonic()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            started = time.monotonic()
            self.poll_once()
            self._wake.wait(max(0.0, POLL_SECONDS - (time.monotonic() - started)))

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="esp32-poller", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=FETCH_TIMEOUT_SECONDS + 1)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state, static_dir):
        self.state = state
        self.static_dir = Path(static_dir)
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AvionicsDashboard/1.0"
    sys_version = ""
    static_files = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/style.css": ("style.css", "text/css; charset=utf-8"),
        "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    }

    def log_message(self, fmt, *args):
        pass  # Browser polling should not flood the terminal.

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status, value):
        self._send(status, json.dumps(value, allow_nan=False).encode("utf-8"))

    def _authority(self, value):
        try:
            parsed = urlsplit("http://" + value)
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return None
            if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                return None
            port = parsed.port or 80
            if port != self.server.server_port:
                return None
            return parsed.hostname, port
        except (ValueError, TypeError):
            return None

    def _allowed(self):
        if len(self.headers.get_all("Host", [])) != 1:
            self._json(403, {"error": "A loopback Host header is required."})
            return False
        authority = self._authority(self.headers.get("Host", ""))
        if authority is None:
            self._json(403, {"error": "Only local dashboard requests are allowed."})
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                valid = (parsed.scheme == "http" and not parsed.path and not parsed.query
                         and not parsed.fragment and self._authority(parsed.netloc) == authority)
            except ValueError:
                valid = False
            if not valid:
                self._json(403, {"error": "Origin must match the local dashboard."})
                return False
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        if path == "/api/telemetry":
            self._json(200, self.server.state.snapshot())
        elif path == "/api/config":
            self._json(200, self.server.state.config())
        elif path in self.static_files:
            filename, content_type = self.static_files[path]
            try:
                content = (self.server.static_dir / filename).read_bytes()
            except OSError:
                self._json(404, {"error": "Dashboard asset is missing: " + filename})
                return
            self._send(200, content, content_type)
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self):
        if not self._allowed():
            return
        if urlsplit(self.path).path != "/api/config":
            self._json(404, {"error": "Not found."})
            return
        if self.headers.get("Transfer-Encoding"):
            self._json(400, {"error": "Transfer encoding is not supported."})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Send application/json."})
            return
        try:
            if len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError()
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            self._json(400, {"error": "A valid Content-Length is required."})
            return
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._json(413, {"error": "Configuration request is too large or empty."})
            return
        try:
            self.connection.settimeout(3)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request.")
            value = json.loads(body.decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {"device_host"}:
                raise ValueError("Send only a device_host field.")
            self.server.state.set_device(value["device_host"])
        except (ValueError, UnicodeError, RecursionError) as exc:
            self._json(400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "Invalid JSON."})
            return
        except OSError:
            self._json(500, {"error": "Could not save local device configuration."})
            return
        self._json(200, self.server.state.config())


def saved_device(config_path):
    try:
        if config_path.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError()
        value = json.loads(config_path.read_text(encoding="utf-8"))
        return validate_device_host(value["device_host"])
    except FileNotFoundError:
        return DEFAULT_DEVICE
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("Ignoring invalid device.local.json; using the default mDNS name.", file=sys.stderr)
        return DEFAULT_DEVICE


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=validate_device_host, help="ESP32 private IPv4 address or .local name")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Local HTTP port (default 8000)")
    parser.add_argument("--no-poll", action="store_true", help="Serve the UI without contacting the board")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in your default browser")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    directory = Path(__file__).resolve().parent
    config_path = directory / "device.local.json"
    host = args.device or saved_device(config_path)
    state = BridgeState(host, config_path)
    try:
        server = DashboardServer(("127.0.0.1", args.port), state, directory / "static")
    except OSError as exc:
        print("Cannot start dashboard: " + str(exc), file=sys.stderr)
        return 1
    if not args.no_poll:
        state.start()
    print("Dashboard: http://localhost:" + str(args.port))
    print("ESP32: " + host + " (change this address in the dashboard)")
    print("Press Ctrl+C to stop.")
    if args.open:
        webbrowser.open("http://localhost:" + str(args.port))
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
