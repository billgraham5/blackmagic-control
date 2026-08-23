/* Web UI for the local camera control service.
 *
 * The service owns the camera connection and pushes state here over a
 * websocket, so this file only has to render and send. Controls the camera
 * does not implement are hidden rather than shown broken.
 */

const ISO_CHIPS = [200, 400, 800, 1600, 3200, 6400];
const FSTOP_CHIPS = [2.8, 4, 5.6, 8, 11];

/* How each slider renders its own value, so the number under your finger
 * updates as you drag instead of waiting for the camera to answer. */
/* Overlay endpoint names are camelCase; these are what a person calls them. */
const OVERLAY_NAMES = {
  zebra: "Zebra",
  falseColor: "False colour",
  focusAssist: "Focus assist",
  frameGuide: "Frame guide",
  frameGrids: "Grids",
  safeArea: "Safe area",
  cleanFeed: "Clean feed",
  displayLUT: "LUT",
  colorBars: "Colour bars",
};

const SLIDERS = {
  wb: { label: "wb-value", format: (v) => `${Math.round(v)}K` },
  tint: { label: "tint-value", format: (v) => (v > 0 ? `+${Math.round(v)}` : `${Math.round(v)}`) },
  focus: { label: "focus-value", format: (v) => `${Math.round(v * 100)}%` },
  zoom: { label: "zoom-value", format: (v) => `${Math.round(v * 100)}%` },
  saturation: { label: "saturation-value", format: (v) => Number(v).toFixed(2) },
};

const el = (id) => document.getElementById(id);
const state = {
  supported: new Set(),
  values: {},
  presets: [],
  overlays: [],
  displays: [],
  /** Aperture as an f-number plus the lens's range, both from the service. */
  iris: { fstop: null, range: null },
  /** Which monitoring output the overlay buttons act on. */
  display: null,
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

/** Hide a section only when every control inside it is unavailable. */
function showCardIfAnyRowVisible(cardId) {
  const card = el(cardId);
  if (!card) return;
  const rows = [...card.querySelectorAll(".row")];
  show(cardId, rows.some((row) => !row.classList.contains("hidden")));
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
  if (wb !== null && !state.dragging.has("wb")) {
    el("wb-value").textContent = `${wb}K`;
    el("wb-slider").value = wb;
  }
  markActive("[data-wb]", "wb", wb);
  show("row-wb-presets", supports("/video/whiteBalance"));
  show("row-wb", supports("/video/whiteBalance"));

  const tint = value("/video/whiteBalanceTint", "whiteBalanceTint");
  if (tint !== null && !state.dragging.has("tint")) {
    el("tint-value").textContent = tint > 0 ? `+${tint}` : `${tint}`;
    el("tint-slider").value = tint;
  }
  show("row-tint", supports("/video/whiteBalanceTint"));

  const ae = state.values["/video/autoExposure"] || {};
  const mode = (ae.mode && ae.mode.mode) || ae.mode || "Off";
  const aeButton = el("ae-toggle");
  aeButton.classList.toggle("on", mode !== "Off");
  aeButton.textContent = mode === "Off" ? "Auto exposure: off" : `Auto exposure: ${mode}`;
  show("row-ae", supports("/video/autoExposure"));

  showCardIfAnyRowVisible("card-exposure");
}

/** Round to one decimal, so f/2.8284 shows as the f/2.8 people expect. */
function tidyFstop(fnumber) {
  return Math.round(fnumber * 10) / 10;
}

function renderIris() {
  const field = el("iris-fstop");
  // Never overwrite a value being typed.
  if (document.activeElement === field) return;

  const fstop = state.iris.fstop;
  if (typeof fstop === "number") {
    field.value = tidyFstop(fstop);
  } else {
    field.value = "";
  }

  const range = state.iris.range;
  if (Array.isArray(range)) {
    field.min = tidyFstop(range[0]);
    field.max = tidyFstop(range[1]);
    el("iris-range").textContent = `f/${tidyFstop(range[0])}\u2013f/${tidyFstop(range[1])}`;
  } else {
    el("iris-range").textContent = "";
  }

  markActive("[data-fstop]", "fstop", field.value);
}

function renderLens() {
  renderIris();
  show("row-iris", supports("/lens/iris"));

  const focus = value("/lens/focus", "focus");
  if (focus !== null && !state.dragging.has("focus")) {
    el("focus-value").textContent = `${Math.round(focus * 100)}%`;
    el("focus-slider").value = focus;
  }
  show("row-focus", supports("/lens/focus"));

  const zoom = state.values["/lens/zoom"] || {};
  if (!state.dragging.has("zoom")) {
    if (zoom.focalLength) {
      el("zoom-value").textContent = `${zoom.focalLength}mm`;
    } else if (zoom.normalised !== undefined) {
      el("zoom-value").textContent = `${Math.round(zoom.normalised * 100)}%`;
    }
    if (zoom.normalised !== undefined) el("zoom-slider").value = zoom.normalised;
  }
  show("row-zoom", supports("/lens/zoom"));

  showCardIfAnyRowVisible("card-lens");
}

function renderColor() {
  const saturation = value("/colorCorrection/color", "saturation");
  if (saturation !== null && !state.dragging.has("saturation")) {
    el("saturation-value").textContent = Number(saturation).toFixed(2);
    el("saturation-slider").value = saturation;
  }
  show("card-color", supports("/colorCorrection/color"));
}

/* Overlays live under /monitoring/<display>/<name>, and which display and which
 * overlays exist varies by body and firmware, so the buttons are built from
 * whatever the probe found rather than hardcoded. */
function rememberedDisplay() {
  try {
    return localStorage.getItem("bmc.display");
  } catch {
    return null; // private windows and blocked site data both throw
  }
}

function rememberDisplay(name) {
  try {
    localStorage.setItem("bmc.display", name);
  } catch {
    /* nothing to do; the choice just will not survive a reload */
  }
}

function overlayButtons() {
  const seen = new Map();
  const display = state.display;
  state.overlays.forEach((path) => {
    if (path === "/camera/colorBars") {
      seen.set("colorBars", { path, url: "/deck/colorbars/toggle" });
      return;
    }
    const parts = path.split("/"); // ["", "monitoring", <display>, <name>]
    const name = parts.pop();
    if (!OVERLAY_NAMES[name] || seen.has(name)) return;
    // Only offer overlays for the output being controlled, so the button's lit
    // state and the endpoint it calls are always the same display.
    if (parts.length === 3 && parts[2] !== display) return;
    seen.set(name, {
      path,
      url: `/deck/monitor/${name}/toggle` + (display ? `?display=${encodeURIComponent(display)}` : ""),
    });
  });
  // Fixed order, so buttons do not shuffle between renders or cameras.
  const order = Object.keys(OVERLAY_NAMES);
  return new Map([...seen].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0])));
}

function renderDisplays() {
  const container = el("display-chips");
  show("row-display", state.displays.length > 1);
  if (state.displays.length <= 1) return;

  if (container.dataset.built !== state.displays.join(",")) {
    container.textContent = "";
    state.displays.forEach((name) => {
      const button = document.createElement("button");
      button.textContent = name;
      button.dataset.display = name;
      button.addEventListener("click", () => {
        state.display = name;
        rememberDisplay(name);
        el("monitor-toggles").dataset.built = ""; // force a rebuild
        renderAll();
      });
      container.append(button);
    });
    container.dataset.built = state.displays.join(",");
  }
  markActive("[data-display]", "display", state.display);
}

function renderMonitoring() {
  renderDisplays();
  const buttons = overlayButtons();
  const container = el("monitor-toggles");

  const signature = `${state.display}:${[...buttons.keys()].join(",")}`;
  if (container.dataset.built !== signature) {
    container.textContent = "";
    buttons.forEach((entry, name) => {
      const button = document.createElement("button");
      button.textContent = OVERLAY_NAMES[name];
      button.dataset.overlay = name;
      button.addEventListener("click", () => send(entry.url));
      container.append(button);
    });
    container.dataset.built = signature;
  }

  buttons.forEach((entry, name) => {
    const button = container.querySelector(`[data-overlay="${name}"]`);
    if (button) button.classList.toggle("on", Boolean(value(entry.path, "enabled", false)));
  });

  const tally = state.values["/camera/tallyStatus"];
  const hasTally = supports("/camera/tallyStatus") && tally;
  show("row-tally", Boolean(hasTally));
  if (hasTally) {
    el("tally-value").textContent =
      tally.tally ?? tally.status ?? (tally.program ? "PROGRAM" : tally.preview ? "PREVIEW" : "off");
  }

  show("card-monitoring", buttons.size > 0 || Boolean(hasTally));
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
  renderMonitoring();
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

  const irisChips = el("iris-chips");
  irisChips.textContent = "";
  FSTOP_CHIPS.forEach((fstop) => {
    const button = document.createElement("button");
    button.textContent = `f/${fstop}`;
    button.dataset.fstop = fstop;
    button.addEventListener("click", () => setFstop(fstop));
    irisChips.append(button);
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

/** Send a typed aperture and show back whatever the lens actually took. */
async function setFstop(fnumber) {
  const value = Number(fnumber);
  if (!Number.isFinite(value) || value <= 0) {
    await refreshIris();
    renderIris();
    return;
  }
  await send(`/api/set/fstop?v=${value}`);
  // The lens snaps to its own steps, so the number typed is rarely the number
  // set. Read back rather than leave a value the camera never took on screen.
  await refreshIris();
  renderIris();
}

function wireControls() {
  const iris = el("iris-fstop");
  iris.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      iris.blur(); // commits via the blur handler below
    }
  });
  iris.addEventListener("change", () => setFstop(iris.value));
  iris.addEventListener("blur", () => setFstop(iris.value));

  document.querySelectorAll("[data-deck]").forEach((button) => {
    button.addEventListener("click", () => send(button.dataset.deck));
  });

  el("record").addEventListener("click", () => send("/deck/record/toggle"));
  el("ae-toggle").addEventListener("click", () => send("/deck/ae/toggle"));

  document.querySelectorAll("input[type=range][data-control]").forEach((slider) => {
    const control = slider.dataset.control;
    const spec = SLIDERS[control];
    const push = throttle((v) => send(`/api/set/${control}?v=${v}`));

    /* Show the value under your finger straight away. Waiting for the camera
     * to answer means a poll interval of not knowing what you are setting --
     * and on firmware that pushes nothing, that is every value on the page. */
    const showLive = () => {
      if (spec) el(spec.label).textContent = spec.format(Number(slider.value));
    };

    slider.addEventListener("input", () => {
      state.dragging.add(control);
      showLive();
      push(slider.value);
    });

    /* Stay "dragging" until the final write lands, so a poll or push that is
     * already in flight cannot snap the label back to the old value. */
    const commit = async () => {
      if (!state.dragging.has(control)) return;
      showLive();
      await send(`/api/set/${control}?v=${slider.value}`);
      state.dragging.delete(control);
      /* The camera snaps to its own legal values, so a fine adjustment often
       * lands somewhere other than where you left the slider. Re-read rather
       * than assume, otherwise the page shows a value the camera never took. */
      await resync();
      renderAll();
    };

    slider.addEventListener("change", commit);
    slider.addEventListener("blur", commit);
    /* On the window, not the slider: releasing outside the control -- easy to
     * do when nudging a small amount -- would otherwise leave the control
     * marked as dragging forever, freezing its display until a page reload. */
    window.addEventListener("pointerup", commit);
    window.addEventListener("pointercancel", commit);
  });
}

/** The f-number conversion lives on the service, so ask it rather than guess. */
async function refreshIris() {
  try {
    const data = await (await fetch("/api/state")).json();
    state.iris = data.iris || state.iris;
  } catch {
    /* keep what we have */
  }
}

/** Pull current state from the service, replacing what we hold. */
async function resync() {
  try {
    const response = await fetch("/api/state");
    const data = await response.json();
    state.values = data.state || {};
    state.supported = new Set(data.supported || []);
    state.iris = data.iris || state.iris;
  } catch {
    /* leave what we have; the websocket will correct us */
  }
}

/* -------------------------------------------------------------- connection */

async function loadState() {
  const response = await fetch("/api/state");
  const data = await response.json();

  state.supported = new Set(data.supported || []);
  state.values = data.state || {};
  state.presets = data.presets || [];
  state.overlays = data.overlays || [];
  state.displays = data.displays || [];
  state.iris = data.iris || { fstop: null, range: null };
  if (!state.displays.includes(state.display)) {
    state.display = rememberedDisplay() || state.displays[0] || null;
  }
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

/** When the last websocket message arrived, used to notice a silent stall. */
let lastMessageAt = Date.now();

function openSocket() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/ws`);

  socket.addEventListener("open", () => {
    lastMessageAt = Date.now();
  });

  socket.addEventListener("message", (event) => {
    lastMessageAt = Date.now();
    const message = JSON.parse(event.data);
    if (message.type === "snapshot") {
      state.supported = new Set(message.supported || []);
      state.values = message.state || {};
    } else if (message.type === "state") {
      state.values[message.property] = message.value;
    }
    if (message.type === "snapshot" || message.property === "/lens/iris") {
      refreshIris();
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

/* Last line of defence. If updates stop arriving for any reason the page must
 * not sit there showing stale values while looking perfectly healthy -- that is
 * indistinguishable from the camera not responding. */
function watchForStall() {
  setInterval(async () => {
    if (Date.now() - lastMessageAt < 10000) return;
    await resync();
    renderAll();
    lastMessageAt = Date.now();
  }, 5000);
}

async function main() {
  await loadState();
  buildChips();
  wireControls();
  renderAll();
  openSocket();
  watchForStall();
  if (!state.supported.size) waitForCamera();
}

main();
