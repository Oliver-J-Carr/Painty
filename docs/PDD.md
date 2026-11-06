# Painty: project design document

Version 0.5.0, 6 November 2026. Owner: Oliver Carr.

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
| Size | Three shapes, chosen per drawing in the New painting dialog: square 512 × 512 (default), landscape 768 × 512 and portrait 512 × 768 | Simple numbers and cheap image tokens. The short side is always 512, so a detail is the same size in pixels on every canvas, and both sides are multiples of the 64 px grid, so the ruler fits exactly. 3:2 sits between 4:3 and 16:9 photos, so a reference image crops little to fit. `CANVAS_SHAPES` in `canvas.py`, not a setting: the shape is part of what you ask for, like the prompt |
| Background | Plain white | Like a blank canvas. The agent paints its own background |
| Origin | (0, 0) top-left, y grows downwards | Every display system works this way (screens draw from the top row down, like reading a page): SVG, PNG pixels, the browser and Paint. Keeping it means the tool coordinates, the SVG and the agent's snapshot all agree, with no flipping and no conversion bugs. Flipping y is a possible "hard mode" later |
| Off-canvas | Coordinates allowed up to half a side past each edge, per axis (−256 to 768 on a 512 side, −384 to 1152 on a 768 side); anything past the edge is clipped | Lets the agent bleed shapes off the edge naturally, while catching wild numbers |

## 4. The paint system

### 4.1 Shapes

Six kinds. Five are drag-box shapes: `line`, `rectangle`, `ellipse`, `triangle`, `diamond`. One is walked: `trail` (section 4.8).

- **Drag box.** Like Microsoft Paint: (x1, y1) and (x2, y2) are opposite corners of a box and the shape fills it. Any pair of opposite corners works in any order (top-left to bottom-right, bottom-left to top-right and so on); the box is normalised. Using "a start and an end" rather than "top-left and bottom-right" keeps that freedom.
- **Lines** go from (x1, y1) to (x2, y2) and keep their direction. They have round ends, and can be bent and tapered (section 4.9).
- **Circles and squares** are an ellipse or rectangle in a square box.
- **Diamond** has its four corners at the middle of each edge of its box. Added in v0.3 (on the experimental-shapes branch, brought back in v0.5) because an isometric top face is a diamond in a box about 1.73 : 1, and it suits kites, gems and highlights.

### 4.2 Triangle and rotation

Options considered: a box plus one of four directions (Paint-like, but limited), three free points (most flexible, but not Paint-like), or a box plus a 360° angle.

**Chosen: box plus a 360° rotation, shared by all filled shapes.**

- With rotation 0 the triangle's tip is at the middle of the box's top edge and its base runs along the bottom edge (isosceles, like Paint).
- `rotation` turns the shape clockwise around the box centre, like a compass: 0° up, 90° right, 180° down, 270° left, and anything in between.
- Rectangles and ellipses use the same field, so it's one concept to learn rather than a triangle special case.
- A turned shape can poke outside its box. The agent has to reason about that, which is part of the adaptation test.
- Lost: lopsided (scalene) triangles. Overlapping shapes can make up for it. Free three-point triangles are a roadmap idea.

### 4.2.1 Skew

Models asked for isometric pictures had to fake slanted faces (one built a car body from about 50 lines; another made a slanted rectangle from two triangles, and its gradient didn't line up). A free polygon would fix that but is SVG's `points` list under another name.

**Chosen: `skew`, like MS Paint's "Stretch and Skew".** The box's left and right edges stay upright and its top and bottom edges tilt by the skew angle (−70° to 70°; positive drops the right side, so it turns the same way as rotation). It works on every box shape and is applied before rotation, both around the box centre. The fill is defined inside the shape's own transformed frame, so a linear blend skews with the face. An isometric box is a diamond on two rectangles skewed 30° and −30°.


### 4.3 Paint (fill)

Raw CSS or SVG styling was considered and **rejected**. It would hand the familiar system back (the model would reach for memorised tricks such as clip-path and box-shadow), defeating the test, and it could break out of the canvas. Instead the agent gets "a paint set, not the paint factory": a small structured vocabulary.

- Fill: one colour (solid), `linear` (2–3 colours blending in a direction) or `radial` (2–3 colours from the centre out to the edge).
- Colours: hex (`#rgb` or `#rrggbb`). Full freedom of colour was a requirement.
- Linear angle: the same compass convention as rotation (0 = bottom to top, 90 = left to right). The blend turns with the shape.
- `opacity`: 0.05 to 1. Below 1, the paint beneath shows through. This is what makes shading, shadows, highlights and glazes possible, which matters for refining detail.
- `thickness` and `outline`: line thickness for lines and trails (1–100 px, default 3); optional outline for other shapes (0–40 px, default 2 when an outline colour is given).

### 4.3.1 Texture: spray and soft

Two extras that work on every shape and every fill (texture as a fill *style* was rejected: it would rule out sprayed gradients):

- `spray` (0.05–1, default 1): only that share of the paint lands, as pixel-fine speckle, like MS Paint's spray can. A thick sprayed line is an airbrush stroke; layered sprays build texture (sand, stubble, fur, rust, mist).
- `soft` (0–30 px, default 0): feathered edges, for glows, shadows, clouds and blends.

Both are a per-shape SVG `<filter>` inside the shape's own fragment, so the one-renderer rule holds. Spray is `feTurbulence` fractal noise, cut at the share asked for and used as a mask. The noise values fall on a bell curve (mean 0.515, spread 0.12, measured through resvg), so the cut comes from that curve, and `spray=0.3` covers about 30%. The noise is seeded with the shape's number, so the browser, the agent's look and the gallery all show the same speckle. The filter region is set in pixels (`userSpaceOnUse`), because a level line's box is 0 px tall and a box-sized filter would hide it. It isn't clipped to the canvas, because a fragment doesn't know which canvas shape it's on (clipping it to 512 would cut off spray on the right of a landscape canvas). Soft blurs first and spray masks after, so a soft sprayed shape thins out towards its edges.

Considered and not built: named pattern fills such as stripes or dots (a `stripes` fill does the thinking for "a stripy tiger"), and a scatter or stamp option (one shape becoming dozens quietly breaks the budget). Rough hand-drawn edges and hatched shading are future ideas.


### 4.4 Layering

Paint is permanent and layers like real paint: draw order is paint order, and where shapes overlap the newer one covers the older (the "painter's algorithm"). There is no layers feature. With opacity below 1, the newer shape tints rather than covers.

### 4.5 No erasing

There is no undo, erase, clear or move. Paint is paint: the agent can only paint over mistakes (for example with the background colour). This keeps the system honest to the physical-canvas idea and makes the test harder in a meaningful way.

### 4.6 Validation

- Every problem in a batch is reported, each named by its number within the batch and quoting what the agent wrote (`shape 3 "ellipse 0 0 40 40 red": 'red' is not a hex colour like #ff8800`).
- **Painted up to the first bad shape:** the shapes before the first invalid one are painted; it and everything after it are not, and cost no budget. The agent sends the rest again, fixed, and it lands in the planned order. Painting each valid shape on its own was rejected: if the face is skipped but the eyes are painted, resending the face covers the eyes. This replaced all-or-nothing batches, which threw away good shapes and made the agent resend the whole batch.
- Whole-batch problems (empty, over the per-call limit, more shapes than the budget has left) still paint nothing.
- Harmless oddities are painted anyway with a note (for example, extra colours on a solid fill).
- Rules: coordinates in range (for a trail, its start and the end of every leg, per axis); at least 2 × 2 px for filled shapes; lines need length (use a small ellipse for a dot) and each leg is at least 1 px; a trail has 1–12 legs; valid hex colours; opacity 0.05–1; skew −70 to 70; spray 0.05–1; soft 0–30; bend 0–256; thickness limits as above.
- Harmless notes also cover extras that do nothing on a kind (rotation and skew on a trail, skew on a line, smooth on a box shape, bend on anything but a line), and every painted trail gets a note saying where it turned and ended (section 4.8).

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
- Walks swap the box for a start point and legs: `trail x y 40@90 30@135 #333 thickness=4` (section 4.8).
- The parser builds the same internal `Shape`, so validation, rendering and the gallery were unchanged. `shapes.json` keeps each shape's original `text`, so format mistakes can be studied.

### 4.8 Trails

A multi-point line was wanted for bendy lines (tails, branches, smiles), but in SVG it's a point list, which a model can fill from memory.

**Chosen: a walk.** The agent gives a start point, then legs written `distance@heading` on the same compass as rotation and blends (`40@90` goes 40 px right). It never writes a list of points or a path command.

- A trail is painted as a line with round ends and joins. Fill, opacity, spray and soft work as on other shapes. Rotation and skew don't apply; the headings set the direction.
- At most 12 legs (`MAX_LEGS`), so one shape can't wander forever. A whole trail counts as one shape.
- `smooth=1` turns the corners into a curve through every corner (Catmull-Rom curves drawn as cubic Béziers by the renderer). It swings out a little between corners rather than cutting them.
- Risk: drift. Models work out headings in their heads badly, and the errors add up. So the paint result lists where every bend and the end of each trail really landed, in whole pixels, and the prompt says to place anything along a trail from those positions rather than its own sums. In v0.3.1 only the end was reported. A tiger's tail stripes then landed about 15 px beside the tail, because the agent placed absolute-coordinate stripes against bends it had calculated itself; it only fixed them after a look.
- Gradients size themselves to the box around every corner. Internally, `start` is the start point, `end` is where the last leg ends, and `legs` is kept in `shapes.json`.

**`patch` was tried and removed (v0.3.0 → v0.3.1).** `patch` was the same walk, closed with a straight edge and filled, with `smooth=1` giving rounded blobs: a free polygon in a different notation. With GPT-6.1-Sol it went badly on organic subjects. A Hojakdo tiger that v0.1 painted well from 125 lines, 64 ellipses and 24 triangles came back malformed from 120 trails and 29 patches (73 smoothed), its body, legs and stripes all patches. The legs (`28@349 82@354 85@352 74@350 …`) showed the model planning outlines as points and converting each step to a heading. The notation didn't stop polygon thinking; it only added arithmetic, and the errors added up. Smoothing made outlines land where it hadn't planned. The prompt's suggestion of smooth patches for "a body" pulled it away from ellipses, which always land where they're meant to. Straight 3-leg patches did work for isometric faces, but `diamond` and `skew` cover those. A straight-only patch capped at 4 legs was considered and not chosen.

### 4.9 Bent and tapered lines

A v0.3.2 tiger had clumsy body stripes. They were trails like `trail 274 314 26@99 18@126 20@155` at a fixed thickness: thick hooks lying along the back instead of bands down the flank. Stripes on the tail and legs, painted as plain `line x1 y1 x2 y2`, landed well. The model is good at "from here to there" in canvas coordinates and poor at choosing headings for a short stroke that must land somewhere exact. A fixed-width, round-ended stroke also doesn't look like a stripe, which is a wedge.

**Chosen: two line extras from MS Paint.**

- `bend=20@180` (lines only): like Paint's curve tool. The middle of the line is pulled 20 px towards the heading, and both ends stay put. It reuses the compass and the `distance@heading` form from trails, so it needs no left or right handedness, which is easy to get wrong with y pointing down. It renders as a quadratic Bézier whose control point is twice as far out, so the curve's middle lands exactly on the bend. 0 to 256 px; a negative amount is refused with a hint to turn the heading round.
- `taper=end` or `taper=both` (lines and trails): a brush stroke. `end` thins from full width at the start to a point at the end, with a round start; `both` is widest in the middle. It renders as a filled outline built from points along the centre line (the curve sampled, the width at each point set by how far along it is), so fills, spray and soft work as on any shape.
- The prompt now says when to use which: a trail for long wandering strokes (branches, vines, curling tails), a bent line whenever both ends must land in exact places (stripes, eyebrows, smiles, whiskers).
- A bend of 8 px or more gets a note in the paint result saying where the curve passes a quarter, half and three quarters of the way along. In v0.3.3 a tail was a deeply bent line (`bend=66@90`), and its stripes landed 10–13 px off it: the model knows a bent line's ends and middle, but guessed the rest of the curve. Gentler bends stay within a few pixels of the straight line, so they get no note.

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

### 5.3 Reference images

A new painting can come with a reference image: a photo or a picture to recreate, or to use for something else ("my dog as a cartoon", "a seascape in these colours").

- **What it means.** An image alone means "recreate it". With text, Painty paints the text and uses the image the way it says; where the words and the image disagree, the words win. One input, both optional, so the same slot covers copying, reference and style.
- **It picks the canvas.** The browser chooses the canvas shape nearest the image's own (by ratio), crops the middle of the image to it and scales it to the canvas size; changing the shape re-crops. So the reference and the canvas are the same size, and a point in one is the same point in the other. The server checks the image with Pillow (PNG, JPEG or WebP, size limits), crops anything not already canvas-sized and re-encodes it, which drops its metadata.
- **Sent once, in the first message.** The reference goes into the person's opening message as an image, not through a tool. It stays in the history, so every later call and every keep going still has it, and after the first call it is served from the prompt cache. A "look at the reference" tool was rejected: each call would add the image to the conversation again.
- **The same ruler and grid as `look`.** The model sees the reference inside the numbered ruler frame with the 64 px grid, so it can read positions straight off it ("the ear is at about 320, 96") and compare like with like when it looks at its canvas.
- **Independent of vision.** With vision off, Painty sees the reference but never its own canvas: copying from a picture without being able to check the work. The model must accept image input either way.
- **The prompt** gets a "reference image" section (`prompts/reference.md`) only when there is one, so plain drawings are unchanged.

### 5.4 The agent's view: ruler, grid and grid modes

Vision models judge *where* things are far better than they read exact numbers. So the agent's snapshot is the painting inside a numbered ruler frame (from 0 to the canvas width or height every 64 px, on all four sides) with faint grey grid lines every 64 px. The ruler and grid are in the agent's view only, never in the painting, the browser canvas or the gallery PNG. The tool result tells the agent they aren't paint.

**Grid modes (v0.5).** Recreations of reference images often came out slightly offset from the original. One suspect is the frame itself: the agent sees a 572 × 572 image of a 512 × 512 canvas. If a model reads positions as pixels of the image, everything lands about 30 px right and down; if it reads them as fractions of the image, the picture shrinks about 10% towards the middle. The ruler numbers are right either way, but nothing forces the model to use them. So the view is a per-drawing setting, `grid`:

| Mode | What the agent sees | Size |
|---|---|---|
| `frame` (default, as before) | Numbered ruler round the picture, faint grid lines on it | canvas + 60 px each way |
| `overlay` | The same grid lines, with small numbers drawn inside the picture along its top and left edges (on a light halo) | exactly the canvas |
| `none` | The painting as it is | exactly the canvas |

- `overlay` separates the two questions: it keeps the grid's help but removes the size mismatch. Comparing all three on the same reference shows whether the grid helps, or the frame hurts.
- One mode applies to every image the agent sees in a drawing (looks, the reference and the exemplar), so it compares like with like.
- The look tool's docstring is fixed for every drawing, so it doesn't describe the grid; the prompt sections (`GRID_NOTES` in `view.py`, filled into the reference, vision and exemplar sections) and each look's result do. Frozen per drawing, so the prompt cache still works.

### 5.5 The exemplar

The experimental shapes (v0.3) were more expressive but placed badly, lines worst of all: the agent painted, looked, and painted over its own slips, again and again. The exemplar teaches the system up front instead.

- **What it is.** Two practice sheets, 768 × 512 each, of numbered examples (box shapes, rotation and skew on one; lines, bends, trails, compass headings and paint on the other). Each example's drag box is a dashed pink outline, and every point it was written with (box corners, line ends, a bent line's middle, a trail's start and corners) is a pink dot labelled with its coordinates. The first user message lists every example's number, caption and exact shape lines.
- **Why "exemplar".** "Reference" already means the person's own image. The exemplar is Painty's, the same for every drawing.
- **Built from code, cached.** The examples are notation strings in `app/exemplar.py`, painted by the real `Canvas`, labelled with Pillow, then put in the drawing's grid mode, and cached per mode (`lru_cache`). It costs milliseconds once per process. A saved PNG was considered and rejected: it would quietly go stale the next time a shape's rendering changed. A test checks every example is valid on its sheet, and the text is built from the same lines as the picture, so they can't disagree.
- **Sent once, first.** Like the reference, it goes in the opening user message, not through a tool: exemplar text and sheets, then the request, then any reference. It stays in the history and is served from the prompt cache after the first call. The instructions get a short exemplar section (`prompts/exemplar.md`) only when it's on.
- **A toggle, default on**, so runs with and without it can be compared. The gallery saves the sheets as the agent saw them (`exemplar-N.png`), because the exemplar will change while it's being tuned. `/exemplar.png?sheet=1&grid=frame` and `/exemplar.txt` show it for checking by eye.
- **Cost.** Two more high-detail images in every drawing's first call, cached after that.

## 6. Budgets and limits

The book's Ch 9 says every agentic loop needs a goal, an iteration strategy and termination conditions. Painty's termination is layered:

| Limit | Default | Enforced by |
|---|---|---|
| Shapes per drawing | 60 | `paint`; the run pauses at 0 |
| Looks per drawing | 5 | `look` returns an error at 0 |
| Shapes per `paint` call | 15 | `paint`. Stops the agent painting everything in one blind batch, which matters for the vision comparison |
| Hidden turn limit | shapes left + looks left + 10 | the SDK's `max_turns`. A runaway guard: one model call per shape and look is the most a sensible run needs |

All the visible limits are set on screen (not env vars), so they can be changed while experimenting: the drawing's budgets in the New painting dialog, the keep going top-ups in the settings drawer.

### 6.1 Keep going

When the shape budget runs out, the drawing **pauses** rather than halting, because the agent may be nearly done. The UI offers "Keep going" (adds the top-up amounts: default 20 shapes and 2 looks) or "Finish here" (saves as is).

How it works: the agent's `tool_use_behavior` ends the run cleanly when the budget hits 0, so the SDK returns the complete conversation history. "Keep going" tops up the budget and runs the agent again with that history plus a message saying how much more it has. Dynamic instructions show the new totals. Hitting the hidden turn limit also pauses, with the history rebuilt from the exception's run data.

### 6.2 Settings are frozen per drawing

A drawing copies the settings when it starts, so changing them never changes the model, vision or budget under a running agent. Settings are locked while painting. Top-up sizes are read live when "Keep going" is clicked.

## 7. Prompting

- **One prompt per drawing** (v0.1). Free text, up to 500 characters. Each new prompt starts a fresh canvas.
- The instructions describe the drawing's own canvas: its shape, width, height, corners and centre.
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
- **Settings where you use them.** The New painting dialog holds the model, reasoning effort, vision, the exemplar toggle, the grid mode and the drawing's budgets, so a comparison run is set up in the same place it's asked for. The looks field is greyed out with vision off. The settings drawer keeps only the keep going top-ups, which matter mid-drawing. All of them are still one set of saved settings: every change is saved straight away and is remembered for the next painting, and the dialog sends its values again just before `paint`, so a drawing starts with exactly what was shown.
- **New painting dialog.** The top bar has a New painting button (Stop while painting), then a short status line saying what Painty is doing (thinking, painting, looking) or that the connection dropped; the feed has the detail. The button opens a dialog with the prompt (a textarea: Enter paints, Shift+Enter starts a new line), an optional reference image (choose, drop or paste; shown cropped to the canvas) and the canvas shape. It replaced the single-line prompt box in the top bar, which had no room for more, and leaves space for style suggestions. The canvas choice is kept until the page reloads; the prompt and image are cleared once a painting starts.
- **The reference on screen.** Under the canvas, next to the prompt, a small copy of the reference (click to enlarge). In the gallery, its tile shows the reference small in the corner, and the stage viewer can swap the preview to the reference for comparison.
- **Canvas shapes on screen.** The canvas is the biggest of its shape that fits the column. Gallery tiles stay square, with landscape and portrait paintings shown whole inside them.
- **Gallery navigation:** "← All paintings" sits in the header beside ×, so the two can't be confused. × always closes the gallery; Escape steps back one level (enlarged picture, painting, gallery).
- **Gallery** with each painting's prompt, settings, budgets used, top-ups, tokens, time, and links to the PNG, SVG and shape log (as the agent wrote it, and as JSON).
- **Model colours and filters.** Each model has its own colour, shown on its name under every gallery tile and on the painting's page, so you can tell at a glance which model made what. Colours follow the order of `PAINTY_MODELS`, then any other models in the gallery alphabetically, so they stay put while the list does (10 colours, then they repeat). Filter chips above the grid show one or more models (none chosen means all) and favourites only, with counts; the filters are kept while the page is open.
- **Favourites.** A star on each tile and beside the title on a painting's page marks the good ones. "Favourite" rather than "starred": it says what it means. Saved as `favourite` in the painting's `meta.json` (written atomically through `PUT /api/gallery/{id}/favourite`), so it travels with the folder. It is the only part of a painting that changes after it's saved.
- **Stage viewer** in each gallery painting: the finished painting large, with a strip of every stage under it (one per paint call; click or use the arrow keys to load one into the preview). Looks alone don't tell the story: a model often paints several batches in a row without looking, tracking its layers in its head. Stages the agent looked at get a cadmium border and an eye badge, and a toggle under the preview swaps in exactly what it saw (ruler and grid included). Under the preview there is only one short line (stage and shapes), the same height on every stage so the layout never jumps; clicking the preview enlarges it with the full story: Painty's note for that paint call and what it was checking when it looked. Older paintings, which have no stages, show their looks in a list instead.
- Type: Pixelify Sans for the brand (a nod to Matey), Recursive for everything else, with its casual style reserved for Painty's own words (notes and titles).

## 10. Gallery

Each painting is a folder under `DATA_DIR/gallery` (in the container, the `painty-data` named volume at `/srv/data`):

- `painting.png`: clean, at 1024 px, for sharing
- `painting.svg`: the same painting as SVG
- `shapes.txt`: every shape in paint order, one line each, exactly as the agent wrote it ("Shapes" in the gallery)
- `shapes.json`: every shape in paint order as parsed data, with its original `text` ("Shapes (JSON)")
- `looks/look-N.png`: exactly what the agent saw, ruler and grid included
- `reference.png` and `reference-view.png`: the reference image, if one was given, cropped to the canvas, and as the agent saw it (ruler and grid included)
- `exemplar-N.png`: the exemplar sheets as the agent saw them, if it was shown them
- `stages/stage-N.png`: the canvas after each paint call that painted something (canvas size, no grid). Rendered at save time from the first shapes of the canvas, so painting gets no slower. A look at a blank canvas adds a `stage-0.png`
- `meta.json`: prompt, title, note, ending, `canvas` (shape, width and height; missing on older paintings, which are 512 × 512 square), `exemplar_sheets` (the exemplar files saved with it; empty when it wasn't shown, missing on older paintings), `reference` (true when a reference image was given; the prompt may then be empty, meaning recreate it), `favourite` (set from the gallery; missing means no), settings, budgets used, top-ups, `shape_format` (`notation`; missing on paintings from before it, which have no `shapes.txt`), tokens (input with the cached share, output with the reasoning share), time, version, `stages` (each with its first and last shape, note and the looks taken there) and `looks` (each look's focus and how many shapes it saw). Paintings from before stages have neither

Endings: `finished` (the agent called finish), `ended` (it stopped without calling finish, so its last words become the note) and `saved` (you saved it by hand).

## 11. Experiments to try

- The same reference with grid `frame`, `overlay` and `none`: does the offset in recreations follow the frame?
- The exemplar on and off, especially for subjects that need lines, bends and trails ("a tiger with a curling striped tail", "an isometric house with a winding path"). Does it cut the paint-then-repaint loop?

- Same prompt, vision on and off. Does sight make paintings more recognisable, or just more expensive?
- Differences in models and reasoning levels.
- Tight against generous budgets: does a 20-shape limit force better composition?
- Increasing difficulty: face, cat, red cat, stripy blue-and-green tiger, a scene.
- Watch the traces: how does the agent use its looks? Does it correct what it sees?
- Recreate a reference image with vision on and off: how close can it get when it can only see the original?
- The same reference with different instructions: recreate it, restyle it, or borrow only its colours.

## 12. Future ideas (not built)

- **Follow-up prompts on the same canvas** ("now add a volleyball net", "now add people playing"). Parked because of perspective: something "behind" an existing object would mean repainting the object over it. It's interesting, but a bigger problem.
- **SVG fallback (plan B).** If the custom system fails badly, try letting the agent describe shapes in SVG-like terms to see whether familiarity is what made the difference. Deliberately not the default, because it defeats the main test.
- **Hard mode** with an unfamiliar coordinate system (y up, or a non-pixel scale).
- **Final touches inside `finish`.** An optional `shapes` list on `finish`, painted (same checks, all-or-nothing, same budget) before the drawing ends, so the last batch and the finish share one model call instead of two. An invalid batch would paint nothing and not finish. It saves one mostly-cached call and its reasoning per drawing. Parked: the saving is small now caching works, it changes how the feed and logs show the last batch, and with full JSON shapes it would copy the shape schema into every request. Revisit after the compact shape format, when shapes are short strings. Not parallel tool calls: they would break one tool call per turn everywhere (a `look` could run before a `paint` in the same response lands).
- Style suggestions in the New painting dialog.
- Re-running a drawing with the same prompt and reference under other settings, for comparisons.
- A draggable crop for the reference image, instead of always the middle.
- Free three-point triangles, polygons or curves.
- A background colour setting.
- A side-by-side comparison view. 
- Deleting gallery items in the app.
- Local models (for example a vision model on local hardware) through the SDK's model providers.

## 13. Known limitations and risks

- Each look adds an image to the conversation, and the whole conversation is resent on every model call, so token use grows with each look.
- Small models may not follow the coordinate system well; that's part of what's being measured.
- Some models reject `reasoning.effort`; use "Model default" (in the New painting dialog) if so.
- Tracing uploads tool outputs and inputs, including look images and reference images, to OpenAI.
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
| 4 Nov 2026 | Prompt box moved into the top bar, replacing the status line; gallery Back button beside ×, Escape steps back one level |
| 5 Nov 2026 | Canvas shapes: square 512 × 512 (default), landscape 768 × 512, portrait 512 × 768, chosen per drawing; off-canvas allowance per axis |
| 5 Nov 2026 | A New painting dialog replaces the top-bar prompt box, with a status line in its place |
| 5 Nov 2026 | Model, reasoning effort, vision and budgets moved from the settings drawer into the New painting dialog; the drawer keeps the keep going top-ups |
| 5 Nov 2026 | Reference images: optional, with or without text; picks and is cropped to the canvas shape; sent once in the first message inside the ruler and grid; saved in the gallery |
| 5 Nov 2026 | Gallery: a colour per model, filters by model and favourites; favourites saved in each painting's meta.json |
| 6 Nov 2026 | Experimental shapes brought back from the experimental-shapes branch, ported by hand onto main: `diamond`, `skew`, `trail` (with `smooth` and reported corners), `bend`, `taper`, `spray`, `soft`. Close looks left out: every look shows the whole canvas |
| 6 Nov 2026 | The exemplar: two practice sheets built from code by the real renderer, labelled, sent once at the start of the first message; a toggle, default on |
| 6 Nov 2026 | Grid modes (`frame`, `overlay`, `none`) for every image the agent sees, to test whether the ruler frame's size mismatch causes offsets |
