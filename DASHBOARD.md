# Local flight instrument panel

Data flow: **X-Plane -> XPLDirect plugin -> native USB -> ESP32 -> Wi-Fi ->
localhost dashboard**. The localhost web server reads the ESP32 over Wi-Fi;
it never opens the USB COM port. No cloud services or online chart libraries
are used.

## Start

1. Build and upload the `esp32-s3-n16r2` environment in VS Code. Keep using the
   board's native **USB** port. Close Serial Monitor after upload.
2. The ESP32 joins the network configured in `include/wifi_config.h`.
   Your supplied credentials are already saved there. This file is ignored by
   Git; `wifi_config.example.h` is the shareable template. The network must
   provide 2.4 GHz Wi-Fi, and the PC must be able to reach devices on that LAN.
3. Start X-Plane with the XPLDirect plugin installed and load an aircraft.
   The device is now named **ESP32-S3 Avionics WiFi**. It announces receipt of
   flight data and, separately, **"ESP32 Wi Fi connected."**
4. Double-click **start-dashboard.cmd** in the project folder. Leave its
   terminal open. It opens **http://localhost:8000** in your browser.
5. The dashboard initially looks for **esp32-avionics.local**. If this name
   does not resolve on your PC, enter the board's LAN IP in the device-address
   field. XPLDirect's debug log reports `WIFI CONNECTED: IP ...` and repeats
   `WIFI IP: ...` every 15 seconds while the simulator is connected. You can
   also find `esp32-avionics` in the router's connected-device list.

If port 8000 is already being used, run `start-dashboard.cmd --port 8001`.
To supply the address on launch: `start-dashboard.cmd --device 192.168.1.123`.
The launcher uses PlatformIO's Python, or Python 3 from the Windows launcher.
No pip installation is needed. Stop the server with Ctrl+C.

## Instruments and graph

- Indicated airspeed (knots), indicated altitude (feet), magnetic heading,
  pitch, roll, and vertical speed (feet/minute).
- Altitude-versus-airspeed graph: **airspeed on X, altitude on Y**. The trace
  records recent real samples; it is not a time-axis chart.
- Pause recording freezes collection, not the aircraft or live instruments.
  Clear trace removes the current browser history.
- Values start blank. Stale or disconnected readings are visibly marked;
  missing data never becomes artificial zero readings. The chart resets for
  a new ESP32 boot or plugin registration session (such as a new aircraft).
- Standard X-Plane pilot instrument datarefs are used. Custom aircraft may
  implement their own instruments. Start with a default aircraft to compare.

The simulator total-running-time dataref provides ongoing traffic even during
steady flight. It is simulator uptime, not elapsed time since takeoff.

## Firmware behavior

USB and XPLDirect remain on the main task. Wi-Fi/HTTP run on a separate task
so a slow browser request cannot block USB telemetry processing. Connection
attempts retry, and unknown numeric readings are returned as JSON null.
Telemetry expires after three seconds without a received simulator update.

The ESP32 serves a read-only JSON endpoint at
`http://esp32-avionics.local/api/telemetry` (or its LAN IP). Only this telemetry
is returned; Wi-Fi credentials are never included. The browser server listens
on the PC's loopback interface and serves dashboard assets only.

## Project files

- `src/main.cpp`: Wi-Fi, USB datarefs, snapshots, and the board's HTTP endpoint.
- `include/wifi_config.h`: local Wi-Fi configuration; keep this file private.
- `dashboard/server.py`: localhost web server and Wi-Fi telemetry polling.
- `dashboard/static/`: offline instrument panel and chart.
- `dashboard/device.local.json`: optional saved board address, ignored by Git.

The earlier USB-only test passed on the physical board. This revision must be
built and uploaded before Wi-Fi or the new panel can receive live telemetry.
