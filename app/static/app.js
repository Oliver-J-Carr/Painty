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
// The short line in the top bar: what Painty is doing while it paints, or that the
// connection dropped. Empty otherwise; the feed and the result cards say the rest.
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

function setStatus(text) {
  $("status").textContent = text || "";
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

// The canvas shapes the server offers, {square: [512, 512], ...}, from hello.
let canvases = { square: [512, 512] };

function setCanvasSize(width, height) {
  $("canvas").setAttribute("viewBox", `0 0 ${width} ${height}`);
  const bg = $("canvas").querySelector("rect");
  bg.setAttribute("width", width);
  bg.setAttribute("height", height);
  document.querySelector(".canvas-wrap").style.setProperty("--ratio", width / height);
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

// A picture in the feed (a look, or the reference image): a thumbnail that enlarges on click.
function feedImage(src, alt, caption) {
  const btn = el("button", "entry-look");
  btn.type = "button";
  btn.setAttribute("aria-label", `Enlarge: ${alt}`);
  const img = el("img");
  img.src = src;
  img.alt = alt;
  img.addEventListener("load", followFeedEnd);
  btn.appendChild(img);
  btn.addEventListener("click", () => openLightbox(src, caption));
  return btn;
}

function addEntry({ kind, text, detail, status, tool, key, image }) {
  const li = el("li", "entry");
  li.dataset.kind = kind || "agent";
  li.dataset.status = status || "done";
  if (tool) li.dataset.tool = tool;
  li.appendChild(el("p", "entry-text", text || ""));
  if (detail) li.appendChild(el("p", "entry-detail", detail));
  if (image) li.appendChild(feedImage(image, text || "Picture", text || ""));
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
  li.appendChild(feedImage(e.image, `What Painty saw on look ${e.n}`, `Look ${e.n}: ${e.focus || ""}`));
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
  setStatus(painting ? PHASES.thinking : "");

  $("new-painting").hidden = painting;
  $("new-painting").disabled = painting;
  $("stop").hidden = !painting;
  if (painting && $("new-dialog").open) $("new-dialog").close();
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
  setCanvasSize(e.width, e.height);
  $("prompt-text").textContent = e.prompt ? `“${e.prompt}”` : "Recreating the reference image";
  $("ref-shown").hidden = !e.reference;
  shownReference = e.reference || null;
  if (e.reference) $("ref-shown-img").src = e.reference;
  $("prompt-shown").hidden = false;
}

let shownReference = null; // the current drawing's reference image (a data URL), if any
$("ref-shown").addEventListener("click", () => {
  if (shownReference) openLightbox(shownReference, "The reference image");
});

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
    setStatus("Disconnected. Reconnecting…");
    $("new-painting").disabled = true;
    $("new-painting").hidden = false;
    $("stop").hidden = true;
    updatePaintButton();
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
      canvases = e.canvases;
      for (const [name, [w, h]] of Object.entries(canvases)) {
        const size = document.querySelector(`[data-size="${name}"]`);
        if (size) size.textContent = `${w} × ${h}`;
      }
      setCanvasSize(...canvases.square);
      previewBudget(e.settings);
      applyState({ state: "idle" });
      $("new-painting").disabled = false;
      updatePaintButton();
      break;
    case "status": applyState(e); break;
    case "phase": if (state === "painting") setStatus(PHASES[e.phase] || "Painting…"); break;
    case "canvas_reset": onCanvasReset(e); break;
    case "shapes": queueShapes(e.shapes); break;
    case "look": onLook(e); break;
    case "budget": renderBudget(e); break;
    case "activity": onActivity(e); break;
    case "settings": fillSettings(e.settings); if (state !== "painting") previewBudget(e.settings); break;
    case "error": toast(e.text, "error"); break;
  }
}

// ------------------------------------------------------------------ new painting dialog
// The canvas choice is kept between drawings (until the page reloads); the prompt and
// reference image are cleared once a painting starts.
const dialog = $("new-dialog");
const newForm = $("new-form");
const PROMPT_HINT = $("prompt").placeholder;
const PROMPT_HINT_WITH_REFERENCE = "Leave empty to recreate the image, or say how to use it: "
  + "“my dog as a cartoon”, “a seascape in these colours”";

function updatePaintButton() {
  const length = $("prompt").value.length;
  $("prompt-count").textContent = String(length);
  const asked = $("prompt").value.trim() || refUrl;
  $("paint").disabled = !asked || state === "connecting" || state === "painting";
}

function openNewPainting() {
  if (state === "painting" || state === "connecting") return;
  updatePaintButton();
  dialog.showModal();
  $("prompt").focus();
}

// ---- the reference image
// The browser picks the canvas shape nearest the image's own, crops the middle of the image
// to it and scales it to the canvas size, so Painty's reference and canvas match pixel for
// pixel. Changing the canvas shape afterwards re-crops the original.
const MAX_REFERENCE_FILE = 25 * 1024 * 1024;
let refSource = null; // the decoded original (an ImageBitmap)
let refUrl = null;    // the cropped image sent with paint (a JPEG data URL)

function nearestShape(width, height) {
  let best = "square";
  let gap = Infinity;
  for (const [name, [w, h]] of Object.entries(canvases)) {
    const d = Math.abs(Math.log(width / height) - Math.log(w / h));
    if (d < gap) { best = name; gap = d; }
  }
  return best;
}

function cropReference() {
  const shape = newForm.elements.canvas.value;
  const [w, h] = canvases[shape] || canvases.square;
  const src = refSource;
  const scale = Math.max(w / src.width, h / src.height);
  const sw = w / scale;
  const sh = h / scale;
  const out = document.createElement("canvas");
  out.width = w;
  out.height = h;
  const g = out.getContext("2d");
  g.fillStyle = "#ffffff"; // transparent parts are laid on white, like the canvas
  g.fillRect(0, 0, w, h);
  g.imageSmoothingQuality = "high";
  g.drawImage(src, (src.width - sw) / 2, (src.height - sh) / 2, sw, sh, 0, 0, w, h);
  refUrl = out.toDataURL("image/jpeg", 0.92);
  $("ref-img").src = refUrl;
  const cropped = sw * sh < 0.99 * src.width * src.height;
  $("ref-text").textContent = `${cropped ? "Cropped to" : "Fits"} the ${shape} canvas, ${w} × ${h}.`;
}

function refError(text) {
  $("ref-error").textContent = text || "";
  $("ref-error").hidden = !text;
}

async function setReference(file) {
  refError("");
  if (!file || !file.type.startsWith("image/")) { refError("That isn't an image."); return; }
  if (file.size > MAX_REFERENCE_FILE) { refError("That image is too big: 25 MB at most."); return; }
  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    refError("That image couldn't be read. Try a PNG, JPEG or WebP.");
    return;
  }
  refSource?.close();
  refSource = bitmap;
  newForm.elements.canvas.value = nearestShape(bitmap.width, bitmap.height);
  cropReference();
  $("ref-empty").hidden = true;
  $("ref-chosen").hidden = false;
  $("prompt").placeholder = PROMPT_HINT_WITH_REFERENCE;
  updatePaintButton();
}

function clearReference() {
  refSource?.close();
  refSource = null;
  refUrl = null;
  $("ref-img").removeAttribute("src");
  $("ref-file").value = "";
  $("ref-chosen").hidden = true;
  $("ref-empty").hidden = false;
  $("prompt").placeholder = PROMPT_HINT;
  refError("");
  updatePaintButton();
}

$("ref-pick").addEventListener("click", () => $("ref-file").click());
$("ref-file").addEventListener("change", () => setReference($("ref-file").files[0]));
$("ref-remove").addEventListener("click", () => { clearReference(); $("ref-pick").focus(); });
for (const radio of newForm.elements.canvas) {
  radio.addEventListener("change", () => { if (refSource) cropReference(); });
}
// Drop an image anywhere on the dialog, or paste one while it's open.
newForm.addEventListener("dragover", (ev) => {
  if (![...ev.dataTransfer.types].includes("Files")) return;
  ev.preventDefault();
  $("ref-slot").classList.add("dragging");
});
newForm.addEventListener("dragleave", (ev) => {
  if (!newForm.contains(ev.relatedTarget)) $("ref-slot").classList.remove("dragging");
});
newForm.addEventListener("drop", (ev) => {
  $("ref-slot").classList.remove("dragging");
  const file = ev.dataTransfer.files[0];
  if (!file) return;
  ev.preventDefault();
  setReference(file);
});
dialog.addEventListener("paste", (ev) => {
  const item = [...(ev.clipboardData?.items || [])].find((i) => i.kind === "file" && i.type.startsWith("image/"));
  if (!item) return; // plain text pastes into the prompt as usual
  ev.preventDefault();
  setReference(item.getAsFile());
});

$("new-painting").addEventListener("click", openNewPainting);
$("close-new").addEventListener("click", () => dialog.close());
$("cancel-new").addEventListener("click", () => dialog.close());
// A click on the backdrop lands on the dialog itself (the form fills the box).
dialog.addEventListener("click", (ev) => { if (ev.target === dialog) dialog.close(); });
$("prompt").addEventListener("input", updatePaintButton);
$("prompt").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    newForm.requestSubmit();
  }
});

newForm.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const prompt = $("prompt").value.trim();
  if ((!prompt && !refUrl) || state === "painting" || state === "connecting") return;
  if (state === "paused" && !confirm("Start a new drawing? The paused one hasn't been saved.")) return;
  // The dialog's settings go first (a number typed just before Enter may not have sent its
  // change yet). The socket keeps order, so the drawing starts with exactly what's shown.
  send({ type: "settings", settings: readSettings() });
  send({ type: "paint", prompt, canvas: newForm.elements.canvas.value, ...(refUrl ? { reference: refUrl } : {}) });
  $("prompt").value = "";
  clearReference();
  dialog.close();
});

// ------------------------------------------------------------------ buttons
$("stop").addEventListener("click", () => send({ type: "stop" }));
$("keep-going").addEventListener("click", () => send({ type: "keep_going" }));
$("finish-here").addEventListener("click", () => send({ type: "save" }));
$("save-halted").addEventListener("click", () => send({ type: "save" }));
$("view-saved").addEventListener("click", () => openGallery(savedId));

// ------------------------------------------------------------------ settings
// The settings live in two places: the painter and budget in the New painting dialog, and
// the keep going top-ups in the settings drawer. Each input is marked data-setting="name".
// Every change is saved on the server straight away, so they carry over to the next painting.
const form = $("settings-form");
const settingInputs = () => document.querySelectorAll("[data-setting]");
const settingInput = (name) => document.querySelector(`[data-setting="${name}"]`);
let settingsLocked = false;

function setupSettings(options, values) {
  const model = $("set-model");
  model.replaceChildren(...options.models.map((m) => new Option(m, m)));
  const effort = $("set-effort");
  effort.replaceChildren(...options.reasoning_efforts.map((v) => new Option(v ? v[0].toUpperCase() + v.slice(1) : "Model default", v)));
  for (const [name, [lo, hi]] of Object.entries(options.limits)) {
    const input = settingInput(name);
    if (input) { input.min = lo; input.max = hi; }
  }
  fillSettings(values);
}

function fillSettings(values) {
  for (const [name, value] of Object.entries(values)) {
    const input = settingInput(name);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  }
  syncLooksInput();
}

function readSettings() {
  const out = {};
  for (const input of settingInputs()) {
    out[input.dataset.setting] = input.type === "checkbox" ? input.checked
      : input.type === "number" ? Number(input.value) : input.value;
  }
  return out;
}

// Looks only matter when Painty can see the canvas.
function syncLooksInput() {
  settingInput("look_budget").disabled = settingsLocked || !settingInput("vision").checked;
}

function lockSettings(locked) {
  settingsLocked = locked;
  form.setAttribute("aria-disabled", String(locked));
  for (const input of settingInputs()) input.disabled = locked;
  syncLooksInput();
  $("settings-help").textContent = locked
    ? "Locked while Painty is painting."
    : "Keep going adds these to a paused drawing, read when you click it. "
      + "The model, vision and budget for each painting are in the New painting dialog.";
}

document.addEventListener("change", (ev) => {
  if (!ev.target.dataset?.setting) return;
  if (ev.target.dataset.setting === "vision") syncLooksInput();
  send({ type: "settings", settings: readSettings() });
});

function toggleSettings(open) {
  $("settings").hidden = !open;
  $("open-settings").setAttribute("aria-expanded", String(open));
  if (open) form.querySelector("input")?.focus();
}
$("open-settings").addEventListener("click", () => toggleSettings($("settings").hidden));
$("close-settings").addEventListener("click", () => toggleSettings(false));

// ------------------------------------------------------------------ gallery
async function openGallery(focusId = null) {
  $("gallery").hidden = false;
  $("gallery-detail").hidden = true;
  $("gallery-back").hidden = true;
  stepStage = null;
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

// A painting's canvas from its meta. Paintings from before canvas shapes are 512 square.
function canvasOf(m) {
  return m.canvas || { shape: "square", width: 512, height: 512 };
}

// A reference with no prompt means "recreate it".
function promptOf(m) {
  return m.prompt || (m.reference ? "Recreate the reference image" : "");
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
    const art = el("span", "card-art");
    const img = el("img", "card-painting");
    img.src = `/gallery/${encodeURIComponent(m.id)}/painting.png`;
    img.alt = m.title;
    img.loading = "lazy";
    art.appendChild(img);
    if (m.reference) { // the reference, small in the corner, so the pair reads at a glance
      const ref = el("img", "card-ref");
      ref.src = `/gallery/${encodeURIComponent(m.id)}/reference.png`;
      ref.alt = "Reference image";
      ref.loading = "lazy";
      art.appendChild(ref);
    }
    const text = el("div", "gallery-text");
    text.append(el("h3", "", m.title), el("p", "gallery-prompt", preview(promptOf(m))), el("p", "gallery-meta", summary(m)));
    card.append(art, text);
    card.addEventListener("click", () => showDetail(m));
    grid.appendChild(card);
  }
}

let gridScroll = 0; // where the grid was scrolled to, restored by "Back to all paintings"
let stepStage = null; // (+1 or -1) => void while a detail with stages is open; arrow keys use it

const EYE_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5 1.5 12 1.5 12Z" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round"/><circle cx="12" cy="12" r="3.6" fill="currentColor"/></svg>';

function eyeBadge() {
  const badge = el("span", "eye-badge");
  badge.innerHTML = EYE_SVG;
  return badge;
}

function shapeSpan(st) {
  if (st.last === 0) return "no shapes yet";
  return st.first === st.last ? `shape ${st.first}` : `shapes ${st.first}–${st.last}`;
}

// Stands in for a look in the stage viewer's picture switch: show the reference image.
const REFERENCE = { reference: true };

// The mini gallery: a big preview with a strip of every stage (one per paint call) under it.
// Stages Painty looked at are marked with an eye, and can swap to exactly what Painty saw.
// A painting made from a reference can also swap to the reference, for comparison.
function stageViewer(m, base) {
  const stages = m.stages;
  const looks = new Map((m.looks || []).map((l) => [l.n, l]));
  const total = stages.filter((st) => st.n > 0).length;
  const final = stages[stages.length - 1];
  const label = (st) => (st === final ? "Final" : st.n === 0 ? "Blank" : String(st.n));
  const src = (st) => (st === final ? `${base}/painting.png` : `${base}/stages/stage-${st.n}.png`);

  const wrap = el("div", "stage-viewer");
  const c = canvasOf(m);
  wrap.style.setProperty("--ratio", c.width / c.height);
  const big = el("button", "stage-preview");
  big.type = "button";
  big.title = "Enlarge, with Painty's notes";
  const bigImg = el("img", "big");
  big.appendChild(bigImg);
  const strip = el("div", "stage-strip");
  // One short line under the strip, the same height on every stage, so nothing jumps.
  // The full story (Painty's note, what it looked for) is in the enlarged view.
  const caption = el("p", "stage-caption");
  const views = el("div", "stage-views");
  views.setAttribute("role", "group");
  views.setAttribute("aria-label", "Which picture to show");
  const infoRow = el("div", "stage-info-row");
  infoRow.append(caption, views);
  const frame = el("div", "stage-frame"); // the space the preview fills
  frame.appendChild(big);
  wrap.append(frame, strip, infoRow);

  let current = stages.length - 1;
  let shownLook = null; // the look on show in the preview, or null for the painting

  const stageName = (st) => (st === final ? `Final (stage ${st.n} of ${total})`
    : st.n === 0 ? "Blank canvas" : `Stage ${st.n} of ${total}`);
  const seenAt = (st) => st.looks.map((n) => looks.get(n) || { n, focus: "" });

  // Everything about the stage, for under the enlarged picture.
  function details(st) {
    const out = [];
    if (st.n > 0 && st.note) out.push(el("p", "lightbox-note", `“${st.note}”`));
    const seen = seenAt(st);
    for (const l of seen) {
      const line = el("p", "lightbox-look");
      if (shownLook && l.n === shownLook.n) line.classList.add("current");
      const who = seen.length > 1 ? `Look ${l.n}: Painty looked here` : "Painty looked here";
      line.append(eyeBadge(), el("span", "", `${who}${l.focus ? `, checking “${l.focus}”` : ""}`));
      out.push(line);
    }
    if (!seen.length && st.n > 0) out.push(el("p", "lightbox-muted", "Painty didn’t look at this stage."));
    return out;
  }

  function show(look) {
    const st = stages[current];
    shownLook = look;
    for (const b of views.children) b.setAttribute("aria-pressed", String(b.look === look));
    if (look === REFERENCE) {
      bigImg.src = `${base}/reference.png`;
      bigImg.alt = "The reference image";
    } else if (look) {
      bigImg.src = `${base}/looks/look-${look.n}.png`;
      bigImg.alt = `What Painty saw on look ${look.n}`;
    } else {
      bigImg.src = src(st);
      bigImg.alt = `${m.title}, ${stageName(st).toLowerCase()}`;
    }
  }

  function select(i) {
    current = Math.max(0, Math.min(stages.length - 1, i));
    const st = stages[current];
    for (const [j, b] of [...strip.children].entries()) b.setAttribute("aria-pressed", String(j === current));
    strip.children[current].scrollIntoView({ block: "nearest", inline: "nearest" });

    const seen = seenAt(st);
    caption.replaceChildren(...(seen.length ? [eyeBadge()] : []),
      el("span", "", `${stageName(st)} · ${shapeSpan(st)}`));
    views.replaceChildren();
    const options = [["Painting", null],
      ...seen.map((l) => [seen.length === 1 ? "What Painty saw" : `Look ${l.n}`, l]),
      ...(m.reference ? [["Reference", REFERENCE]] : [])];
    if (options.length > 1) {
      for (const [text, look] of options) {
        const b = el("button", "stage-view", text);
        b.type = "button";
        b.look = look;
        b.addEventListener("click", () => show(look));
        views.appendChild(b);
      }
    }
    show(null);
  }

  stages.forEach((st, i) => {
    const b = el("button", "stage-thumb");
    b.type = "button";
    const looked = st.looks.length > 0;
    if (looked) b.classList.add("looked");
    const name = st === final ? "Final stage" : st.n === 0 ? "Blank canvas" : `Stage ${st.n} of ${total}`;
    b.setAttribute("aria-label", `${name}, ${shapeSpan(st)}${looked ? ", Painty looked at this" : ""}`);
    const img = el("img");
    img.src = `${base}/stages/stage-${st.n}.png`;
    img.alt = "";
    img.loading = "lazy";
    b.append(img, el("span", "stage-label", label(st)));
    if (looked) b.appendChild(eyeBadge());
    b.addEventListener("click", () => select(i));
    strip.appendChild(b);
  });

  big.setAttribute("aria-label", "Enlarge, with Painty's notes");
  big.addEventListener("click", () => {
    const st = stages[current];
    if (shownLook === REFERENCE) {
      openLightbox(bigImg.src, "The reference image");
      return;
    }
    const heading = shownLook ? `What Painty saw on look ${shownLook.n} · ${stageName(st)}`
      : `${stageName(st)} · ${shapeSpan(st)}`;
    openLightbox(bigImg.src, heading, details(st));
  });
  stepStage = (delta) => select(current + delta);
  select(current);
  return wrap;
}

function showDetail(m) {
  stepStage = null;
  const base = `/gallery/${encodeURIComponent(m.id)}`;
  const s = m.settings || {};
  const d = $("gallery-detail");
  d.replaceChildren();
  d.classList.toggle("has-stages", Boolean(m.stages?.length));

  const left = m.stages?.length ? stageViewer(m, base) : el("div");
  if (!m.stages?.length) { // older paintings: no stage snapshots
    const big = el("img", "big");
    big.src = `${base}/painting.png`;
    big.alt = m.title;
    left.appendChild(big);
  }

  const right = el("div");
  right.append(el("h3", "", m.title), el("p", "painting-note", m.note || ""));

  const endings = { finished: "Painty finished it", ended: "Painty stopped without finishing", saved: "You saved it" };
  const c = canvasOf(m);
  const rows = [
    ["Prompt", promptOf(m)],
    ["Canvas", `${c.shape[0].toUpperCase()}${c.shape.slice(1)}, ${c.width} × ${c.height}`],
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
    ...(m.reference ? [["Reference", "reference.png"], ["Reference as Painty saw it", "reference-view.png"]] : []),
    ["Shapes (JSON)", "shapes.json"], ["Details", "meta.json"]];
  for (const [label, file] of fileLinks) {
    const a = el("a", "", label);
    a.href = `${base}/${file}`;
    a.target = "_blank";
    a.rel = "noopener";
    files.appendChild(a);
  }
  right.appendChild(files);

  if (m.looks_used && !m.stages?.length) { // newer paintings show their looks in the stage strip
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
  $("gallery-back").hidden = false;
  $("gallery-back").focus();
}

// From a painting back to the grid, where it was scrolled to.
function backToGrid() {
  stepStage = null;
  $("gallery-detail").hidden = true;
  $("gallery-back").hidden = true;
  $("gallery-grid").hidden = false;
  $("gallery-body").scrollTop = gridScroll;
  $("close-gallery").focus();
}

$("open-gallery").addEventListener("click", () => openGallery());
$("gallery-back").addEventListener("click", backToGrid);
$("close-gallery").addEventListener("click", () => { $("gallery").hidden = true; });

// ------------------------------------------------------------------ lightbox, toast, keys
// extra: more lines (nodes) under the caption, such as a stage's notes.
function openLightbox(src, caption, extra = []) {
  $("lightbox-img").src = src;
  $("lightbox-img").alt = caption;
  $("lightbox-caption").replaceChildren(el("p", "lightbox-heading", caption), ...extra);
  $("lightbox").hidden = false;
}
// The figure is sized from the picture's own shape, so tall and wide pictures both fit.
$("lightbox-img").addEventListener("load", (ev) => {
  const img = ev.target;
  if (img.naturalHeight) img.parentElement.style.setProperty("--ratio", img.naturalWidth / img.naturalHeight);
});
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
  // Left and right step through a painting's stages in the gallery.
  if ((ev.key === "ArrowLeft" || ev.key === "ArrowRight") && stepStage && $("lightbox").hidden
      && !$("gallery").hidden && !$("gallery-detail").hidden) {
    ev.preventDefault();
    stepStage(ev.key === "ArrowLeft" ? -1 : 1);
    return;
  }
  if (ev.key !== "Escape" || dialog.open) return; // the dialog closes itself on Escape
  // Escape steps back one level: lightbox, then painting, then the gallery itself.
  if (!$("lightbox").hidden) $("lightbox").hidden = true;
  else if (!$("gallery").hidden && !$("gallery-detail").hidden) backToGrid();
  else if (!$("gallery").hidden) $("gallery").hidden = true;
  else if (!$("settings").hidden) toggleSettings(false);
});

connect();
