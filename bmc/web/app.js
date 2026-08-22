/* Web UI for the local camera control service.
 *
 * The service owns the camera connection and pushes state here over a
 * websocket, so this file only has to render and send. Controls the camera
 * does not implement are hidden rather than shown broken.
 */

const ISO_CHIPS = [200, 400, 800, 1600, 3200, 6400];

const el = (id) => document.getElementById(id);
const state = {
  supported: new Set(),
  values: {},
  presets: [],
  wbPresets: {},
  /** Sliders the user is currently dragging, so pushes do not fight them. */
  dragging: new Set(),
};

/* ----------------------------------------------------------------- sending */

async function send(url) {
  try {
    const response = await fetch(url);
    const text = (await response.text()).trim();
    if (!response.ok) {
      showError(text || `${response.status} ${response.statusText}`);
      return null;
    }
    clearError();
    return text;
  } catch (error) {
    showError(`Cannot reach the control service: ${error.message}`);
    return null;
  }
}

/** Rate-limit slider traffic: send at most every 120ms, always send the last. */
function throttle(fn, wait = 120) {
  let last = 0;
  let timer = null;
  return (...args) => {
    const now = Date.now();
    const remaining = wait - (now - last);
    clearTimeout(timer);
    if (remaining <= 0) {
      last = now;
      fn(...args);
    } else {
      timer = setTimeout(() => {
        last = Date.now();
        fn(...args);
      }, remaining);
    }
  };
}

function showError(message) {
  el("banner").textContent = message;
}

function clearError() {
  el("banner").textContent = "";
}

/* ---------------------------------------------------------------- rendering */

function supports(...paths) {
  return paths.every((path) => state.supported.has(path));
}

function show(id, visible) {
  el(id)?.classList.toggle("hidden", !visible);
}

function value(path, key, fallback = null) {
  const entry = state.values[path];
  if (entry && typeof entry === "object" && entry[key] !== undefined) return entry[key];
  return fallback;
}

function renderRecord() {
  const available = supports("/transports/0/record");
  show("record", available);
  if (!available) return;
  const recording = Boolean(value("/transports/0/record", "recording", false));
  const button = el("record");
  button.classList.toggle("on", recording);
  button.textContent = recording ? "STOP" : "REC";
}

function markActive(selector, attribute, current) {
  document.querySelectorAll(selector).forEach((button) => {
    button.classList.toggle("on", String(current) === button.dataset[attribute]);
  });
}

function renderExposure() {
  const iso = value("/video/iso", "iso");
  if (iso !== null) el("iso-value").textContent = `${iso}`;
  markActive("[data-iso]", "iso", iso);
  show("row-iso", supports("/video/iso"));

  const shutter = state.values["/video/shutter"] || {};
  if (shutter.shutterSpeed) {
    el("shutter-value").textContent = `1/${shutter.shutterSpeed}`;
  } else if (shutter.shutterAngle) {
    el("shutter-value").textContent = `${shutter.shutterAngle / 100}°`;
  }
  markActive("[data-shutter]", "shutter", shutter.shutterSpeed || "");
  show("row-shutter", supports("/video/shutter"));

  const wb = value("/video/whiteBalance", "whiteBalance");
  if (wb !== null) {
    el("wb-value").textContent = `${wb}K`;
    if (!state.dragging.has("wb")) el("wb-slider").value = wb;
  }
  markActive("[data-wb]", "wb", wb);
  show("row-wb", supports("/video/whiteBalance"));

  const tint = value("/video/whiteBalanceTint", "whiteBalanceTint");
  if (tint !== null) {
    el("tint-value").textContent = tint > 0 ? `+${tint}` : `${tint}`;
    if (!state.dragging.has("tint")) el("tint-slider").value = tint;
  }
  show("row-tint", supports("/video/whiteBalanceTint"));

  const ae = state.values["/video/autoExposure"] || {};
  const mode = (ae.mode && ae.mode.mode) || ae.mode || "Off";
  const aeButton = el("ae-toggle");
  aeButton.classList.toggle("on", mode !== "Off");
  aeButton.textContent = mode === "Off" ? "Auto exposure: off" : `Auto exposure: ${mode}`;
  show("row-ae", supports("/video/autoExposure"));

  show("card-exposure", supports("/video/iso") || supports("/video/whiteBalance"));
}

function renderLens() {
  const iris = state.values["/lens/iris"] || {};
  if (iris.apertureStop) {
    el("iris-value").textContent = `f/${iris.apertureStop}`;
  } else if (iris.normalised !== undefined) {
    el("iris-value").textContent = `${Math.round(iris.normalised * 100)}%`;
  }
  if (iris.normalised !== undefined && !state.dragging.has("iris")) {
    el("iris-slider").value = iris.normalised;
  }
  show("row-iris", supports("/lens/iris"));

  const focus = value("/lens/focus", "focus");
  if (focus !== null) {
    el("focus-value").textContent = `${Math.round(focus * 100)}%`;
    if (!state.dragging.has("focus")) el("focus-slider").value = focus;
  }
  show("row-focus", supports("/lens/focus"));

  const zoom = state.values["/lens/zoom"] || {};
  if (zoom.focalLength) {
    el("zoom-value").textContent = `${zoom.focalLength}mm`;
  } else if (zoom.normalised !== undefined) {
    el("zoom-value").textContent = `${Math.round(zoom.normalised * 100)}%`;
  }
  if (zoom.normalised !== undefined && !state.dragging.has("zoom")) {
    el("zoom-slider").value = zoom.normalised;
  }
  show("row-zoom", supports("/lens/zoom"));

  show("card-lens", supports("/lens/iris") || supports("/lens/focus"));
}

function renderColor() {
  const saturation = value("/colorCorrection/color", "saturation");
  if (saturation !== null) {
    el("saturation-value").textContent = Number(saturation).toFixed(2);
    if (!state.dragging.has("saturation")) el("saturation-slider").value = saturation;
  }
  show("card-color", supports("/colorCorrection/color"));
}

function renderPresets() {
  const active = value("/presets/active", "preset");
  const names = (state.values["/presets"] || {}).presets || state.presets;
  const container = el("preset-chips");
  container.textContent = "";
  names.forEach((name) => {
    const button = document.createElement("button");
    button.textContent = name;
    button.classList.toggle("on", name === active);
    button.addEventListener("click", async () => {
      await send(`/deck/preset/${encodeURIComponent(name)}`);
    });
    container.append(button);
  });
  show("card-presets", supports("/presets/active") && names.length > 0);
}

function renderMedia() {
  const workingset = (state.values["/media/workingset"] || {}).workingset;
  const disk = Array.isArray(workingset)
    ? workingset.find((d) => d.activeDisk) || workingset[0]
    : null;
  if (disk) {
    const seconds = disk.remainingRecordTime;
    el("media-remaining").textContent =
      typeof seconds === "number"
        ? `${Math.floor(seconds / 3600)}h ${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}m`
        : "—";
    el("media-volume").textContent = disk.volume || "";
  }
  show("card-media", supports("/media/workingset") && Boolean(disk));
}

function renderStatusLine() {
  const parts = [];
  const iso = value("/video/iso", "iso");
  if (iso !== null) parts.push(`ISO ${iso}`);
  const shutter = state.values["/video/shutter"] || {};
  if (shutter.shutterSpeed) parts.push(`1/${shutter.shutterSpeed}`);
  else if (shutter.shutterAngle) parts.push(`${shutter.shutterAngle / 100}°`);
  const wb = value("/video/whiteBalance", "whiteBalance");
  if (wb !== null) parts.push(`${wb}K`);
  el("status-line").textContent = parts.join("   ") || "connected";
}

function renderAll() {
  renderRecord();
  renderExposure();
  renderLens();
  renderColor();
  renderPresets();
  renderMedia();
  renderStatusLine();
}

/* ------------------------------------------------------------------- wiring */

function buildChips() {
  const isoChips = el("iso-chips");
  isoChips.textContent = "";
  ISO_CHIPS.forEach((iso) => {
    const button = document.createElement("button");
    button.textContent = iso;
    button.dataset.iso = iso;
    button.addEventListener("click", () => send(`/deck/iso/${iso}`));
    isoChips.append(button);
  });

  const wbChips = el("wb-chips");
  wbChips.textContent = "";
  Object.entries(state.wbPresets)
    .sort((a, b) => a[1] - b[1])
    .forEach(([name, kelvin]) => {
      const button = document.createElement("button");
      button.textContent = `${kelvin}K`;
      button.title = name;
      button.dataset.wb = kelvin;
      button.addEventListener("click", () => send(`/deck/wb/${kelvin}`));
      wbChips.append(button);
    });
}

function wireControls() {
  document.querySelectorAll("[data-deck]").forEach((button) => {
    button.addEventListener("click", () => send(button.dataset.deck));
  });

  el("record").addEventListener("click", () => send("/deck/record/toggle"));
  el("ae-toggle").addEventListener("click", () => send("/deck/ae/toggle"));

  document.querySelectorAll("input[type=range][data-control]").forEach((slider) => {
    const control = slider.dataset.control;
    const push = throttle((v) => send(`/api/set/${control}?v=${v}`));

    const startDrag = () => state.dragging.add(control);
    const endDrag = () => {
      state.dragging.delete(control);
      send(`/api/set/${control}?v=${slider.value}`);
    };

    slider.addEventListener("pointerdown", startDrag);
    slider.addEventListener("pointerup", endDrag);
    slider.addEventListener("pointercancel", endDrag);
    slider.addEventListener("input", () => {
      state.dragging.add(control);
      push(slider.value);
    });
    slider.addEventListener("change", endDrag);
  });
}

/* -------------------------------------------------------------- connection */

async function loadState() {
  const response = await fetch("/api/state");
  const data = await response.json();

  state.supported = new Set(data.supported || []);
  state.values = data.state || {};
  state.presets = data.presets || [];
  state.wbPresets = data.wbPresets || {};

  el("dot").className = `dot ${data.connected ? "on" : "off"}`;
  el("camera-name").textContent = data.camera?.host || "Camera control";
  el("media-manager").href = data.camera?.mediaManager || "#";

  if (!data.connected) {
    showError(data.error || "Camera not connected. Retrying…");
    el("status-line").textContent = "not connected";
  } else {
    clearError();
  }

  const polled = state.supported.size - (data.pushed || []).length;
  el("capability-note").textContent =
    `${state.supported.size} endpoints available, ${polled} polled. `;

  return data.connected;
}

function openSocket() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/ws`);

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "snapshot") {
      state.supported = new Set(message.supported || []);
      state.values = message.state || {};
    } else if (message.type === "state") {
      state.values[message.property] = message.value;
    }
    renderAll();
  });

  socket.addEventListener("close", () => {
    el("dot").className = "dot off";
    setTimeout(openSocket, 2000);
  });
}

/** Re-read capabilities until the camera turns up, then stop asking. */
async function waitForCamera() {
  if (await loadState()) {
    buildChips();
    renderAll();
    return;
  }
  setTimeout(waitForCamera, 3000);
}

async function main() {
  await loadState();
  buildChips();
  wireControls();
  renderAll();
  openSocket();
  if (!state.supported.size) waitForCamera();
}

main();
