"use strict";

// The trace stores actual received data only. Wall time bounds its visible history.
const POLL_INTERVAL_MS = 200;
const FETCH_TIMEOUT_MS = 3000;
const TRACE_WINDOW_MS = 120000;
const TRACE_MAX_SAMPLES = 1200;
const isFiniteNumber = value => typeof value === "number" && Number.isFinite(value);
const hasLiveData = data => !!(data && data.device_online && data.xplane_connected && data.data_fresh);

function sessionKey(data) {
  if (!data || data.boot_id == null || data.session_id == null) return null;
  return JSON.stringify([data.device_id || data.device_host || "", data.boot_id, data.session_id]);
}

class FlightTrace {
  constructor() { this.samples = []; this.session = null; this.lastSequence = null; this.paused = false; this.resetCount = 0; }
  clear() { this.samples = []; }
  reset() { this.clear(); this.session = null; this.lastSequence = null; this.resetCount++; }
  prune(now) { this.samples = this.samples.filter(sample => now - sample.receivedAt <= TRACE_WINDOW_MS).slice(-TRACE_MAX_SAMPLES); }
  accept(data, now = Date.now()) {
    this.prune(now);
    const key = sessionKey(data);
    if (data && data.device_online && key !== null && key !== this.session) {
      this.clear(); this.lastSequence = null; this.session = key; this.resetCount++;
    }
    if (!hasLiveData(data) || data.sequence == null || data.sequence === this.lastSequence) return false;
    this.lastSequence = data.sequence;
    const values = data.telemetry || {};
    if (this.paused || !isFiniteNumber(values.airspeed_kt) || !isFiniteNumber(values.altitude_ft)) return false;
    this.samples.push({ speed: values.airspeed_kt, altitude: values.altitude_ft, receivedAt: now, sequence: data.sequence });
    if (this.samples.length > TRACE_MAX_SAMPLES) this.samples.splice(0, this.samples.length - TRACE_MAX_SAMPLES);
    return true;
  }
}

function niceBounds(values, fallback, minimumSpan, targetTicks = 5) {
  if (!values.length) return { min: fallback[0], max: fallback[1], step: (fallback[1] - fallback[0]) / targetTicks };
  let low = Math.min(...values), high = Math.max(...values);
  const centre = (high + low) / 2, span = Math.max(high - low, minimumSpan) * 1.2;
  low = centre - span / 2; high = centre + span / 2;
  const rough = (high - low) / targetTicks, power = 10 ** Math.floor(Math.log10(rough));
  const fraction = rough / power;
  const step = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10) * power;
  return { min: Math.floor(low / step) * step, max: Math.ceil(high / step) * step, step };
}

function formatTime(seconds) {
  if (!isFiniteNumber(seconds) || seconds < 0) return "—";
  const total = Math.floor(seconds);
  return `${Math.floor(total / 3600).toString().padStart(2, "0")}:${Math.floor(total / 60 % 60).toString().padStart(2, "0")}:${(total % 60).toString().padStart(2, "0")}`;
}

// Node can exercise the real sample filtering and axis calculations without a DOM.
if (typeof module !== "undefined" && module.exports) module.exports = { FlightTrace, niceBounds, formatTime, hasLiveData, sessionKey };

if (typeof document !== "undefined") initializeDashboard();

function initializeDashboard() {
  const $ = id => document.getElementById(id);
  const trace = new FlightTrace();
  const chart = $("trace-canvas"), attitude = $("attitude-canvas");
  const fields = ["airspeed_kt", "altitude_ft", "heading_deg", "pitch_deg", "roll_deg", "vertical_speed_fpm", "sim_time_s"];
  const lastValues = Object.fromEntries(fields.map(key => [key, null]));
  let current = null, deviceHost = "esp32-avionics.local", lastRealUpdate = null, plot = null;
  let hoverPosition = null, configRevision = 0, session = null, lastReceivedSequence = null;
  const numeric = (value, digits = 0) => isFiniteNumber(value) ? value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";

  function setChip(prefix, text, state) {
    $(`${prefix}-status`).textContent = text;
    $(`${prefix}-chip`).className = `status-chip ${state || ""}`;
  }

  function setNotice(title, message) {
    $("notice").hidden = !title;
    if (title) {
      if ($("notice-title").textContent !== title) $("notice-title").textContent = title;
      if ($("notice-text").textContent !== message) $("notice-text").textContent = message;
    }
  }

  function resetInstruments() {
    fields.forEach(key => { lastValues[key] = null; });
    lastRealUpdate = null; lastReceivedSequence = null;
  }

  function updateDashboard(data) {
    const now = Date.now();
    current = data;
    if (data.device_host) deviceHost = data.device_host;
    const key = sessionKey(data);
    if (data.device_online && key !== null && key !== session) {
      resetInstruments(); session = key;
    }
    const live = hasLiveData(data);
    trace.accept(data, now);
    if (live) {
      fields.forEach(key => { if (isFiniteNumber(data.telemetry?.[key])) lastValues[key] = data.telemetry[key]; });
      if (data.sequence !== lastReceivedSequence || lastRealUpdate == null) { lastRealUpdate = now; lastReceivedSequence = data.sequence; }
    }
    $("device-address").textContent = data.ip || deviceHost;
    $("signal-strength").textContent = isFiniteNumber(data.wifi_rssi_dbm) ? `Wi-Fi ${data.wifi_rssi_dbm} dBm` : "Wi-Fi —";
    setChip("device", data.device_online ? "Online" : "Offline", data.device_online ? "good" : "bad");
    setChip("sim", data.xplane_connected ? "Connected" : "Waiting", data.xplane_connected ? "good" : "warn");
    const age = isFiniteNumber(data.age_ms) ? `${(data.age_ms / 1000).toFixed(1)}s old` : "Waiting";
    setChip("data", live ? (data.sim_paused ? "Sim paused" : "Live") : (data.data_fresh === false && isFiniteNumber(data.age_ms) ? age : "Waiting"), live ? "good" : "warn");
    $("instruments").classList.toggle("stale", !live);
    $("instrument-state").textContent = live ? (data.sim_paused ? "SIMULATION PAUSED" : "LIVE FLIGHT DATA") : lastRealUpdate ? "LAST KNOWN VALUES · STALE" : "AWAITING TELEMETRY";
    if (!data.device_online) {
      setNotice("ESP32 is not reachable", data.error || `Waiting for ${deviceHost}. Keep the ESP32 powered and connect this computer to the same network. Open Connection to use the board's IP address.`);
    } else if (!data.xplane_connected) {
      setNotice("Waiting for X-Plane", "The board is on Wi-Fi. Connect its native USB port, close Serial Monitor, and load an aircraft with the XPLDirect plugin enabled.");
    } else if (!live) {
      setNotice("Flight data is stale", "The simulator link is present, but fresh telemetry has not arrived. The dimmed instruments retain their last received values; the trace is waiting.");
    } else if (data.sim_paused) {
      setNotice("Simulation is paused", "The connection is live. Resume X-Plane to continue the flight; the trace uses received values.");
    } else setNotice(null, null);
    renderInstruments();
    renderTrace();
  }

  function renderInstruments() {
    $("airspeed-value").textContent = numeric(lastValues.airspeed_kt, 1);
    $("altitude-value").textContent = numeric(lastValues.altitude_ft);
    const heading = isFiniteNumber(lastValues.heading_deg) ? ((lastValues.heading_deg % 360) + 360) % 360 : null;
    $("heading-value").textContent = heading === null ? "—" : (Math.round(heading) % 360).toString().padStart(3, "0");
    $("heading-direction").textContent = heading === null ? "—" : ["NORTH", "NORTHEAST", "EAST", "SOUTHEAST", "SOUTH", "SOUTHWEST", "WEST", "NORTHWEST"][Math.round(heading / 45) % 8];
    $("pitch-value").textContent = numeric(lastValues.pitch_deg, 1) + (isFiniteNumber(lastValues.pitch_deg) ? "°" : "");
    $("roll-value").textContent = numeric(lastValues.roll_deg, 1) + (isFiniteNumber(lastValues.roll_deg) ? "°" : "");
    const vertical = lastValues.vertical_speed_fpm;
    $("vertical-speed-value").textContent = (isFiniteNumber(vertical) && vertical > 0 ? "+" : "") + numeric(vertical);
    $("vertical-direction").textContent = !isFiniteNumber(vertical) ? "—" : Math.abs(vertical) < 50 ? "LEVEL" : vertical > 0 ? "CLIMBING" : "DESCENDING";
    $("vsi-symbol").textContent = !isFiniteNumber(vertical) ? "↕" : Math.abs(vertical) < 50 ? "→" : vertical > 0 ? "↗" : "↘";
    $("flight-time-value").textContent = formatTime(lastValues.sim_time_s);
    $("sim-run-state").textContent = !hasLiveData(current) ? "—" : current.sim_paused ? "PAUSED" : "RUNNING";
    $("last-update").textContent = lastRealUpdate ? `Last received ${new Date(lastRealUpdate).toLocaleTimeString([], { hour12: false })}` : "No flight data received";
    renderAttitude();
  }

  function prepareCanvas(canvas) {
    const rect = canvas.getBoundingClientRect(), ratio = Math.min(window.devicePixelRatio || 1, 3);
    const width = Math.max(rect.width, 1), height = Math.max(rect.height, 1);
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx, width, height };
  }

  function renderAttitude() {
    const { ctx, width: w, height: h } = prepareCanvas(attitude);
    ctx.fillStyle = "#0c151f"; ctx.fillRect(0, 0, w, h);
    if (!isFiniteNumber(lastValues.pitch_deg) || !isFiniteNumber(lastValues.roll_deg)) {
      ctx.fillStyle = "#647c91"; ctx.font = "10px Segoe UI"; ctx.textAlign = "center"; ctx.fillText("NO ATTITUDE DATA", w / 2, h / 2 + 3); return;
    }
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip();
    ctx.translate(w / 2, h / 2); ctx.rotate(-lastValues.roll_deg * Math.PI / 180);
    ctx.translate(0, lastValues.pitch_deg * h / 65);
    ctx.fillStyle = "#234961"; ctx.fillRect(-w * 5, -h * 10, w * 10, h * 10);
    ctx.fillStyle = "#62533e"; ctx.fillRect(-w * 5, 0, w * 10, h * 10);
    ctx.strokeStyle = "#d6e4e6"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(-w * 5, 0); ctx.lineTo(w * 5, 0); ctx.stroke();
    ctx.strokeStyle = "#e2e9ec85";
    for (let deg = -30; deg <= 30; deg += 10) {
      if (deg === 0) continue;
      const y = -deg * h / 65, half = deg % 20 === 0 ? 15 : 9;
      ctx.beginPath(); ctx.moveTo(-half, y); ctx.lineTo(half, y); ctx.stroke();
    }
    ctx.restore(); ctx.strokeStyle = "#ffd684"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(w / 2 - 26, h / 2); ctx.lineTo(w / 2 - 9, h / 2); ctx.lineTo(w / 2 - 9, h / 2 + 4); ctx.moveTo(w / 2 + 26, h / 2); ctx.lineTo(w / 2 + 9, h / 2); ctx.lineTo(w / 2 + 9, h / 2 + 4); ctx.stroke();
    ctx.fillStyle = "#ffd684"; ctx.beginPath(); ctx.arc(w / 2, h / 2, 2, 0, Math.PI * 2); ctx.fill();
  }

  function renderTrace() {
    trace.prune(Date.now());
    const { ctx, width: w, height: h } = prepareCanvas(chart);
    const left = w < 500 ? 69 : 81, right = 26, top = 20, bottom = 54;
    const plotW = Math.max(10, w - left - right), plotH = Math.max(10, h - top - bottom);
    const samples = trace.samples;
    const x = niceBounds(samples.map(s => s.speed), [0, 200], 10, w < 500 ? 4 : 6);
    const y = niceBounds(samples.map(s => s.altitude), [0, 10000], 200, 5);
    const px = speed => left + (speed - x.min) / (x.max - x.min) * plotW;
    const py = altitude => top + plotH - (altitude - y.min) / (y.max - y.min) * plotH;
    plot = { left, top, width: plotW, height: plotH, px, py };
    ctx.font = "10px Consolas, monospace"; ctx.lineWidth = 1;
    for (let value = x.min; value <= x.max + x.step / 10; value += x.step) {
      const sx = px(value);
      ctx.strokeStyle = "#233444"; ctx.beginPath(); ctx.moveTo(sx, top); ctx.lineTo(sx, top + plotH); ctx.stroke();
      ctx.fillStyle = "#7990a5"; ctx.textAlign = "center"; ctx.fillText(numeric(value, x.step < 1 ? 1 : 0), sx, top + plotH + 20);
    }
    for (let value = y.min; value <= y.max + y.step / 10; value += y.step) {
      const sy = py(value);
      ctx.strokeStyle = "#233444"; ctx.beginPath(); ctx.moveTo(left, sy); ctx.lineTo(left + plotW, sy); ctx.stroke();
      ctx.fillStyle = "#7990a5"; ctx.textAlign = "right"; ctx.fillText(numeric(value), left - 10, sy + 3);
    }
    ctx.font = "10px Segoe UI, sans-serif"; ctx.fillStyle = "#a1b1c0"; ctx.textAlign = "center";
    ctx.fillText("INDICATED AIRSPEED  (kt)", left + plotW / 2, h - 10);
    ctx.save(); ctx.translate(15, top + plotH / 2); ctx.rotate(-Math.PI / 2); ctx.fillText("INDICATED ALTITUDE  (ft)", 0, 0); ctx.restore();
    if (samples.length) {
      ctx.save(); ctx.beginPath(); ctx.rect(left - 5, top - 5, plotW + 10, plotH + 10); ctx.clip();
      ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.strokeStyle = hasLiveData(current) ? "#65dfdf" : "#50878d";
      ctx.beginPath(); samples.forEach((sample, index) => index ? ctx.lineTo(px(sample.speed), py(sample.altitude)) : ctx.moveTo(px(sample.speed), py(sample.altitude))); ctx.stroke();
      const end = samples[samples.length - 1], endX = px(end.speed), endY = py(end.altitude);
      ctx.fillStyle = "#65dfdf20"; ctx.beginPath(); ctx.arc(endX, endY, 10, 0, 2 * Math.PI); ctx.fill();
      ctx.fillStyle = hasLiveData(current) ? "#d7ffff" : "#8db1b5"; ctx.beginPath(); ctx.arc(endX, endY, 3.5, 0, 2 * Math.PI); ctx.fill();
      ctx.restore();
    }
    $("graph-empty").hidden = samples.length > 0;
    $("sample-count").textContent = `${samples.length} ${samples.length === 1 ? "sample" : "samples"}`;
    const recording = $("recording-status");
    const label = trace.paused ? "Paused" : hasLiveData(current) ? "Recording" : "Waiting";
    recording.className = `recording-status ${trace.paused ? "paused" : hasLiveData(current) ? "recording" : ""}`;
    recording.lastChild.textContent = label;
    renderTooltip();
  }

  function renderTooltip() {
    const tooltip = $("graph-tooltip");
    if (!hoverPosition || !plot || !trace.samples.length) { tooltip.hidden = true; return; }
    let nearest = null, distance = 24 * 24;
    trace.samples.forEach(sample => {
      const d = (plot.px(sample.speed) - hoverPosition.x) ** 2 + (plot.py(sample.altitude) - hoverPosition.y) ** 2;
      if (d <= distance) { nearest = sample; distance = d; }
    });
    if (!nearest) { tooltip.hidden = true; return; }
    tooltip.textContent = `${numeric(nearest.speed, 1)} kt · ${numeric(nearest.altitude)} ft\n${new Date(nearest.receivedAt).toLocaleTimeString([], { hour12: false })}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(0, Math.min(hoverPosition.x + 14, chart.clientWidth - tooltip.offsetWidth - 4))}px`;
    tooltip.style.top = `${Math.max(0, Math.min(hoverPosition.y - 48, chart.clientHeight - tooltip.offsetHeight - 4))}px`;
  }

  async function fetchJSON(url, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      const response = await fetch(url, { cache: "no-store", ...options, signal: controller.signal });
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
      return value;
    } finally { clearTimeout(timeout); }
  }

  async function poll() {
    const started = Date.now(), revision = configRevision;
    try {
      const data = await fetchJSON("/api/telemetry");
      if (revision === configRevision) updateDashboard(data);
    } catch (error) {
      if (revision === configRevision) updateDashboard({ device_online: false, device_host: deviceHost, error: error.name === "AbortError" ? "The local server did not respond in time. Retrying automatically." : "The local server or device could not be reached. Keep the dashboard server running; it will retry automatically.", telemetry: {} });
    } finally { setTimeout(poll, Math.max(0, POLL_INTERVAL_MS - (Date.now() - started))); }
  }

  $("settings-toggle").addEventListener("click", () => {
    const panel = $("settings-panel"); panel.hidden = !panel.hidden;
    $("settings-toggle").setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) $("device-host").focus();
  });
  $("device-form").addEventListener("submit", async event => {
    event.preventDefault();
    const feedback = $("config-feedback"), save = $("save-host");
    save.disabled = true; feedback.className = "form-feedback"; feedback.textContent = "Saving connection…";
    try {
      const config = await fetchJSON("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device_host: $("device-host").value.trim() }) });
      configRevision++; deviceHost = config.device_host; $("device-host").value = deviceHost;
      session = null; trace.reset(); resetInstruments();
      updateDashboard({ device_online: false, device_host: deviceHost, error: "Connection saved. Waiting for the ESP32 to respond…", telemetry: {} });
      feedback.textContent = "Connection saved. Checking the board…";
    } catch (error) { feedback.className = "form-feedback error"; feedback.textContent = error.message; }
    finally { save.disabled = false; }
  });
  $("pause-trace").addEventListener("click", () => {
    trace.paused = !trace.paused;
    $("pause-trace").textContent = trace.paused ? "Resume trace" : "Pause trace";
    $("pause-trace").setAttribute("aria-pressed", String(trace.paused)); renderTrace();
  });
  $("clear-trace").addEventListener("click", () => { trace.clear(); renderTrace(); });
  chart.addEventListener("pointermove", event => { const rect = chart.getBoundingClientRect(); hoverPosition = { x: event.clientX - rect.left, y: event.clientY - rect.top }; renderTooltip(); });
  chart.addEventListener("pointerleave", () => { hoverPosition = null; renderTooltip(); });
  new ResizeObserver(() => { renderTrace(); renderAttitude(); }).observe($("instruments"));
  new ResizeObserver(renderTrace).observe($("graph-frame"));
  $("device-host").value = deviceHost;
  fetchJSON("/api/config").then(config => { if (!configRevision && config.device_host) { deviceHost = config.device_host; $("device-host").value = deviceHost; $("device-address").textContent = deviceHost; } }).catch(() => {});
  renderInstruments(); renderTrace(); poll();
}
