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

/* Every continuous value can be dragged or typed, and the two stay in step.
 *
 * `toField` converts what the API reports into what the field shows; `toApi`
 * converts back. They differ where the camera works in a unit nobody says out
 * loud -- focus and zoom are normalised 0-1 on the wire and percentages on
 * screen. `round` is the precision the field is worth showing.
 */
const CONTROLS = {
  wb: { round: 0 },
  tint: { round: 0 },
  focus: { round: 1, toField: (v) => v * 100, toApi: (v) => v / 100 },
  zoom: { round: 0, toField: (v) => v * 100, toApi: (v) => v / 100 },
  saturation: { round: 2 },
};

const identity = (v) => v;

function toField(control, apiValue) {
  const spec = CONTROLS[control];
  const converted = (spec.toField || identity)(Number(apiValue));
  return Number(converted.toFixed(spec.round));
}

function toApi(control, fieldValue) {
  return (CONTROLS[control].toApi || identity)(Number(fieldValue));
}

/** Is this control currently being dragged or typed into? */
function busy(control) {
  const field = el(`${control}-input`);
  return state.dragging.has(control) || (Boolean(field) && document.activeElement === field);
}

/** Show a value on both the field and the slider, without fighting the user. */
function showControl(control, apiValue) {
  if (apiValue === null || apiValue === undefined || Number.isNaN(Number(apiValue))) return;
  const field = el(`${control}-input`);
  const slider = el(`${control}-slider`);
  if (field && document.activeElement !== field) field.value = toField(control, apiValue);
  if (slider && !state.dragging.has(control)) slider.value = apiValue;
}

const el = (id) => document.getElementById(id);
const state = {
  supported: new Set(),
  values: {},
  presets: [],
  overlays: [],
  displays: [],
  schema: {},
  tab: "control",
  /** Aperture as an f-number plus the lens's range, both from the service. */
  iris: { fstop: null, range: null },
  /** Which monitoring output the overlay buttons act on. */
  display: null,
  wbPresets: {},
  /** Sliders the user is currently dragging, so pushes do not fight them. */
  dragging: new Set(),
};

/* ----------------------------------------------------------------- sending */

async function send(url, body) {
  try {
    const response = await fetch(
      url,
      body === undefined
        ? undefined
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }
    );
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
  if (!busy("wb")) showControl("wb", wb);
  markActive("[data-wb]", "wb", wb);
  show("row-wb-presets", supports("/video/whiteBalance"));
  show("row-wb", supports("/video/whiteBalance"));

  if (!busy("tint")) showControl("tint", value("/video/whiteBalanceTint", "whiteBalanceTint"));
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

  if (!busy("focus")) showControl("focus", value("/lens/focus", "focus"));
  show("row-focus", supports("/lens/focus"));

  const zoom = state.values["/lens/zoom"] || {};
  if (!busy("zoom")) showControl("zoom", zoom.normalised);
  // Focal length is what the lens is actually at; the field stays in percent
  // because not every lens reports millimetres.
  el("zoom-focal").textContent = zoom.focalLength ? `${zoom.focalLength}mm` : "";
  show("row-zoom", supports("/lens/zoom"));

  showCardIfAnyRowVisible("card-lens");
}

function renderColor() {
  if (!busy("saturation")) {
    showControl("saturation", value("/colorCorrection/color", "saturation"));
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
  // Name the output on the overlays themselves. The default is whichever output
  // the camera lists first, which is not necessarily the one being watched, and
  // an overlay applied to the wrong output looks like a button that does nothing.
  const heading = el("overlays-label");
  if (heading) {
    heading.textContent = state.display ? `Overlays — ${state.display}` : "Overlays";
  }
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
  if (state.tab === "status") renderStatus();
  if (state.tab === "configure") renderConfigure();
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

function selectTab(name) {
  state.tab = name;
  ["control", "status", "configure"].forEach((tab) => {
    show(`tab-${tab}`, tab === name);
  });
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.classList.toggle("on", button.dataset.tab === name);
  });
  renderAll();
}

function wireControls() {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
  });
  el("status-filter").addEventListener("input", renderStatus);
  el("config-filter").addEventListener("input", renderConfigure);

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
    const push = throttle((v) => send(`/api/set/${control}?v=${v}`));

    /* Keep the number field in step with the slider as it moves. Waiting for
     * the camera to answer means a poll interval of not knowing what is being
     * set, and on firmware that pushes nothing that is every value here. */
    const showLive = () => {
      const field = el(`${control}-input`);
      if (field) field.value = toField(control, slider.value);
    };

    slider.addEventListener("input", () => {
      state.dragging.add(control);
      showLive();
      push(slider.value);
    });

    /* Stay "dragging" until the final write lands, so a poll or push already in
     * flight cannot snap the value back. */
    const commit = async () => {
      if (!state.dragging.has(control)) return;
      showLive();
      await send(`/api/set/${control}?v=${slider.value}`);
      state.dragging.delete(control);
      await resync();
      renderAll();
    };

    slider.addEventListener("change", commit);
    /* On the window, not the slider: releasing outside the control -- easy when
     * nudging a small amount -- would otherwise leave it marked as dragging and
     * frozen until a page reload. */
    window.addEventListener("pointerup", commit);
    window.addEventListener("pointercancel", commit);
  });

  Object.keys(CONTROLS).forEach((control) => {
    const field = el(`${control}-input`);
    if (!field) return;

    /* Typed entry is the exact-value path: the slider cannot reach 5637K. The
     * camera still snaps to its own steps, so read back rather than trusting
     * what was typed. */
    const commit = async () => {
      if (field.value === "") {
        await resync();
        renderAll();
        return;
      }
      const wanted = toApi(control, field.value);
      if (!Number.isFinite(wanted)) {
        await resync();
        renderAll();
        return;
      }
      await send(`/api/set/${control}?v=${wanted}`);
      await resync();
      renderAll();
    };

    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        field.blur(); // commits through the blur handler
      }
    });
    field.addEventListener("change", commit);
    field.addEventListener("blur", commit);
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

/** The writable shape of every endpoint, as the camera describes it. */
async function loadSchema() {
  try {
    state.schema = await (await fetch("/api/schema")).json();
  } catch {
    state.schema = {};
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

/* ------------------------------------------------- status and configuration */

/** Group by the first path segment: /video/..., /lens/..., /monitoring/... */
function groupByPrefix(paths) {
  const groups = new Map();
  paths.forEach((path) => {
    const prefix = path.split("/")[1] || "other";
    if (!groups.has(prefix)) groups.set(prefix, []);
    groups.get(prefix).push(path);
  });
  return groups;
}

/** Render a camera value compactly: {"iso":400} reads as "iso 400". */
function readout(value) {
  if (value === undefined || value === null) return "\u2014";
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) {
    return value.length ? value.map(readout).join(", ") : "(empty)";
  }
  const parts = Object.entries(value).map(([key, item]) => {
    const rendered =
      item !== null && typeof item === "object" ? JSON.stringify(item) : String(item);
    return `${key} ${rendered}`;
  });
  return parts.join("   ") || "(no value)";
}

function card(title) {
  const section = document.createElement("section");
  section.className = "card";
  const heading = document.createElement("h2");
  heading.textContent = title;
  section.append(heading);
  return section;
}

function renderStatus() {
  const filter = el("status-filter").value.trim().toLowerCase();
  const paths = [...state.supported].filter((p) => p.toLowerCase().includes(filter)).sort();
  const container = el("status-groups");
  container.textContent = "";

  groupByPrefix(paths).forEach((group, prefix) => {
    const section = card(prefix);
    group.forEach((path) => {
      const entry = document.createElement("div");
      entry.className = "entry";

      const name = document.createElement("span");
      name.className = "path";
      name.textContent = path;

      const value = document.createElement("span");
      value.className = "readout";
      value.textContent = readout(state.values[path]);

      entry.append(name, value);
      if (state.schema[path]?.pushed) {
        const pill = document.createElement("span");
        pill.className = "pill";
        pill.textContent = "live";
        pill.title = "Pushed by the camera rather than polled";
        entry.append(pill);
      }
      section.append(entry);
    });
    container.append(section);
  });

  el("status-count").textContent = `${paths.length} of ${state.supported.size}`;
}

/** One input for one documented field, returning its current value on demand. */
function fieldInput(spec, current) {
  let input;
  if (spec.enum) {
    input = document.createElement("select");
    spec.enum.forEach((option) => {
      const item = document.createElement("option");
      item.value = String(option);
      item.textContent = option === "" ? "(none)" : String(option);
      input.append(item);
    });
    if (current !== undefined) input.value = String(current);
    input.read = () => input.value;
  } else if (spec.type === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(current);
    input.read = () => input.checked;
  } else if (spec.type === "number" || spec.type === "integer") {
    input = document.createElement("input");
    input.type = "number";
    input.step = spec.type === "integer" ? "1" : "any";
    if (spec.minimum !== undefined) input.min = spec.minimum;
    if (spec.maximum !== undefined) input.max = spec.maximum;
    if (current !== undefined) input.value = current;
    input.read = () => (input.value === "" ? undefined : Number(input.value));
  } else {
    input = document.createElement("input");
    input.type = "text";
    if (current !== undefined) input.value = current;
    input.read = () => (input.value === "" ? undefined : input.value);
  }
  if (spec.description) input.title = spec.description;
  return input;
}

function renderConfigure() {
  const filter = el("config-filter").value.trim().toLowerCase();
  const paths = Object.keys(state.schema)
    .filter((p) => state.schema[p].writable && p.toLowerCase().includes(filter))
    .sort();
  const container = el("config-groups");
  container.textContent = "";

  groupByPrefix(paths).forEach((group, prefix) => {
    const section = card(prefix);
    group.forEach((path) => {
      const spec = state.schema[path];
      const current = state.values[path];
      const entry = document.createElement("div");
      entry.className = "entry";

      const name = document.createElement("span");
      name.className = "path";
      name.textContent = path;
      if (spec.summary) name.title = spec.summary;

      const fields = document.createElement("span");
      fields.className = "fields";
      const inputs = new Map();

      Object.entries(spec.fields).forEach(([key, definition]) => {
        // A nested object is one level deep at most in this API.
        const nested = definition.properties;
        const source =
          current && typeof current === "object" ? current[key] : undefined;
        if (nested) {
          Object.entries(nested).forEach(([innerKey, innerSpec]) => {
            const label = document.createElement("label");
            label.textContent = `${key}.${innerKey}`;
            const input = fieldInput(
              innerSpec,
              source && typeof source === "object" ? source[innerKey] : undefined
            );
            inputs.set(`${key}.${innerKey}`, input);
            fields.append(label, input);
          });
          return;
        }
        if (Object.keys(spec.fields).length > 1 || key !== "") {
          const label = document.createElement("label");
          label.textContent = key || "value";
          fields.append(label);
        }
        const input = fieldInput(definition, source);
        inputs.set(key, input);
        fields.append(input);
      });

      const apply = document.createElement("button");
      apply.textContent = "Set";
      apply.addEventListener("click", async () => {
        const body = {};
        inputs.forEach((input, key) => {
          const value = input.read();
          if (value === undefined) return;
          if (key.includes(".")) {
            const [outer, inner] = key.split(".");
            body[outer] = body[outer] || {};
            body[outer][inner] = value;
          } else {
            body[key] = value;
          }
        });
        await send("/api/raw", { path, body });
        await resync();
        renderAll();
      });
      fields.append(apply);

      entry.append(name, fields);
      section.append(entry);
    });
    container.append(section);
  });

  el("config-count").textContent = `${paths.length} settable`;
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
    await loadSchema();
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
  await loadSchema();
  buildChips();
  wireControls();
  renderAll();
  openSocket();
  watchForStall();
  if (!state.supported.size) waitForCamera();
}

main();
