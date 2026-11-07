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
// The current drawing as it started (from canvas_reset), for Paint again on the finished card.
let lastRun = null;

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

// The queue holds shapes ({svg} from the server, or {node} from a replay) and, for a
// replay, steps to run in between ({run}, such as a feed entry). Each item waits its
// own delay (SHAPE_GAP_MS unless it says otherwise) before the next one.
function queueShapes(shapes) {
  for (const s of shapes) shapeQueue.push(s);
  if (!pumping) pump();
}

function showItem(item) {
  if (item.svg) layer.appendChild(parseFragment(item.svg));
  if (item.node) layer.appendChild(document.importNode(item.node, true));
  if (item.run) item.run();
}

function pump() {
  const next = shapeQueue.shift();
  if (!next) { pumping = false; return; }
  pumping = true;
  showItem(next);
  if (reducedMotion) { pump(); return; }
  setTimeout(pump, next.delay ?? SHAPE_GAP_MS);
}

// Show everything still queued at once (a replay's Skip to end).
function flushQueue() {
  while (shapeQueue.length) showItem(shapeQueue.shift());
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
  if (detail && tool === "plan") { // the whole plan, folded away until opened
    const plan = el("details", "entry-plan");
    plan.append(el("summary", "", "Show the plan"), el("p", "", detail));
    li.appendChild(plan);
  } else if (detail) li.appendChild(el("p", "entry-detail", detail));
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

// Dollars, with enough places to show small amounts: $1.24, $0.042, $0.0037.
// ---- benchmark scores
const pct = (v) => `${Math.round(100 * v)}%`;
const offsetText = (o) => (o ? `${o[0] >= 0 ? "+" : ""}${o[0]}, ${o[1] >= 0 ? "+" : ""}${o[1]} px` : "none");
const cardTitle = (name) => benchmarks.find((b) => b.name === name)?.title || name;

// "Benchmark score 72%", then the best and weakest items.
function scoreLine(s, card) {
  const items = [...(s.items || [])].sort((a, b) => b.score - a.score);
  const parts = [`${cardTitle(card || s.card)}`];
  if (items.length > 1) {
    parts.push(`best: ${items[0].name} ${pct(items[0].score)}`,
      `weakest: ${items.at(-1).name} ${pct(items.at(-1).score)}`);
  }
  return [el("strong", "", `Benchmark score ${pct(s.score)}`), ` · ${parts.join(" · ")}`];
}

function usd(n) {
  if (n >= 1) return `$${n.toFixed(2)}`;
  if (n === 0) return "$0.00";
  return n < 0.0001 ? "under $0.0001" : `$${n.toPrecision(2)}`;
}

// An estimated cost from the server ({usd, rates, var}), for a details row: the amount, with
// the rates it used on hover, or which .env variable to set when the model has no price.
function costInfo(c) {
  if (!c) return "?";
  if (c.usd === null || c.usd === undefined) {
    return ["No price set. Add ", el("code", "", c.var), " to .env"];
  }
  const span = el("span", "", `≈ ${usd(c.usd)}`);
  if (c.rates) {
    const [pin, pcached, pout, pwrite] = c.rates;
    const write = pwrite !== undefined && pwrite !== pin ? `, $${pwrite} cache write` : "";
    span.title = `At $${pin} in, $${pcached} cached${write}, $${pout} out per million tokens`;
  }
  return span;
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

  if (state === "paused") showTopUp(e);
  if (state === "finished") {
    const score = $("finished-score");
    score.hidden = !e.benchmark_score;
    score.replaceChildren(...(e.benchmark_score ? scoreLine(e.benchmark_score, e.meta?.benchmark) : []));
    savedId = e.saved_id || null;
    $("finished-title").textContent = e.title || "Untitled";
    $("finished-note").textContent = e.note || "";
    const t = e.tokens || {};
    const tokens = t.input || t.output ? `Tokens: ${tokenText(t)}. ` : "";
    const cost = typeof e.cost?.usd === "number" ? `Cost ≈ ${usd(e.cost.usd)}. ` : "";
    $("finished-meta").textContent = `${shapesOnCanvas} shapes. ${tokens}${cost}${savedId ? "Saved to the gallery." : "Not saved."}`;
    $("view-saved").hidden = !savedId;
    if (lastRun) lastRun.title = e.title || "Untitled";
  }
  if (state === "stopped" || state === "error") {
    $("halted-text").textContent = e.reason || STATES[state];
    $("save-halted").hidden = shapesOnCanvas === 0;
  }
}

function onCanvasReset(e) {
  endReplay();
  clearCanvas();
  clearFeed();
  savedId = null;
  setCanvasSize(e.width, e.height);
  showPrompt(e.prompt ? `“${e.prompt}”` : "Recreating the reference image", e.reference);
  lastRun = { prompt: e.prompt, reference: e.reference, benchmark: e.benchmark, shape: e.shape,
              settings: e.settings, title: null };
}

let shownReference = null; // the current drawing's reference image (a data URL), if any
$("ref-shown").addEventListener("click", () => {
  if (shownReference) openLightbox(shownReference, "The reference image");
});

// The paused card asks how many shapes and looks to add, filled in with a suggestion for
// whatever ran out (0 for the rest). A benchmark's budget is fixed: it can only finish here.
function showTopUp(e) {
  const t = e.top_up;
  $("keep-going").hidden = !t;
  $("top-up-fields").hidden = !t;
  $("paused-text").textContent = t ? `${e.reason} How much more should Painty have?`
    : `${e.reason} A benchmark's budget is fixed, so finish here to save it for scoring.`;
  if (!t) return;
  $("top-up-shapes").value = t.shapes;
  $("top-up-looks-field").hidden = t.looks === null; // painting blind: no looks to add
  $("top-up-looks").value = t.looks ?? 0;
  if ($("gallery").hidden && !dialog.open) { // ready to type over, unless something's on top
    const first = t.shapes || t.looks === null ? $("top-up-shapes") : $("top-up-looks");
    first.focus({ preventScroll: true });
    first.select();
  }
}

function setTopUpLimits(limits) {
  for (const [kind, [lo, hi]] of Object.entries(limits || {})) {
    const input = $(`top-up-${kind}`);
    if (input) { input.min = lo; input.max = hi; }
  }
}

// ------------------------------------------------------------------ replay
// A gallery painting painted again on the studio canvas, shape by shape, with its
// stages, looks and plans in the feed at the points they happened. All in the browser:
// painting.svg holds every shape's own fragment, exactly as it was painted, so the
// replay matches the original even if the renderer has changed since.
const STAGE_GAP_MS = 700; // before each batch, as if Painty were thinking
const LOOK_GAP_MS = 900;  // after each look, to take it in
let replaying = null;     // the painting being replayed (its meta), while its card shows

async function replay(m) {
  if (state === "painting" || state === "paused") {
    toast("Painty is busy. Replay a painting once this one has finished or been stopped.");
    return;
  }
  if (!$("halted").hidden && !$("save-halted").hidden) {
    toast("This drawing isn't saved yet. Save it to the gallery first, or it will be lost from view.");
    return;
  }
  const base = `/gallery/${encodeURIComponent(m.id)}`;
  let shapes, plans = [];
  try {
    const svg = await (await fetch(`${base}/painting.svg`)).text();
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
    shapes = [...doc.documentElement.children].filter((n) => n.matches("g[data-n]"));
    if (m.plans?.length) plans = await planTexts(`${base}/plan.txt`);
  } catch {
    toast("This painting couldn't be loaded for a replay.", "error");
    return;
  }
  if (state === "painting" || state === "paused") return; // a painting started while it loaded

  $("gallery").hidden = true;
  const c = canvasOf(m);
  clearCanvas();
  clearFeed();
  setCanvasSize(c.width, c.height);
  showPrompt(promptOf(m) ? `“${promptOf(m)}”` : "Recreating the reference image",
             m.reference ? `${base}/reference.png` : null);
  for (const id of ["paused", "finished", "halted"]) show(id, false);
  replaying = m;
  $("replay-title").textContent = m.title || "Untitled";
  $("replay-meta").textContent = `Replaying ${m.shapes_used} shapes…`;
  $("replay-skip").hidden = false;
  $("replay-again").hidden = true;
  show("replay", true);
  setStatus("Replaying…");

  let painted = 0, looked = 0;
  const meter = () => renderBudget({ shapes_used: painted, shapes_total: m.shapes_budget,
                                     looks_used: looked, looks_total: m.looks_budget || 0 });
  meter();
  const s = m.settings || {};
  addEntry({ text: `▶️ Replaying “${m.title || "Untitled"}”`,
             detail: `${s.model || "?"}, vision ${s.vision ? "on" : "off"}, ${c.shape} ${c.width} × ${c.height}` });

  const steps = [];
  const planStep = (i) => steps.push({ delay: STAGE_GAP_MS, run: () => addEntry(
    { kind: "tool", tool: "plan", text: "📝 Writing a plan", detail: plans[i] || "" }) });
  const lookStep = (i) => steps.push({ delay: LOOK_GAP_MS, run: () => {
    looked += 1;
    meter();
    const focus = m.looks?.[i - 1]?.focus || "";
    addEntry({ kind: "tool", tool: "look", text: `👀 Looking at the canvas${focus ? `: ${focus}` : ""}`,
               image: `${base}/looks/look-${i}.png` });
  } });
  const shapeSteps = (from, to) => {
    for (const node of shapes.slice(from - 1, to)) steps.push({ node, run: () => { painted += 1; meter(); } });
  };
  const plansAfter = (n) => (m.plans || []).forEach((p, i) => { if (p.shapes === n) planStep(i); });

  plansAfter(0);
  if (m.stages?.length) {
    for (const st of m.stages) {
      if (st.n > 0) {
        const count = st.last - st.first + 1;
        steps.push({ delay: STAGE_GAP_MS, run: () => addEntry({ kind: "tool", tool: "paint",
          text: `🖌️ Painting ${count} shape${count === 1 ? "" : "s"}${st.note ? `: ${st.note}` : ""}` }) });
        shapeSteps(st.first, st.last);
      }
      for (const i of st.looks || []) lookStep(i);
      if (st.n > 0) plansAfter(st.last);
    }
  } else {
    shapeSteps(1, shapes.length); // older paintings have no stages: just the shapes
  }
  steps.push({ run: () => {
    painted = shapes.length;
    meter();
    addEntry({ kind: "tool", tool: "finish", text: `🏁 “${m.title || "Untitled"}”`, detail: m.note || "" });
    $("replay-meta").textContent = `Replayed ${shapes.length} shapes.`;
    $("replay-skip").hidden = true;
    $("replay-again").hidden = false;
    setStatus("");
  } });
  queueShapes(steps);
}

// plan.txt holds every plan in order, each headed "Plan N, written ...".
async function planTexts(url) {
  const res = await fetch(url);
  if (!res.ok) return [];
  return (await res.text()).split(/^(?=Plan \d+, written )/m)
    .map((part) => part.replace(/^Plan \d+, written [^\n]*\n+/, "").trim()).filter(Boolean);
}

function endReplay() {
  if (!replaying) return;
  replaying = null;
  show("replay", false);
}

function showPrompt(text, reference) {
  $("prompt-text").textContent = text;
  $("ref-shown").hidden = !reference;
  shownReference = reference || null;
  if (reference) $("ref-shown-img").src = reference;
  $("prompt-shown").hidden = false;
}

$("replay-skip").addEventListener("click", flushQueue);
$("replay-again").addEventListener("click", () => { if (replaying) replay(replaying); });
$("replay-gallery").addEventListener("click", () => { if (replaying) openGallery(replaying.id); });

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
      savedSettings = e.settings;
      setupSettings(e.options, e.settings);
      setTopUpLimits(e.options.top_up_limits);
      configuredModels = e.options.models;
      canvases = e.canvases;
      setupBenchmarks(e.benchmarks || [], e.benchmark_prompt || "");
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
    case "settings":
      savedSettings = e.settings;
      fillSettings(e.settings);
      // Preview the next painting's budget only when no drawing is on the go: a paused,
      // stopped or failed one keeps its own meters (and its shape count, for saving).
      if (state === "idle" || state === "finished") previewBudget(e.settings);
      break;
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
  const card = benchmarks.find((c) => c.name === benchmark);
  if (card) $("ref-text").textContent = `Benchmark: ${card.title} (${card.shapes} shapes). ${card.about}`;
}

function refError(text) {
  $("ref-error").textContent = text || "";
  $("ref-error").hidden = !text;
}

async function setReference(file) {
  dropBenchmark(true);
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
  dropBenchmark(true);
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
  // A benchmark card re-cropped to another shape is just an image, so it stops being the card.
  radio.addEventListener("change", () => { dropBenchmark(false); if (refSource) cropReference(); });
}

// ---- benchmark cards
// A card is a reference image painted by the server in Painty's own notation, to be
// reproduced and then scored (python -m app.benchmark_score). Picking one loads it into
// the reference slot (which sets the canvas shape) and fills in the standard prompt. The
// server paints the card again itself, so Painty gets its exact pixels, not this JPEG.
let benchmarks = [];       // the cards, from hello
let benchmarkPrompt = "";  // the standard prompt, from hello
let benchmark = null;      // the chosen card's name, while the reference is that card

function setupBenchmarks(cards, prompt) {
  benchmarks = cards;
  benchmarkPrompt = prompt;
  const pick = $("benchmark-pick");
  pick.replaceChildren(pick.options[0]);
  for (const c of cards) {
    const option = el("option", "", `${c.title} (${c.shape}, ${c.shapes} shapes)`);
    option.value = c.name;
    pick.appendChild(option);
  }
  pick.hidden = !cards.length;
}

// Forget the chosen card. clearPrompt: also clear the prompt if it's still the standard one.
function dropBenchmark(clearPrompt) {
  if (benchmark && clearPrompt && $("prompt").value === benchmarkPrompt) $("prompt").value = "";
  benchmark = null;
  releaseBudget();
}

async function pickBenchmark(name) {
  const card = benchmarks.find((c) => c.name === name);
  if (!card) return;
  let blob;
  try {
    blob = await (await fetch(`/benchmark/${encodeURIComponent(name)}.png`)).blob();
  } catch {
    refError("That benchmark card couldn't be loaded. Try again.");
    return;
  }
  await setReference(blob);
  if (!refSource) return;
  benchmark = name;
  holdBudget(card);
  cropReference(); // shows the card's name in place of the crop note
  if (!$("prompt").value.trim()) $("prompt").value = benchmarkPrompt;
  updatePaintButton();
}

$("benchmark-pick").addEventListener("change", async (ev) => {
  const name = ev.target.value;
  ev.target.value = "";
  if (name) await pickBenchmark(name);
});
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
  send({ type: "paint", prompt, canvas: newForm.elements.canvas.value,
         ...(benchmark ? { benchmark } : refUrl ? { reference: refUrl } : {}) });
  $("prompt").value = "";
  clearReference();
  endRerun(); // its settings were just sent, so they stay
  dialog.close();
  $("gallery").hidden = true; // Paint again from a gallery painting: show the new one painting
});

// ---- paint again
// Paint again (on the finished card and on a gallery painting) opens this dialog filled in with
// an earlier painting's prompt, reference image, canvas and settings, to keep or change. They're
// only a suggestion until Paint is pressed: closing the dialog instead clears the prompt and image
// and puts the saved settings back.
let savedSettings = null; // the settings as saved on the server, from hello and settings
let rerunning = false;    // the dialog holds an earlier painting's settings, not saved yet

async function paintAgain({ prompt, reference, benchmark: card, shape, settings, title }) {
  if (state === "painting" || state === "connecting") {
    toast("Painty is busy. Wait for this painting to end, or stop it, then try again.");
    return;
  }
  // Older paintings predate the grid modes (they had the ruler frame), the exemplar and planning.
  const wanted = { grid: "frame", exemplar: false, planning: false, ...(settings || {}) };
  const gone = wanted.model && !configuredModels.includes(wanted.model) ? wanted.model : null;
  const values = {};
  for (const input of dialog.querySelectorAll("[data-setting]")) {
    const name = input.dataset.setting;
    if (name in wanted && !(name === "model" && gone)) values[name] = wanted[name];
  }
  if (card) for (const n of CARD_BUDGET) delete values[n]; // the card sets these; keep your own aside

  clearReference();
  if (card && benchmarks.some((c) => c.name === card)) {
    await pickBenchmark(card);
  } else if (reference) { // a card no longer offered is re-run as a plain reference image
    try {
      await setReference(await (await fetch(reference)).blob());
    } catch {
      refError("The reference image couldn't be loaded. Add it again, or paint without it.");
    }
  }
  if (state === "painting" || state === "connecting") return; // changed while the image loaded
  newForm.elements.canvas.value = canvases[shape] ? shape : "square";
  if (refSource) cropReference(); // the saved reference is already this shape, so nothing is lost
  $("prompt").value = prompt || "";
  fillSettings(values);
  rerunning = true;
  const note = $("rerun-note");
  note.textContent = `Filled in from “${title || "Untitled"}”. Change anything you like, then paint.`
    + (gone ? ` ${gone} isn't offered any more, so it's set to ${$("set-model").value}.` : "");
  note.hidden = false;
  openNewPainting();
}

function endRerun() {
  rerunning = false;
  $("rerun-note").hidden = true;
}

dialog.addEventListener("close", () => {
  if (!rerunning) return;
  endRerun();
  $("prompt").value = "";
  clearReference();
  if (savedSettings) fillSettings(savedSettings);
});

// ------------------------------------------------------------------ buttons
$("stop").addEventListener("click", () => send({ type: "stop" }));
$("paused").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const looks = $("top-up-looks-field").hidden ? 0 : Number($("top-up-looks").value) || 0;
  send({ type: "keep_going", shapes: Number($("top-up-shapes").value) || 0, looks });
});
$("finish-here").addEventListener("click", () => send({ type: "save" }));
$("save-halted").addEventListener("click", () => send({ type: "save" }));
$("view-saved").addEventListener("click", () => openGallery(savedId));
$("paint-again").addEventListener("click", () => { if (lastRun) paintAgain(lastRun); });

// ------------------------------------------------------------------ settings
// The settings (painter and budget) live in the New painting dialog. Each input is marked
// data-setting="name". Every change is saved on the server straight away, so they carry
// over to the next painting. Keep going top-ups aren't settings: they're typed when paused.
const settingInputs = () => document.querySelectorAll("[data-setting]");
const settingInput = (name) => document.querySelector(`[data-setting="${name}"]`);
let settingsLocked = false;
// The grid modes, as named in the New painting dialog and the gallery.
const GRID_NAMES = { frame: "Ruler frame", overlay: "Grid on the image", none: "None" };

function setupSettings(options, values) {
  const model = $("set-model");
  model.replaceChildren(...options.models.map((m) => new Option(m, m)));
  const effort = $("set-effort");
  const effortName = (v) => (v ? v[0].toUpperCase() + v.slice(1) : "Model default");
  effort.replaceChildren(...options.reasoning_efforts.map((v) => new Option(effortName(v), v)));
  $("set-planning-effort").replaceChildren(...options.reasoning_efforts.map((v) => new Option(effortName(v), v)));
  $("set-grid").replaceChildren(...options.grids.map((v) => new Option(GRID_NAMES[v] || v, v)));
  for (const [name, [lo, hi]] of Object.entries(options.limits)) {
    const input = settingInput(name);
    if (input) { input.min = lo; input.max = hi; }
  }
  fillSettings(values);
}

// While a benchmark card is chosen, it sets the shape and look budgets and locks them.
// Your own values are kept aside (and never sent while the card holds the fields), so
// they come back when the card is removed and stay saved for your next painting.
const CARD_BUDGET = ["shape_budget", "look_budget"];
let ownBudget = null; // {shape_budget, look_budget}, while a card holds the fields

function holdBudget(card) {
  if (!ownBudget) ownBudget = Object.fromEntries(CARD_BUDGET.map((n) => [n, settingInput(n).value]));
  settingInput("shape_budget").value = card.shape_budget;
  settingInput("look_budget").value = card.look_budget;
  const note = $("budget-note");
  note.textContent = `Set by the benchmark card, the same on every run: ${card.shape_budget} shapes `
    + `(the card has ${card.shapes}) and ${card.look_budget} looks with vision on, no top-ups.`;
  note.hidden = false;
  syncLooksInput();
}

function releaseBudget() {
  if (!ownBudget) return;
  for (const n of CARD_BUDGET) settingInput(n).value = ownBudget[n];
  ownBudget = null;
  $("budget-note").hidden = true;
  syncLooksInput();
}

function fillSettings(values) {
  for (const [name, value] of Object.entries(values)) {
    if (ownBudget && CARD_BUDGET.includes(name)) { ownBudget[name] = value; continue; }
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
    if (ownBudget && CARD_BUDGET.includes(input.dataset.setting)) continue; // the card's, not yours
    out[input.dataset.setting] = input.type === "checkbox" ? input.checked
      : input.type === "number" ? Number(input.value) : input.value;
  }
  return out;
}

// Looks only matter when Painty can see the canvas, and the dialog says so. A benchmark
// card holds both budgets. The planning effort only matters with planning on.
function syncLooksInput() {
  const blind = !settingInput("vision").checked;
  settingInput("planning_effort").disabled = settingsLocked || !settingInput("planning").checked;
  settingInput("shape_budget").disabled = settingsLocked || Boolean(ownBudget);
  settingInput("look_budget").disabled = settingsLocked || Boolean(ownBudget) || blind;
  $("looks-note").hidden = !blind;
}

function lockSettings(locked) {
  settingsLocked = locked;
  for (const input of settingInputs()) input.disabled = locked;
  syncLooksInput();
}

document.addEventListener("change", (ev) => {
  if (!ev.target.dataset?.setting) return;
  if (["vision", "planning"].includes(ev.target.dataset.setting)) syncLooksInput();
  if (rerunning && dialog.contains(ev.target)) return; // saved when Paint is pressed
  send({ type: "settings", settings: readSettings() });
});

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
  galleryItems = items;
  assignModelColours(items);
  renderGallery();
  const focus = focusId && items.find((m) => m.id === focusId);
  if (focus) showDetail(focus);
  else $("close-gallery").focus();
}

// ---- model colours
// Each model gets its own colour, so you can tell at a glance which model made which painting.
// The models in PAINTY_MODELS come first, in their order, then any others in the gallery
// alphabetically, so the colours stay put while the model list stays the same.
const MODEL_COLOURS = ["#8ecae6", "#f4a3a8", "#b5e48c", "#cdb4db", "#ffd166",
  "#8fd9c1", "#f7a072", "#a9b8f0", "#d9c2a3", "#e7a6d9"];
let configuredModels = []; // PAINTY_MODELS, from hello
let modelColours = new Map();

function assignModelColours(items) {
  const inGallery = new Set(items.map((m) => m.settings?.model).filter(Boolean));
  const others = [...inGallery].filter((m) => !configuredModels.includes(m)).sort();
  const order = [...configuredModels, ...others];
  modelColours = new Map(order.map((m, i) => [m, MODEL_COLOURS[i % MODEL_COLOURS.length]]));
}

function modelPill(model) {
  const pill = el("span", "model-pill", model || "unknown model");
  const colour = modelColours.get(model);
  if (colour) pill.style.setProperty("--model", colour);
  return pill;
}

// ---- favourites
const TARGET_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/></svg>';
const STAR_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.8l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L12 17.1l-5.7 3.2 1.2-6.4-4.7-4.4 6.4-.8z" stroke-linejoin="round"/></svg>';

function starButton(m, cls = "") {
  const b = el("button", `star ${cls}`.trim());
  b.type = "button";
  b.dataset.id = m.id;
  b.innerHTML = STAR_SVG;
  syncStar(b, m);
  b.addEventListener("click", (ev) => { ev.stopPropagation(); toggleFavourite(m); });
  return b;
}

function syncStar(b, m) {
  const on = Boolean(m.favourite);
  b.setAttribute("aria-pressed", String(on));
  b.setAttribute("aria-label", on ? "Remove from favourites" : "Add to favourites");
  b.title = on ? "Favourite: click to remove" : "Add to favourites";
}

async function toggleFavourite(m) {
  const want = !m.favourite;
  m.favourite = want; // show it straight away, and put it back if saving fails
  showFavourite(m);
  try {
    const res = await fetch(`/api/gallery/${encodeURIComponent(m.id)}/favourite`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ favourite: want }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  } catch (err) {
    m.favourite = !want;
    showFavourite(m);
    toast(`Couldn't save that favourite. ${err.message}`, "error");
  }
}

function showFavourite(m) {
  for (const b of document.querySelectorAll(".star")) if (b.dataset.id === m.id) syncStar(b, m);
  // Showing favourites only: an unstarred painting leaves the grid (or comes back).
  if (!$("gallery-grid").hidden) renderGallery({ keepScroll: true });
  else renderFilters();
}

// ---- filters
// No model chosen means every model; models are either-or, the other filters stack on top.
// Kept while the page is open.
let galleryItems = [];
const galleryFilter = { models: new Set(), favourites: false, benchmarks: false };

// Each filter on its own: does painting m pass it?
const FACETS = {
  models: (m) => !galleryFilter.models.size || galleryFilter.models.has(m.settings?.model),
  favourites: (m) => !galleryFilter.favourites || Boolean(m.favourite),
  benchmarks: (m) => !galleryFilter.benchmarks || Boolean(m.benchmark),
};

// Passes every filter, or every filter but one (skip), for the chips' counts.
function shown(m, skip = null) {
  return Object.entries(FACETS).every(([name, passes]) => name === skip || passes(m));
}

// A chip's count stacks on the other filters: how many paintings it would show with them.
// A model chip ignores the model choice (models are either-or), so it counts its own.
function countWhere(skip, test) {
  return galleryItems.filter((m) => shown(m, skip) && test(m)).length;
}

function filterChip(label, pressed, onClick, colour) {
  const b = el("button", "filter-chip");
  b.type = "button";
  b.setAttribute("aria-pressed", String(pressed));
  if (colour) {
    const dot = el("span", "chip-dot");
    dot.style.setProperty("--model", colour);
    b.appendChild(dot);
  }
  b.append(label);
  b.addEventListener("click", onClick);
  return b;
}

function renderFilters() {
  const inGallery = new Set(galleryItems.map((m) => m.settings?.model));
  for (const name of [...galleryFilter.models]) if (!inGallery.has(name)) galleryFilter.models.delete(name);
  const counts = new Map();
  for (const m of galleryItems) {
    if (shown(m, "models")) counts.set(m.settings?.model, (counts.get(m.settings?.model) || 0) + 1);
  }

  const chips = [filterChip(`All models (${countWhere("models", () => true)})`, galleryFilter.models.size === 0, () => {
    galleryFilter.models.clear();
    renderGallery();
  })];
  for (const [model, colour] of modelColours) {
    if (!inGallery.has(model)) continue; // a model stays listed (at 0) while other filters hide it
    chips.push(filterChip(`${model} (${counts.get(model) || 0})`, galleryFilter.models.has(model), () => {
      if (galleryFilter.models.has(model)) galleryFilter.models.delete(model);
      else galleryFilter.models.add(model);
      renderGallery();
    }, colour));
  }
  $("filter-models").replaceChildren(...chips);

  const fav = $("filter-favourites");
  fav.innerHTML = STAR_SVG;
  fav.append(` Favourites (${countWhere("favourites", (m) => m.favourite)})`);
  fav.setAttribute("aria-pressed", String(galleryFilter.favourites));
  const bench = $("filter-benchmarks");
  bench.innerHTML = TARGET_SVG;
  bench.append(` Benchmarks (${countWhere("benchmarks", (m) => m.benchmark)})`);
  bench.setAttribute("aria-pressed", String(galleryFilter.benchmarks));
  $("gallery-filters").hidden = galleryItems.length === 0 || $("gallery-grid").hidden;
}

$("filter-favourites").addEventListener("click", () => {
  galleryFilter.favourites = !galleryFilter.favourites;
  renderGallery();
});
$("filter-benchmarks").addEventListener("click", () => {
  galleryFilter.benchmarks = !galleryFilter.benchmarks;
  renderGallery();
});

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
  return `eyes ${s.vision ? "on" : "off"}, ${m.shapes_used} shapes`; // the model is its own pill
}

function renderGallery({ keepScroll = false } = {}) {
  const grid = $("gallery-grid");
  const scroll = $("gallery-body").scrollTop;
  grid.replaceChildren();
  renderFilters();
  const items = galleryItems.filter(shown);
  $("filter-count").textContent = items.length === galleryItems.length ? ""
    : `Showing ${items.length} of ${galleryItems.length}`;
  $("gallery-empty").textContent = galleryItems.length
    ? "No paintings match these filters."
    : "No paintings yet. Finished drawings are saved here automatically.";
  $("gallery-empty").hidden = items.length > 0;
  for (const m of items) {
    const card = el("article", "gallery-card");
    const open = el("button", "card-open");
    open.type = "button";
    const art = el("span", "card-art");
    const img = el("img", "card-painting");
    img.src = `/gallery/${encodeURIComponent(m.id)}/painting.png`;
    img.alt = m.title;
    img.loading = "lazy";
    art.appendChild(img);
    if (m.benchmark_score) {
      const badge = el("span", "card-score", pct(m.benchmark_score.score));
      badge.title = `Benchmark score: ${cardTitle(m.benchmark)}`;
      art.appendChild(badge);
    }
    if (m.reference) { // the reference, small in the corner, so the pair reads at a glance
      const ref = el("img", "card-ref");
      ref.src = `/gallery/${encodeURIComponent(m.id)}/reference.png`;
      ref.alt = "Reference image";
      ref.loading = "lazy";
      art.appendChild(ref);
    }
    const text = el("div", "gallery-text");
    const meta = el("p", "gallery-meta");
    meta.append(modelPill(m.settings?.model), ` ${summary(m)}`);
    text.append(el("h3", "", m.title), el("p", "gallery-prompt", preview(promptOf(m))), meta);
    open.append(art, text);
    open.addEventListener("click", () => showDetail(m));
    card.append(open, starButton(m, "card-star"));
    grid.appendChild(card);
  }
  if (keepScroll) $("gallery-body").scrollTop = scroll;
}

// A benchmark painting's score item by item, and the picture of the difference.
function scoreItems(m, base) {
  const wrap = el("section", "score-items");
  wrap.appendChild(el("h4", "", "Benchmark items"));
  const table = el("table", "score-table");
  const head = el("tr");
  for (const h of ["Item", "Score", "Shape", "Colour", "Offset"]) head.appendChild(el("th", "", h));
  table.appendChild(head);
  for (const i of m.benchmark_score.items) {
    const row = el("tr");
    row.title = `${i.level}. Shape at its best offset: ${pct(i.shifted)}`;
    for (const v of [i.name, pct(i.score), pct(i.shape), i.colour === null ? "–" : pct(i.colour),
      i.offset ? offsetText(i.offset).replace(" px", "") : "–"]) row.appendChild(el("td", "", v));
    table.appendChild(row);
  }
  wrap.appendChild(table);
  const diff = el("button", "score-diff");
  diff.type = "button";
  diff.title = "Card, painting and difference: grey matches, red is missing, blue is extra";
  const img = el("img");
  img.src = `${base}/benchmark-diff.png`;
  img.alt = "The card, the painting and the difference between them";
  img.loading = "lazy";
  diff.appendChild(img);
  diff.addEventListener("click", () => openLightbox(img.src, "Card · painting · difference",
    [el("p", "", "Grey is paint in both, red is paint the copy is missing, blue is paint it added.")]));
  wrap.appendChild(diff);
  return wrap;
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
  const title = el("div", "detail-title");
  title.append(el("h3", "", m.title), starButton(m));
  right.append(title, el("p", "painting-note", m.note || ""));
  const again = el("button", "ghost-dark again", "Paint again");
  again.type = "button";
  again.title = "Open New painting with this painting's prompt, image and settings";
  again.addEventListener("click", () => paintAgain({
    prompt: m.prompt, reference: m.reference ? `${base}/reference.png` : null,
    benchmark: m.benchmark, shape: canvasOf(m).shape, settings: m.settings, title: m.title,
  }));
  const replayBtn = el("button", "ghost-dark again", "Replay");
  replayBtn.type = "button";
  replayBtn.title = "Paint it again on the canvas, shape by shape, as it was painted";
  replayBtn.disabled = !m.shapes_used;
  replayBtn.addEventListener("click", () => replay(m));
  const actions = el("div", "detail-actions");
  actions.append(again, replayBtn);
  right.appendChild(actions);

  const endings = { finished: "Painty finished it", ended: "Painty stopped without finishing", saved: "You saved it" };
  const c = canvasOf(m);
  const rows = [
    ["Prompt", promptOf(m)],
    ["Canvas", `${c.shape[0].toUpperCase()}${c.shape.slice(1)}, ${c.width} × ${c.height}`],
    ...(m.benchmark ? [["Benchmark", cardTitle(m.benchmark)]] : []),
    ...(m.benchmark_score ? [["Score", `${pct(m.benchmark_score.score)} (whole canvas `
      + `${pct(m.benchmark_score.canvas)}, offset ${offsetText(m.benchmark_score.offset)})`]] : []),
    ["Model", [modelPill(s.model), ` (effort: ${s.reasoning_effort || "default"})`]],
    ["Vision", s.vision ? "On" : "Off"],
    ["Grid", GRID_NAMES[s.grid || "frame"] || s.grid], // older paintings had the ruler frame
    ["Exemplar", s.exemplar ? "Shown" : "Not shown"],
    ["Planning", s.planning ? `On (effort: ${s.planning_effort || "default"}), `
      + `${(m.plans || []).length} plan${(m.plans || []).length === 1 ? "" : "s"} written` : "Off"],
    ["Shapes", `${m.shapes_used} of ${m.shapes_budget}`],
    ["Looks", `${m.looks_used} of ${m.looks_budget}`],
    ["Top-ups", String(m.top_ups)],
    ["Tokens", tokenText({ input: m.input_tokens, cached: m.cached_tokens,
                           output: m.output_tokens, reasoning: m.reasoning_tokens })],
    ["Cost", costInfo(m.cost)],
    ["Time", m.duration_s ? `${Math.round(m.duration_s)} s` : "?"],
    ["Ending", endings[m.ending] || m.ending],
    ["Painted", new Date(m.created_at * 1000).toLocaleString("en-GB")],
  ];
  const dl = el("dl");
  for (const [k, v] of rows) {
    const dd = el("dd");
    dd.append(...[].concat(v)); // text, or text and elements such as the model pill
    dl.append(el("dt", "", k), dd);
  }
  right.appendChild(dl);
  if (m.benchmark_score) right.appendChild(scoreItems(m, base));

  const files = el("p", "files");
  const fileLinks = [["PNG", "painting.png"], ["SVG", "painting.svg"],
    ...(m.shape_format === "notation" ? [["Shapes", "shapes.txt"]] : []),
    ...(m.reference ? [["Reference", "reference-view.png"]] : []), // as Painty saw it, grid and all
    ...(m.benchmark ? [["Benchmark card", "benchmark.json"]] : []),
    ...(m.benchmark_score ? [["Score diff", "benchmark-diff.png"]] : []),
    ...(m.exemplar_sheets || []).map((file, i) => [`Exemplar ${i + 1}`, file]),
    ...(m.plans?.length ? [["Plan", "plan.txt"]] : []),
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
  $("gallery-filters").hidden = true;
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
  renderGallery(); // picks up a favourite changed on the painting's page
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
});

connect();
