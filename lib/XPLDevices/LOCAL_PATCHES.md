# Local ESP32 compatibility changes

Based on XPLDevices 0.13.1 by Martin Ruskowski, including its bundled
XPLDirect implementation by Michael Gerlicher. The upstream LICENSE and
library manifest are preserved.

- Keep the existing Stream-based native USB interface and wire protocol.
- Use normal byte-string matching and formatting on ESP32 instead of AVR-only
  far-flash functions and the AVR `%S` printf extension.
- Rename AnalogIn range parameters so they do not hide ESP32's min/max functions.
- Match the argument types in LedShift/ShiftOut's 64-pin limits. Keep AVR
  register access under its architecture guard and use digitalWrite on ESP32,
  preserving descending bit order and the final latch pulse. Initialize output
  state/update flags before constructors send their first frame.
- Zero-initialize dataref records, including their update flags and timestamps;
  preserve the provided divider for floating-point array registrations.
- Clear registration and update state on a new handshake, reset, or normal
  X-Plane shutdown so a previous aircraft's state is not reported as ready.
- Expose a connection-generation counter so the dashboard can clear old flight
  samples and invalidate instrument values on every new registration session.
- Guard invalid hasUpdated handles, reserve receive-buffer terminator space,
  and bound the variable-length registration/name/debug packet formatting.

Keep application diagnostics out of raw Serial writes: Serial carries the
XPLDirect protocol. Use XP.sendDebugMessage for plugin log messages.
