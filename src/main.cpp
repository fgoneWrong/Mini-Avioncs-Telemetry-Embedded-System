#include <Arduino.h>
#include <XPLDevices.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <esp_system.h>
#include <math.h>
#include "wifi_config.h"

namespace {
constexpr char kDeviceName[] = "ESP32-S3 Avionics WiFi";
constexpr char kHostname[] = "esp32-avionics";
constexpr uint32_t kFreshForMs = 3000;

struct FlightValues {
    float airspeed = NAN;
    float altitude = NAN;
    float heading = NAN;
    float pitch = NAN;
    float roll = NAN;
    float verticalSpeed = NAN;
    float simTime = NAN;
};
struct FlightSnapshot {
    FlightValues values;
    uint32_t session = 0;
    uint32_t sequence = 0;
    uint32_t lastReceiveMs = 0;
    bool received = false;
    bool connected = false;
    bool paused = false;
};
struct NetworkSnapshot {
    bool connected = false;
    char ip[16] = "";
};

FlightValues values;
long simPaused = 0;
FlightSnapshot flight;
NetworkSnapshot network;
portMUX_TYPE snapshotMux = portMUX_INITIALIZER_UNLOCKED;
WebServer server(80);
char bootId[17];
char deviceId[32];
bool taskStarted = false;

String numberJson(float value) {
    return isfinite(value) ? String(value, 2) : String("null");
}

void sendTelemetry() {
    FlightSnapshot sample;
    portENTER_CRITICAL(&snapshotMux);
    sample = flight;
    portEXIT_CRITICAL(&snapshotMux);
    const uint32_t now = millis();
    const uint32_t age = now - sample.lastReceiveMs;
    const bool fresh = sample.connected && sample.received && age <= kFreshForMs;
    String json;
    json.reserve(1024);
    json = "{\"device_id\":\"";
    json += deviceId;
    json += "\",\"boot_id\":\"";
    json += bootId;
    json += "\",\"session_id\":" + String(sample.session);
    json += ",\"sequence\":" + String(sample.sequence);
    json += ",\"uptime_ms\":" + String(now);
    json += ",\"wifi_connected\":true,\"wifi_rssi_dbm\":" + String(WiFi.RSSI());
    json += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
    json += ",\"xplane_connected\":";
    json += sample.connected ? "true" : "false";
    json += ",\"data_fresh\":";
    json += fresh ? "true" : "false";
    json += ",\"sim_paused\":";
    json += sample.paused ? "true" : "false";
    json += ",\"age_ms\":" + (sample.received ? String(age) : String("null"));
    json += ",\"telemetry\":{\"airspeed_kt\":" + numberJson(sample.values.airspeed);
    json += ",\"altitude_ft\":" + numberJson(sample.values.altitude);
    json += ",\"heading_deg\":" + numberJson(sample.values.heading);
    json += ",\"pitch_deg\":" + numberJson(sample.values.pitch);
    json += ",\"roll_deg\":" + numberJson(sample.values.roll);
    json += ",\"vertical_speed_fpm\":" + numberJson(sample.values.verticalSpeed);
    json += ",\"sim_time_s\":" + numberJson(sample.values.simTime) + "}}";
    server.sendHeader("Cache-Control", "no-store");
    server.send(200, "application/json", json);
}

// Wi-Fi and HTTP run separately so slow HTTP clients cannot block USB parsing.
// Only loop() accesses XP; tasks exchange small snapshots under a short lock.
void networkTask(void *) {
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(kHostname);
    WiFi.setAutoReconnect(true);
    WiFi.begin("MMF", "fahim2006");
    server.on("/api/telemetry", HTTP_GET, sendTelemetry);
    server.on("/", HTTP_GET, []() {
        server.send(200, "text/plain", "ESP32 avionics telemetry online. Open your PC's localhost dashboard. API: /api/telemetry\n");
    });
    server.onNotFound([]() { server.send(404, "text/plain", "Not found"); });

    bool online = false;
    uint32_t lastAttemptMs = millis();
    for (;;) {
        const bool connected = WiFi.status() == WL_CONNECTED;
        if (connected && !online) {
            if (MDNS.begin(kHostname)) {
                MDNS.addService("http", "tcp", 80);
            }
            server.begin();
            online = true;
        } else if (!connected && online) {
            server.stop();
            MDNS.end();
            online = false;
        }
        NetworkSnapshot status;
        status.connected = connected;
        if (connected) {
            const String address = WiFi.localIP().toString();
            address.toCharArray(status.ip, sizeof(status.ip));
        } else if (millis() - lastAttemptMs >= 15000) {
            WiFi.reconnect();
            lastAttemptMs = millis();
        }
        portENTER_CRITICAL(&snapshotMux);
        network = status;
        portEXIT_CRITICAL(&snapshotMux);
        if (connected) server.handleClient();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
}

void setup() {
    Serial.begin(XPLDIRECT_BAUDRATE);
    XP.begin(kDeviceName);
    snprintf(bootId, sizeof(bootId), "%08lx%08lx",
             static_cast<unsigned long>(esp_random()), static_cast<unsigned long>(esp_random()));
    const uint64_t chipId = ESP.getEfuseMac();
    snprintf(deviceId, sizeof(deviceId), "esp32-s3-%08lx",
             static_cast<unsigned long>(chipId & 0xffffffff));

    XP.registerDataRef(F("sim/cockpit2/gauges/indicators/airspeed_kts_pilot"), XPL_READ, 100, 0, &values.airspeed);
    XP.registerDataRef(F("sim/cockpit2/gauges/indicators/altitude_ft_pilot"), XPL_READ, 100, 0, &values.altitude);
    XP.registerDataRef(F("sim/cockpit2/gauges/indicators/heading_AHARS_deg_mag_pilot"), XPL_READ, 100, 0, &values.heading);
    XP.registerDataRef(F("sim/cockpit2/gauges/indicators/pitch_AHARS_deg_pilot"), XPL_READ, 100, 0, &values.pitch);
    XP.registerDataRef(F("sim/cockpit2/gauges/indicators/roll_AHARS_deg_pilot"), XPL_READ, 100, 0, &values.roll);
    XP.registerDataRef(F("sim/cockpit2/gauges/indicators/vvi_fpm_pilot"), XPL_READ, 100, 0, &values.verticalSpeed);
    XP.registerDataRef(F("sim/time/total_running_time_sec"), XPL_READ, 100, 0, &values.simTime);
    XP.registerDataRef(F("sim/time/paused"), XPL_READ, 100, 0, &simPaused);

    taskStarted = xTaskCreatePinnedToCore(networkTask, "avionics-http", 8192,
                                        nullptr, 1, nullptr, 0) == pdPASS;
}

void loop() {
    static uint32_t generation = 0;
    static uint32_t sequence = 0;
    static uint32_t lastReceiveMs = 0;
    static bool received = false;
    static bool announced = false;
    static bool wifiAnnounced = false;
    static uint32_t lastWifiLogMs = 0;

    XP.xloop();
    if (generation != XP.connectionGeneration()) {
        generation = XP.connectionGeneration();
        values = FlightValues{};
        simPaused = 0;
        received = false;
        announced = false;
        wifiAnnounced = false;
    }
    if (XP.datarefsUpdated()) {
        received = true;
        lastReceiveMs = millis();
        ++sequence;
    }
    const bool usbOpen = static_cast<bool>(Serial);
    const bool connected = usbOpen && XP.connectionStatus() && XP.allDataRefsRegistered();
    if (!usbOpen || !XP.connectionStatus()) {
        received = false;
        announced = false;
        wifiAnnounced = false;
    }
    FlightSnapshot sample;
    sample.values = values;
    sample.session = generation;
    sample.sequence = sequence;
    sample.lastReceiveMs = lastReceiveMs;
    sample.received = received;
    sample.connected = connected;
    sample.paused = simPaused != 0;
    NetworkSnapshot wifi;
    portENTER_CRITICAL(&snapshotMux);
    flight = sample;
    wifi = network;
    portEXIT_CRITICAL(&snapshotMux);

    if (connected && received && !announced) {
        XP.sendDebugMessage("USB TEST PASS: ESP32-S3 received X-Plane telemetry.");
        XP.sendSpeakMessage("ESP32 connected. Flight data received.");
        announced = true;
    }
    if (connected && wifi.connected && !wifiAnnounced) {
        char message[72];
        snprintf(message, sizeof(message), "WIFI CONNECTED: IP %s; esp32-avionics.local", wifi.ip);
        XP.sendDebugMessage(message);
        XP.sendSpeakMessage("ESP32 Wi Fi connected.");
        wifiAnnounced = true;
    }
    if (!wifi.connected) wifiAnnounced = false;
    if (connected && millis() - lastWifiLogMs >= 15000) {
        if (!taskStarted) XP.sendDebugMessage("WIFI ERROR: network task could not start; restart board.");
        else if (!wifi.connected) XP.sendDebugMessage("WIFI WAITING: check 2.4 GHz network and local WiFi config.");
        else {
            char message[72];
            snprintf(message, sizeof(message), "WIFI IP: %s; dashboard: http://localhost:8000", wifi.ip);
            XP.sendDebugMessage(message);
        }
        lastWifiLogMs = millis();
    }
    delay(1);
}
