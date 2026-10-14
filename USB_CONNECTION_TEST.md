# ESP32-S3 native USB connection test

This project uses the board's **USB** port, XPLDevices, and the **XPLDirect**
X-Plane plugin. No Python bridge, multiplexers, buttons, or LEDs are required.

## Run the test

1. Install the XPLDirect plugin under your X-Plane 12 `Resources/plugins/`
   folder and restart X-Plane if it was running.
2. Connect a USB data cable to the ESP32-S3's native **USB** port.
3. In PlatformIO, build and upload the `esp32-s3-n16r2` environment.
   If auto-detection fails, select the ESP32 COM port explicitly. COM6 was
   detected during setup, but it can change after flashing or reconnecting.
4. Close every serial monitor. XPLDirect needs exclusive access to this port.
5. Launch an aircraft in X-Plane. Look for the device named
   **ESP32-S3 Avionics USB Test** in the XPLDirect plugin's device information/log.
6. On the first received simulator value, the firmware asks X-Plane to speak
   **"ESP32 connected. Flight data received."** It also sends the plugin message
   **`USB TEST PASS: ESP32-S3 received X-Plane telemetry.`**
7. With the plugin's debug/serial logging enabled, look for `USB TEST` entries
   in the plugin log (normally `XPLDirectError.log` in the X-Plane root).
   The firmware reports simulator time and strobe status every five seconds
   and when the strobe value changes. Toggle the aircraft's strobe switch to
   verify changing data arrives. Custom aircraft may expose a different strobe
   dataref; use a default aircraft for the first test.

Speech may be muted or unavailable. The explicit PASS log message is the
alternative confirmation; seeing only the device name proves discovery,
not receipt of aircraft data. The PASS message records a successful transfer,
not continuous connection health. Periodic messages contain the latest values;
check that simulator time advances to verify fresh updates.

## Troubleshooting

- If the port is busy, close PlatformIO/Arduino Serial Monitor, other bridges,
  and X-Plane before uploading; reopen X-Plane afterward.
- If the first upload cannot connect, hold BOOT, tap RESET, release BOOT,
  upload, then tap RESET if necessary. Check the COM port again.
- If the plugin started before the device was ready, use its rescan/reset
  function or restart X-Plane with the board already connected.
- Do not use `Serial.print()` for debugging: Serial carries XPLDirect frames.
- The USB flags in `platformio.ini` select hardware USB CDC. `Serial0` refers
  to the other, UART-connected port and is not used by this test.
- The supplied memory settings assume 16 MB flash and Quad-SPI PSRAM; confirm
  the physical module matches them if you see boot or PSRAM errors.

## Local library

The installed XPLDevices 0.13.1 bundles its own XPLDirect implementation.
The project-owned copy in `lib/XPLDevices` fixes ESP32 portability issues;
see its `LOCAL_PATCHES.md`. Avoid replacing it with the unpatched
registry copy when testing. The X-Plane plugin's serial protocol is unchanged.

Source: https://github.com/MartinRusk/XPLDevices

## Validation during setup

- Detected native USB serial device COM6, USB VID:PID 303A:1001.
- PlatformIO resolved the project-owned XPLDevices 0.13.1 library.
- Adafruit MCP23017 2.3.2 and BusIO 1.17.4 are explicit project dependencies.
  This prevents PlatformIO from deleting/reinstalling the dependency directory
  while building the local XPLDevices library. Stale build records were cleaned;
  compile-database generation passed (this is not a full firmware build).
- Build did not reach source compilation: Windows could not start the installed
  Xtensa GCC 8.4 compiler (`Illegal System DLL Relocation`). The same compiler's
  standalone `--version` command failed too. Try Build in VS Code to determine
  whether this also affects your normal environment.
- No firmware was uploaded, and a live X-Plane connection is not yet verified.
