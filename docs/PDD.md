# Painty: project design document

Version 0.2.0, 1 November 2026. Owner: Oliver Carr.

This records what Painty is for, every design decision made so far and the reasons behind them, so later work (by a person, Claude Code or another agent) builds on them rather than undoing them. `AGENTS.md` covers how to work on the code; this covers why it is the way it is.

## 1. Purpose

Painty is a personal research project, built alongside *AI Agents in Action, 2nd ed.* (Michael Lanham) as a sibling to the Oliver-J-Carr/Matey project. It asks one question:

> Can a language model adapt to a paint system it has never seen, and use tool calls to paint a recognisable picture from a free-text description?

Examples: "draw a face", "draw a cat", "draw a red cat", "draw a stripy tiger with blue and green stripes".

What this does and doesn't test:

- **It does test** adapting to a custom tool interface, reasoning about space through raw coordinates, planning layers, and (with vision) judging and correcting its own work.
- **It doesn't test** total novelty. The model has seen Paint, SVG and countless pictures in training, and it needs that knowledge to know what a cat looks like. The point is that it must turn that knowledge into *this* system, not write code it memorised.

## 2. Scope of v0.1

In scope:

- One prompt per drawing. The agent works until it finishes or runs out of budget.
- Four shapes, a structured paint vocabulary, permanent layered paint.
- Optional vision for the model to visually peek at what it has drawn so far, through a `look` tool.
- On-screen settings for model, reasoning effort, vision and budgets.
- Live canvas, activity feed with look thumbnails, keep going, stop, gallery.
- Runs locally as a Docker container.

Out of scope for now (see section 12): follow-up prompts on the same canvas, raw SVG mode, user drawing or editing, deleting from the gallery in the app, multiple users.

## 3. The canvas

| Decision | Choice | Why |
|---|---|---|
| Size | 512 × 512 px, fixed | Simple numbers, cheap image tokens, square. A constant in `canvas.py`, not a setting |
| Background | Plain white | Like a blank canvas. The agent paints its own background |
| Origin | (0, 0) top-left, y grows downwards | Every display system works this way (screens draw from the top row down, like reading a page): SVG, PNG pixels, the browser and Paint. Keeping it means the tool coordinates, the SVG and the agent's snapshot all agree, with no flipping and no conversion bugs. Flipping y is a possible "hard mode" later |
| Off-canvas | Coordinates allowed from −256 to 768; anything past the edge is clipped | Lets the agent bleed shapes off the edge naturally, while catching wild numbers |

## 4. The paint system

### 4.1 Shapes

Four kinds: `line`, `rectangle`, `ellipse`, `triangle`.

- **Drag box.** Like Microsoft Paint: (x1, y1) and (x2, y2) are opposite corners of a box and the shape fills it. Any pair of opposite corners works in any order (top-left to bottom-right, bottom-left to top-right and so on); the box is normalised. Using "a start and an end" rather than "top-left and bottom-right" keeps that freedom.
- **Lines** go from (x1, y1) to (x2, y2) and keep their direction. They have round ends.
- **Circles and squares** are an ellipse or rectangle in a square box.

### 4.2 Triangle and rotation

Options considered: a box plus one of four directions (Paint-like, but limited), three free points (most flexible, but not Paint-like), or a box plus a 360° angle.

**Chosen: box plus a 360° rotation, shared by all filled shapes.**

- With rotation 0 the triangle's tip is at the middle of the box's top edge and its base runs along the bottom edge (isosceles, like Paint).
- `rotation` turns the shape clockwise around the box centre, like a compass: 0° up, 90° right, 180° down, 270° left, and anything in between.
- Rectangles and ellipses use the same field, so it's one concept to learn rather than a triangle special case.
- A turned shape can poke outside its box. The agent has to reason about that, which is part of the adaptation test.
- Lost: lopsided (scalene) triangles. Overlapping shapes can make up for it. Free three-point triangles are a roadmap idea.

### 4.3 Paint (fill)

Raw CSS or SVG styling was considered and **rejected**. It would hand the familiar system back (the model would reach for memorised tricks such as clip-path and box-shadow), defeating the test, and it could break out of the canvas. Instead the agent gets "a paint set, not the paint factory": a small structured vocabulary.

- Fill: one colour (solid), `linear` (2–3 colours blending in a direction) or `radial` (2–3 colours from the centre out to the edge).
- Colours: hex (`#rgb` or `#rrggbb`). Full freedom of colour was a requirement.
- Linear angle: the same compass convention as rotation (0 = bottom to top, 90 = left to right). The blend turns with the shape.
- `opacity`: 0.05 to 1. Below 1, the paint beneath shows through. This is what makes shading, shadows, highlights and glazes possible, which matters for refining detail.
- `thickness` and `outline`: line thickness for lines (1–100 px, default 3); optional outline for other shapes (0–40 px, default 2 when an outline colour is given).

### 4.4 Layering

Paint is permanent and layers like real paint: draw order is paint order, and where shapes overlap the newer one covers the older (the "painter's algorithm"). There is no layers feature. With opacity below 1, the newer shape tints rather than covers.

### 4.5 No erasing

There is no undo, erase, clear or move. Paint is paint: the agent can only paint over mistakes (for example with the background colour). This keeps the system honest to the physical-canvas idea and makes the test harder in a meaningful way.

### 4.6 Validation

- Every problem in a batch is reported, each named by its number within the batch and quoting what the agent wrote (`shape 3 "ellipse 0 0 40 40 red": 'red' is not a hex colour like #ff8800`).
- **Painted up to the first bad shape:** the shapes before the first invalid one are painted; it and everything after it are not, and cost no budget. The agent sends the rest again, fixed, and it lands in the planned order. Painting each valid shape on its own was rejected: if the face is skipped but the eyes are painted, resending the face covers the eyes. This replaced all-or-nothing batches, which threw away good shapes and made the agent resend the whole batch.
- Whole-batch problems (empty, over the per-call limit, more shapes than the budget has left) still paint nothing.
- Harmless oddities are painted anyway with a note (for example, extra colours on a solid fill).
- Rules: coordinates in range; at least 2 × 2 px for filled shapes; lines need length (use a small ellipse for a dot); valid hex colours; opacity 0.05–1; thickness limits as above.

### 4.7 The shape notation

The agent writes each shape as one short line of text rather than a JSON object:

```
kind x1 y1 x2 y2 fill [name=value extras]
rectangle 0 0 512 340 linear #4a90d9 #cfe8ff 180
ellipse 196 180 316 300 #f2c9a0 outline=#5a3a1a thickness=3
```

- **Why:** output tokens are the expensive kind, and JSON spends most of them on braces, quotes and field names. A typical shape is roughly a third of the tokens, and past paint calls in the history shrink by the same factor.
- **Fixed positions only for what every shape has** (kind, box, fill). The rarely used options are optional `name=value` tags with defaults, so most shapes have none, and there's no long run of positional numbers to miscount.
- **Tags use the full field names** (`opacity=`, not `o=`): they're rare, so letters would save little, and full names explain themselves.
- **Colours keep the `#`.** It costs almost nothing and means a colour can never be mistaken for a number.
- **Forgiving about small slips:** letter case, commas, spaces round `=`, `rect`, and several shapes crammed into one string. Never about meaning: no named colours, no missing `#`, no unknown tags.
- It is still Painty's own system, not SVG: no tags, no attributes, no path commands.
- Risk: strict JSON schemas guaranteed well-formed shapes; now only the parser and the prompt do. If some models do badly, a JSON mode could come back as a setting for comparison.
- The parser builds the same internal `Shape`, so validation, rendering and the gallery were unchanged. `shapes.json` keeps each shape's original `text`, so format mistakes can be studied.

## 5. Tools

| Tool | Arguments | What it does |
|---|---|---|
| `paint` | `shapes` (ordered list of shape notation strings, bottom layer first), `note` | Validates and paints a batch, up to any bad shape. Returns what was painted, any problems and what's left in the budget |
| `look` | `focus` (what it wants to check) | Returns a PNG of the canvas to the same model. Vision on only |
| `finish` | `title`, `note` | Ends the drawing. The painting is saved to the gallery |

### 5.1 Batches and ordering

Parallel calls may run at the same time, which could scramble the paint order. So:

- `paint` takes an **ordered list** of shapes, which gives reliable layering, fewer round trips and easy counting.
- `parallel_tool_calls=False`, so there is one tool call per model turn.
- A lock in `paint` is a backstop.

### 5.2 Vision: the same model sees its own work

A key requirement: the model that paints must see the canvas itself, rather than receiving a text description from a separate model. It's a natively multimodal model (image input goes into the same network that's doing the painting).

- `look` returns the PNG through the Agents SDK's `ToolOutputImage`, straight into the painter's own context.
- Default model `gpt-6-luna`. Others are offered for comparison.

### 5.3 The agent's view: ruler and grid

Vision models judge *where* things are far better than they read exact numbers. So the agent's snapshot is the painting inside a numbered ruler frame (0 to 512 every 64 px, on all four sides) with faint grey grid lines every 64 px. The ruler and grid are in the agent's view only, never in the painting, the browser canvas or the gallery PNG. The tool result tells the agent they aren't paint.

## 6. Budgets and limits

The book's Ch 9 says every agentic loop needs a goal, an iteration strategy and termination conditions. Painty's termination is layered:

| Limit | Default | Enforced by |
|---|---|---|
| Shapes per drawing | 60 | `paint`; the run pauses at 0 |
| Looks per drawing | 5 | `look` returns an error at 0 |
| Shapes per `paint` call | 15 | `paint`. Stops the agent painting everything in one blind batch, which matters for the vision comparison |
| Hidden turn limit | shapes left + looks left + 10 | the SDK's `max_turns`. A runaway guard: one model call per shape and look is the most a sensible run needs |

All the visible limits are set in the on-screen settings panel (not env vars), so they can be changed while experimenting.

### 6.1 Keep going

When the shape budget runs out, the drawing **pauses** rather than halting, because the agent may be nearly done. The UI offers "Keep going" (adds the top-up amounts: default 20 shapes and 2 looks) or "Finish here" (saves as is).

How it works: the agent's `tool_use_behavior` ends the run cleanly when the budget hits 0, so the SDK returns the complete conversation history. "Keep going" tops up the budget and runs the agent again with that history plus a message saying how much more it has. Dynamic instructions show the new totals. Hitting the hidden turn limit also pauses, with the history rebuilt from the exception's run data.

### 6.2 Settings are frozen per drawing

A drawing copies the settings when it starts, so changing them never changes the model, vision or budget under a running agent. Settings are locked while painting. Top-up sizes are read live when "Keep going" is clicked.

## 7. Prompting

- **One prompt per drawing** (v0.1). Free text, up to 500 characters. Each new prompt starts a fresh canvas.
- The system prompt (`app/prompts/painter.md`) teaches the paint system, coordinates, layering, budget, a working method (plan in pixels, paint back to front in stages, look after each main stage but not after the final touches) and to always end with `finish`. It has a vision-on and a vision-off section.
- The agent works on its own and never asks questions.
- Each `paint` call carries a short `note`, shown in the feed, so viewers can follow its plan.
- Instructions are dynamic, so the budget totals are right after a "keep going" top-up, but they never change within a run. The prompt cache only reuses an exact prefix of each request, and the instructions come before the whole history, so live counts there (an earlier "right now" status line) made every call miss the cache. Live counts are in the tool results instead.
- Each drawing's id is sent as the `prompt_cache_key`, so all its calls, across keep going segments, are routed to the same cache.

## 8. Architecture

Mirrors Matey: FastAPI + OpenAI Agents SDK backend, vanilla HTML/CSS/JS front end, one WebSocket, one container.

- **One renderer.** Each shape becomes one self-contained SVG fragment. The browser appends those fragments to its canvas (animated one by one). The backend joins the same fragments into one SVG and rasterises it with resvg for looks and the gallery. What you see is exactly what the agent sees.
- **State.** Drawing state is per WebSocket connection and in memory. Settings (`DATA_DIR/settings.json`) and the gallery (`DATA_DIR/gallery`) are app-wide plain files. No database.
- **Background task.** Each run is an asyncio task, so the socket can still receive "stop".
- **Tracing.** Each run segment (the start, and each keep going) is one trace in the OpenAI Traces dashboard, grouped by drawing id, with the model and vision setting in the metadata.

Drawing lifecycle:

```
idle --paint--> painting --finish--------> finished (saved)
                  |  \---stop-----------> stopped  --save--> finished
                  |   \--error----------> error    --save--> finished
                  v
                paused --keep going--> painting
                   \-----finish here--> finished (saved)
```

## 9. Interface

- **Canvas on a cutting mat.** The backdrop is a craft cutting mat whose grid echoes the coordinate grid the agent works in. The canvas is the one loud element.
- **Meters** for shapes (bar) and looks (dots).
- **Activity feed** (permanent right column): every batch with its note and shape counts, every look with a thumbnail of exactly what the agent saw (click to enlarge), errors, and the finish.
- **Paused card** with Keep going and Finish here. **Finished card** with title, note, shape and token counts, and a link to the gallery.
- **Settings drawer** for model, reasoning effort, vision and budgets.
- **Gallery** with each painting's prompt, settings, budgets used, top-ups, tokens, time, and links to the PNG, SVG and shape log (as the agent wrote it, and as JSON).
- **Stage viewer** in each gallery painting: the finished painting large, with a strip of every stage under it (one per paint call; click or use the arrow keys to load one into the preview). Looks alone don't tell the story: a model often paints several batches in a row without looking, tracking its layers in its head. Stages the agent looked at get a cadmium border and an eye badge, and a toggle under the preview swaps in exactly what it saw (ruler and grid included). Under the preview there is only one short line (stage and shapes), the same height on every stage so the layout never jumps; clicking the preview enlarges it with the full story: Painty's note for that paint call and what it was checking when it looked. Older paintings, which have no stages, show their looks in a list instead.
- Type: Pixelify Sans for the brand (a nod to Matey), Recursive for everything else, with its casual style reserved for Painty's own words (notes and titles).

## 10. Gallery

Each painting is a folder under `DATA_DIR/gallery` (in the container, the `painty-data` named volume at `/srv/data`):

- `painting.png`: clean, at 1024 px, for sharing
- `painting.svg`: the same painting as SVG
- `shapes.txt`: every shape in paint order, one line each, exactly as the agent wrote it ("Shapes" in the gallery)
- `shapes.json`: every shape in paint order as parsed data, with its original `text` ("Shapes (JSON)")
- `looks/look-N.png`: exactly what the agent saw, ruler and grid included
- `stages/stage-N.png`: the canvas after each paint call that painted something (512 px, no grid). Rendered at save time from the first shapes of the canvas, so painting gets no slower. A look at a blank canvas adds a `stage-0.png`
- `meta.json`: prompt, title, note, ending, settings, budgets used, top-ups, `shape_format` (`notation`; missing on paintings from before it, which have no `shapes.txt`), tokens (input with the cached share, output with the reasoning share), time, version, `stages` (each with its first and last shape, note and the looks taken there) and `looks` (each look's focus and how many shapes it saw). Paintings from before stages have neither

Endings: `finished` (the agent called finish), `ended` (it stopped without calling finish, so its last words become the note) and `saved` (you saved it by hand).

## 11. Experiments to try

- Same prompt, vision on and off. Does sight make paintings more recognisable, or just more expensive?
- Differences in models and reasoning levels.
- Tight against generous budgets: does a 20-shape limit force better composition?
- Increasing difficulty: face, cat, red cat, stripy blue-and-green tiger, a scene.
- Watch the traces: how does the agent use its looks? Does it correct what it sees?

## 12. Future ideas (not built)

- **Follow-up prompts on the same canvas** ("now add a volleyball net", "now add people playing"). Parked because of perspective: something "behind" an existing object would mean repainting the object over it. It's interesting, but a bigger problem.
- **SVG fallback (plan B).** If the custom system fails badly, try letting the agent describe shapes in SVG-like terms to see whether familiarity is what made the difference. Deliberately not the default, because it defeats the main test.
- **Hard mode** with an unfamiliar coordinate system (y up, or a non-pixel scale).
- **Final touches inside `finish`.** An optional `shapes` list on `finish`, painted (same checks, all-or-nothing, same budget) before the drawing ends, so the last batch and the finish share one model call instead of two. An invalid batch would paint nothing and not finish. It saves one mostly-cached call and its reasoning per drawing. Parked: the saving is small now caching works, it changes how the feed and logs show the last batch, and with full JSON shapes it would copy the shape schema into every request. Revisit after the compact shape format, when shapes are short strings. Not parallel tool calls: they would break one tool call per turn everywhere (a `look` could run before a `paint` in the same response lands).
- Free three-point triangles, polygons or curves.
- A canvas size or background colour setting.
- A side-by-side comparison view. 
- Deleting gallery items in the app.
- Local models (for example a vision model on local hardware) through the SDK's model providers.

## 13. Known limitations and risks

- Each look adds an image to the conversation, and the whole conversation is resent on every model call, so token use grows with each look.
- Small models may not follow the coordinate system well; that's part of what's being measured.
- Some models reject `reasoning.effort`; use "Model default" if so.
- Tracing uploads tool outputs, including look images, to OpenAI.
- The gallery lives in the `painty-data` Docker volume (the devbox convention for persistent data). `docker compose down -v` would delete it.

## 14. Decision log

| Date | Decision |
|---|---|
| 31 Oct 2026 | Custom shape-based paint system; no raw SVG or CSS for the agent |
| 31 Oct 2026 | Start/end drag box, any direction; (0, 0) top-left |
| 31 Oct 2026 | Triangle as box plus 360° rotation; rotation shared by all filled shapes |
| 31 Oct 2026 | Structured fill (solid, linear, radial, 2–3 hex colours), opacity, outline |
| 31 Oct 2026 | Permanent layered paint; no erase or undo |
| 31 Oct 2026 | Batch `paint` tool, one tool call per turn, all-or-nothing batches |
| 31 Oct 2026 | Vision through `ToolOutputImage` to the same model; vision toggle for comparison |
| 31 Oct 2026 | Ruler and grid in the agent's view only |
| 31 Oct 2026 | Budgets: 60 shapes, 5 looks, 15 per call; set on screen; keep going adds 20 and 2 |
| 31 Oct 2026 | Pause and keep going on budget exhaustion, resuming the same conversation |
| 31 Oct 2026 | One prompt per drawing in v0.1 |
| 31 Oct 2026 | Gallery as plain files in a named Docker volume; settings saved to JSON |
| 31 Oct 2026 | Mirror Matey's structure; port 8091; named Painty |
| 1 Nov 2026 | Record cached and reasoning tokens per drawing |
| 1 Nov 2026 | Instructions fixed within a run (no live status line) and drawing id as `prompt_cache_key`, so the prompt cache works |
| 1 Nov 2026 | No final look before finishing: the prompt says to finish straight after the final touches. A look at that point rarely led to a correction, and each look sends an image |
| 1 Nov 2026 | Shapes written as one-line text notation instead of JSON; batches painted up to the first bad shape instead of all or nothing |
| 4 Nov 2026 | Gallery saves a snapshot of every stage (each paint call), and shows them in one strip with the looked-at stages marked and a toggle to what the agent saw |
