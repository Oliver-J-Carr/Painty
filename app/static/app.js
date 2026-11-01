// Painty front end: one WebSocket in, everything else is drawing what it says.
// The server owns all state. The browser only renders events and sends commands.

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const SVG_NS = "http://www.w3.org/2000/svg";
// Pause between shapes appearing, so you can watch the layers go down.
const SHAPE_GAP_MS = 140;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

let ws = null;
let state = "connecting";
let shapesOnCanvas = 0;
let savedId = null;

// ------------------------------------------------------------------ status line
const PHASES = {
  thinking: "Thinking…",
  paint: "Painting…",
  look: "Looking at the canvas…",
  finish: "Finishing…",
};
const STATES = {
  connecting: "Connecting…",
  idle: "Ready. Describe a picture to paint.",
  painting: "Thinking…",
  paused: "Paused. Out of budget.",
  finished: "Finished.",
  stopped: "Stopped.",
  error: "The drawing failed.",
};

function setStatus(text, dataState = state) {
  $("status").textContent = text;
  $("status").dataset.state = dataState;
}

// ------------------------------------------------------------------ canvas
const layer = $("paint-layer");
const shapeQueue = [];
let pumping = false;

function parseFragment(svg) {
  const doc = new DOMParser().parseFromString(`<svg xmlns="${SVG_NS}">${svg}</svg>`, "image/svg+xml");
  return document.importNode(doc.documentElement.firstElementChild, true);
}

function queueShapes(shapes) {
  for (const s of shapes) shapeQueue.push(s);
  if (!pumping) pump();
}

function pump() {
  const next = shapeQueue.shift();
  if (!next) { pumping = false; return; }
  pumping = true;
  layer.appendChild(parseFragment(next.svg));
  if (reducedMotion) { pump(); return; }
  setTimeout(pump, SHAPE_GAP_MS);
}

function clearCanvas() {
  shapeQueue.length = 0;
  layer.replaceChildren();
}

// ------------------------------------------------------------------ meters
function renderBudget(b) {
  shapesOnCanvas = b.shapes_used;
  $("shapes-count").textContent = `${b.shapes_used} of ${b.shapes_total}`;
  $("shapes-fill").style.width = b.shapes_total ? `${(100 * b.shapes_used) / b.shapes_total}%` : "0";
  $("looks-meter").hidden = b.looks_total === 0;
  $("looks-count").textContent = `${b.looks_used} of ${b.looks_total}`;
  const eyes = $("looks-eyes");
  eyes.replaceChildren();
  // Dots only while they fit on a line; the count says the rest.
  if (b.looks_total <= 12) {
    for (let i = 0; i < b.looks_total; i++) eyes.appendChild(el("span", i < b.looks_used ? "used" : ""));
  }
}

function previewBudget(settings) {
  renderBudget({
    shapes_used: 0, shapes_total: settings.shape_budget,
    looks_used: 0, looks_total: settings.vision ? settings.look_budget : 0,
  });
}

// ------------------------------------------------------------------ activity feed
const feedList = $("feed-list");
const entries = new Map(); // tool call id -> <li>

// Follow new activity unless the reader has scrolled up to look at something.
let followFeed = true;
feedList.addEventListener("scroll", () => {
  followFeed = feedList.scrollHeight - feedList.scrollTop - feedList.clientHeight < 40;
});
// Instant, not smooth: a smooth scroll is cut short by the next entry arriving.
function followFeedEnd() {
  if (followFeed) feedList.scrollTop = feedList.scrollHeight;
}

function addEntry({ kind, text, detail, status, tool, key }) {
  const li = el("li", "entry");
  li.dataset.kind = kind || "agent";
  li.dataset.status = status || "done";
  if (tool) li.dataset.tool = tool;
  li.appendChild(el("p", "entry-text", text || ""));
  if (detail) li.appendChild(el("p", "entry-detail", detail));
  feedList.appendChild(li);
  $("feed-empty").hidden = true;
  if (key) entries.set(key, li);
  followFeedEnd();
  return li;
}

function onActivity(e) {
  if (e.kind !== "tool") { addEntry(e); return; }
  const li = entries.get(e.key);
  if (e.status === "running" || !li) { addEntry(e); return; }
  li.dataset.status = e.status;
  if (e.result) li.appendChild(el("p", "entry-result", e.result));
  followFeedEnd();
}

function onLook(e) {
  const li = entries.get(e.key) || addEntry({ kind: "tool", tool: "look", text: "👀 Looking at the canvas" });
  const btn = el("button", "entry-look");
  btn.type = "button";
  btn.setAttribute("aria-label", `Enlarge look ${e.n}`);
  const img = el("img");
  img.src = e.image;
  img.alt = `What Painty saw on look ${e.n}`;
  img.addEventListener("load", followFeedEnd);
  btn.appendChild(img);
  btn.addEventListener("click", () => openLightbox(e.image, `Look ${e.n}: ${e.focus || ""}`));
  li.appendChild(btn);
  followFeedEnd();
}

function clearFeed() {
  feedList.replaceChildren();
  followFeed = true;
  entries.clear();
  $("feed-empty").hidden = false;
}

// ------------------------------------------------------------------ states
function show(id, on) { $(id).hidden = !on; }

// "12,345 in (82% cached), 3,456 out (2,100 reasoning)". Older gallery
// paintings have no cached or reasoning counts, so those parts are left out.
function tokenText({ input = 0, cached, output = 0, reasoning }) {
  const share = cached !== undefined && input ? ` (${Math.round((cached / input) * 100)}% cached)` : "";
  const thinking = reasoning ? ` (${reasoning.toLocaleString()} reasoning)` : "";
  return `${input.toLocaleString()} in${share}, ${output.toLocaleString()} out${thinking}`;
}

function applyState(e) {
  state = e.state;
  const painting = state === "painting";
  setStatus(STATES[state] || state);

  $("prompt").disabled = painting;
  $("paint").hidden = painting;
  $("paint").disabled = painting;
  $("stop").hidden = !painting;
  lockSettings(painting);

  show("paused", state === "paused");
  show("finished", state === "finished");
  show("halted", state === "stopped" || state === "error");

  if (state === "paused") {
    const t = e.top_up || {};
    const looks = t.looks ? ` and ${t.looks} more looks` : "";
    $("paused-text").textContent = `${e.reason} Give Painty ${t.shapes} more shapes${looks}?`;
  }
  if (state === "finished") {
    savedId = e.saved_id || null;
    $("finished-title").textContent = e.title || "Untitled";
    $("finished-note").textContent = e.note || "";
    const t = e.tokens || {};
    const tokens = t.input || t.output ? `Tokens: ${tokenText(t)}. ` : "";
    $("finished-meta").textContent = `${shapesOnCanvas} shapes. ${tokens}${savedId ? "Saved to the gallery." : "Not saved."}`;
    $("view-saved").hidden = !savedId;
  }
  if (state === "stopped" || state === "error") {
    $("halted-text").textContent = e.reason || STATES[state];
    $("save-halted").hidden = shapesOnCanvas === 0;
  }
}

function onCanvasReset(e) {
  clearCanvas();
  clearFeed();
  savedId = null;
  $("prompt-shown").textContent = `“${e.prompt}”`;
  $("prompt-shown").hidden = false;
}

// ------------------------------------------------------------------ socket
function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.addEventListener("message", (msg) => handle(JSON.parse(msg.data)));
  ws.addEventListener("close", () => {
    state = "connecting";
    setStatus("Disconnected. Reconnecting…", "connecting");
    $("prompt").disabled = true;
    $("paint").disabled = true;
    $("stop").hidden = true;
    $("paint").hidden = false;
    setTimeout(connect, 2000);
  });
}

function handle(e) {
  switch (e.type) {
    case "hello":
      // A fresh connection is a fresh studio (e.g. after a server restart).
      clearCanvas();
      clearFeed();
      $("prompt-shown").hidden = true;
      setupSettings(e.options, e.settings);
      setCanvasSize(e.canvas_size);
      previewBudget(e.settings);
      applyState({ state: "idle" });
      $("prompt").disabled = false;
      $("paint").disabled = false;
      break;
    case "status": applyState(e); break;
    case "phase": if (state === "painting") setStatus(PHASES[e.phase] || "Painting…", "painting"); break;
    case "canvas_reset": onCanvasReset(e); break;
    case "shapes": queueShapes(e.shapes); break;
    case "look": onLook(e); break;
    case "budget": renderBudget(e); break;
    case "activity": onActivity(e); break;
    case "settings": fillSettings(e.settings); if (state !== "painting") previewBudget(e.settings); break;
    case "error": toast(e.text, "error"); break;
  }
}

function setCanvasSize(size) {
  $("canvas").setAttribute("viewBox", `0 0 ${size} ${size}`);
  const bg = $("canvas").querySelector("rect");
  bg.setAttribute("width", size);
  bg.setAttribute("height", size);
}

// ------------------------------------------------------------------ composer + buttons
$("composer").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const prompt = $("prompt").value.trim();
  if (!prompt || state === "painting") return;
  if (state === "paused" && !confirm("Start a new drawing? The paused one hasn't been saved.")) return;
  send({ type: "paint", prompt });
  $("prompt").value = "";
});
$("stop").addEventListener("click", () => send({ type: "stop" }));
$("keep-going").addEventListener("click", () => send({ type: "keep_going" }));
$("finish-here").addEventListener("click", () => send({ type: "save" }));
$("save-halted").addEventListener("click", () => send({ type: "save" }));
$("view-saved").addEventListener("click", () => openGallery(savedId));

// ------------------------------------------------------------------ settings
const form = $("settings-form");

function setupSettings(options, values) {
  const model = $("set-model");
  model.replaceChildren(...options.models.map((m) => new Option(m, m)));
  const effort = $("set-effort");
  effort.replaceChildren(...options.reasoning_efforts.map((v) => new Option(v ? v[0].toUpperCase() + v.slice(1) : "Model default", v)));
  for (const [name, [lo, hi]] of Object.entries(options.limits)) {
    const input = form.elements[name];
    if (input) { input.min = lo; input.max = hi; }
  }
  fillSettings(values);
}

function fillSettings(values) {
  for (const [name, value] of Object.entries(values)) {
    const input = form.elements[name];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  }
}

function readSettings() {
  const out = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    out[input.name] = input.type === "checkbox" ? input.checked
      : input.type === "number" ? Number(input.value) : input.value;
  }
  return out;
}

function lockSettings(locked) {
  form.setAttribute("aria-disabled", String(locked));
  for (const input of form.elements) input.disabled = locked;
  $("settings-help").textContent = locked
    ? "Locked while Painty is painting."
    : "Changes save as you make them and apply to the next drawing. Keep going uses the top-up amounts set when you click it.";
}

form.addEventListener("change", () => send({ type: "settings", settings: readSettings() }));

function toggleSettings(open) {
  $("settings").hidden = !open;
  $("open-settings").setAttribute("aria-expanded", String(open));
  if (open) form.elements[0]?.focus();
}
$("open-settings").addEventListener("click", () => toggleSettings($("settings").hidden));
$("close-settings").addEventListener("click", () => toggleSettings(false));

// ------------------------------------------------------------------ gallery
async function openGallery(focusId = null) {
  $("gallery").hidden = false;
  $("gallery-detail").hidden = true;
  $("gallery-grid").hidden = false;
  $("gallery-body").scrollTop = 0;
  let items = [];
  try {
    items = await (await fetch("/api/gallery")).json();
  } catch {
    toast("Couldn't load the gallery.", "error");
  }
  renderGallery(items);
  const focus = focusId && items.find((m) => m.id === focusId);
  if (focus) showDetail(focus);
  else $("close-gallery").focus();
}

const PROMPT_PREVIEW_CHARS = 80;
function preview(text) {
  return text.length > PROMPT_PREVIEW_CHARS ? `${text.slice(0, PROMPT_PREVIEW_CHARS).trimEnd()}…` : text;
}

function summary(m) {
  const s = m.settings || {};
  return `${s.model || "?"}, eyes ${s.vision ? "on" : "off"}, ${m.shapes_used} shapes`;
}

function renderGallery(items) {
  const grid = $("gallery-grid");
  grid.replaceChildren();
  $("gallery-empty").hidden = items.length > 0;
  for (const m of items) {
    const card = el("button", "gallery-card");
    card.type = "button";
    const img = el("img");
    img.src = `/gallery/${encodeURIComponent(m.id)}/painting.png`;
    img.alt = m.title;
    img.loading = "lazy";
    const text = el("div", "gallery-text");
    text.append(el("h3", "", m.title), el("p", "gallery-prompt", preview(m.prompt)), el("p", "gallery-meta", summary(m)));
    card.append(img, text);
    card.addEventListener("click", () => showDetail(m));
    grid.appendChild(card);
  }
}

let gridScroll = 0; // where the grid was scrolled to, restored by "Back to all paintings"

function showDetail(m) {
  const base = `/gallery/${encodeURIComponent(m.id)}`;
  const s = m.settings || {};
  const d = $("gallery-detail");
  d.replaceChildren();

  const left = el("div");
  const big = el("img", "big");
  big.src = `${base}/painting.png`;
  big.alt = m.title;
  left.appendChild(big);

  const right = el("div");
  const back = el("button", "ghost-dark back", "Back to all paintings");
  back.type = "button";
  back.addEventListener("click", () => {
    d.hidden = true;
    $("gallery-grid").hidden = false;
    $("gallery-body").scrollTop = gridScroll;
  });
  right.append(back, el("h3", "", m.title), el("p", "painting-note", m.note || ""));

  const endings = { finished: "Painty finished it", ended: "Painty stopped without finishing", saved: "You saved it" };
  const rows = [
    ["Prompt", m.prompt],
    ["Model", `${s.model} (effort: ${s.reasoning_effort || "default"})`],
    ["Vision", s.vision ? "On" : "Off"],
    ["Shapes", `${m.shapes_used} of ${m.shapes_budget}`],
    ["Looks", `${m.looks_used} of ${m.looks_budget}`],
    ["Top-ups", String(m.top_ups)],
    ["Tokens", tokenText({ input: m.input_tokens, cached: m.cached_tokens,
                           output: m.output_tokens, reasoning: m.reasoning_tokens })],
    ["Time", m.duration_s ? `${Math.round(m.duration_s)} s` : "?"],
    ["Ending", endings[m.ending] || m.ending],
    ["Painted", new Date(m.created_at * 1000).toLocaleString("en-GB")],
  ];
  const dl = el("dl");
  for (const [k, v] of rows) dl.append(el("dt", "", k), el("dd", "", v));
  right.appendChild(dl);

  const files = el("p", "files");
  const fileLinks = [["PNG", "painting.png"], ["SVG", "painting.svg"],
    ...(m.shape_format === "notation" ? [["Shapes", "shapes.txt"]] : []),
    ["Shapes (JSON)", "shapes.json"], ["Details", "meta.json"]];
  for (const [label, file] of fileLinks) {
    const a = el("a", "", label);
    a.href = `${base}/${file}`;
    a.target = "_blank";
    a.rel = "noopener";
    files.appendChild(a);
  }
  right.appendChild(files);

  if (m.looks_used) {
    right.appendChild(el("p", "", "What Painty saw"));
    const looks = el("div", "looks");
    for (let i = 1; i <= m.looks_used; i++) {
      const img = el("img");
      img.src = `${base}/looks/look-${i}.png`;
      img.alt = `Look ${i}`;
      img.loading = "lazy";
      img.addEventListener("click", () => openLightbox(img.src, `Look ${i}`));
      looks.appendChild(img);
    }
    right.appendChild(looks);
  }

  d.append(left, right);
  gridScroll = $("gallery-body").scrollTop;
  $("gallery-grid").hidden = true;
  d.hidden = false;
  $("gallery-body").scrollTop = 0;
  back.focus();
}

$("open-gallery").addEventListener("click", () => openGallery());
$("close-gallery").addEventListener("click", () => { $("gallery").hidden = true; });

// ------------------------------------------------------------------ lightbox, toast, keys
function openLightbox(src, caption) {
  $("lightbox-img").src = src;
  $("lightbox-img").alt = caption;
  $("lightbox-caption").textContent = caption;
  $("lightbox").hidden = false;
}
$("lightbox").addEventListener("click", () => { $("lightbox").hidden = true; });
$("gallery").addEventListener("click", (ev) => { if (ev.target === $("gallery")) $("gallery").hidden = true; });

let toastTimer = null;
function toast(text, kind = "info") {
  const t = $("toast");
  t.textContent = text;
  t.dataset.kind = kind;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
}

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (!$("lightbox").hidden) $("lightbox").hidden = true;
  else if (!$("gallery").hidden) $("gallery").hidden = true;
  else if (!$("settings").hidden) toggleSettings(false);
});

connect();
