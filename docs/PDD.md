# Painty: project design document

Version 0.11.0, 11 November 2026. Owner: Oliver Carr.

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

Out of scope for now (see section 12): follow-up prompts on the same canvas, raw SVG mode, user drawing or editing, multiple users.

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
| `paint` | `shapes` (ordered list of shape notation strings, bottom layer first), `note` (the parts this batch paints) | Validates and paints a batch, up to any bad shape. Returns what was painted, any problems, what's left in the budget and the canvas record (section 7) |
| `look` | `focus` (what it wants to check) | Returns a PNG of the canvas to the same model. Vision on only |
| `plan` | `plan` (text: composition, parts in paint order, overlaps, stages) | Records the agent's plan. Costs no budget. Planning on only (section 5.7) |
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

### 5.6 The benchmark

Watching single paintings shows *that* a model struggles, not how much or where. The benchmark gives a repeatable measure: Painty reproduces a known picture as exactly as it can, and the copy is scored against it, so models and settings can be compared on numbers.

- **Cards, written in the notation.** Each card is a picture in Painty's own shape notation (`app/benchmark.py`), painted by the real renderer. So every part of it can be reproduced exactly with the paint tool. A perfect copy is possible, and any gap is the model's, not the paint system's. We also have the true shapes, and a card can't drift from the renderer (the same reason the exemplar is built from code). A photo or a hand-drawn test image was rejected: a perfect copy might not be possible, and the score would mix up "can't see it" with "can't paint it".
- **Graded.** Five cards by level, plus one combined: basic (rectangles and circles of different sizes, a thin bar, an outline, a line), intermediate (rotation at odd angles, skew, a diagonal blend), solids (an isometric cube, a cylinder, a cone), effects (a shaded sphere, see-through circles, a glow, spray, a taper, a bend) and letters (HAZE in straight lines, SOUP with a smooth trail and bent lines). Separate cards show which level breaks a model; the combined card tests everything in one run. Every card fits the default shape budget.
- **Nothing from the exemplar.** The cards use different angles, skews, proportions and colours, and no card line repeats an exemplar line (tested), so a model can't score by copying a line it was shown.
- **Items in cells.** Each card is a set of items, each in its own cell with a margin, so it can be scored item by item. A test checks every item paints inside its cell.
- **Picked in the New painting dialog.** Choosing a card loads it into the reference slot, sets the canvas shape and fills in a standard prompt (`BENCHMARK_PROMPT`, editable), so runs are comparable. The `paint` message names the card, and the server paints the card itself rather than using the browser's JPEG crop, so Painty gets exact pixels. Otherwise it's an ordinary reference: sent once, in the drawing's grid mode. The card's definition is saved with the painting (`benchmark.json`), so it is scored against the card as it was, even after the cards change. Paint again re-picks the card.
- **Scoring** (`app/benchmark_score.py`, a command line for now; Pillow only). The card (`reference.png`) is compared with the painting (`painting.svg` rendered at canvas size). A pixel has paint when it is more than 24 from white in any channel. Edges get 1 px of grace (`EDGE_TOLERANCE`): a pixel only counts as missing or extra if the other image has no paint within 1 px, and its colour only counts as wrong if it is outside the range of colours within 1 px in the other image, because an anti-aliased edge is a blend of them. Per item: **score** (the share of painted pixels in the right place with colours matching within 40 per channel: the headline number), **shape** (the same without colour), **colour** (how close the colours are where both have paint), **offset** (the shift within ±32 px that best lines the copy up) and **shifted** (the shape IoU at that offset). Offset and shifted separate "right shape, wrong place" from "wrong shape". The whole card gets the mean item score, a whole-canvas score (stray paint included) and a global offset, which measures directly the drift that grid modes (section 5.4) were added to investigate.
- **A fixed budget per card.** A card sets the drawing's shape and look budgets, and they are locked in the dialog. They're the same on every run, so scores compare fairly, and a model can't buy a better score with a bigger budget. Shapes are `SHAPE_ALLOWANCE` (3) times the card's own plus `SHAPE_SPARE` (10), rounded up to 5. That's generous on purpose: paint is permanent, so every correction after a look is a new shape painted over the mistake, and none can be won back. Twice the card's shapes was tried first and judged too tight. Looks are `BENCHMARK_LOOKS` (3). Painting it right first time is the ideal, but seeing and correcting mistakes is within the project's theme, so a few looks are allowed. Forbidding looks would make vision pointless. Unlimited looks would reward trial and error over planning. There are no top-ups: a paused benchmark can only finish there (the server refuses keep going). Vision stays a choice, so blind and sighted runs of the same card can be compared. With vision off, there are no looks. The server overrides the budget for that drawing only, and the saved settings keep your own values.
- **Why edges get grace.** The first scorer used exact pixels, and each score is relative to the item's own area, so thin shapes, which are nearly all edge, were marked down far too hard. A perfect copy moved 1 px diagonally scored 66% on the combined card's tapered stroke but 91% on its skewed rectangle, and a visibly wrong skew (2,626 px out) outscored a near match on the stroke (636 px out). With 1 px of grace that shift scores about 100% everywhere. 2 px was rejected: a stroke a third too thick reached 98%. The offset search still uses exact overlap, so offsets stay precise. In the difference picture every pixel that isn't grey now counts against the score, with wrong colour in amber, so the picture and the number agree.
- **The combined card's "bent" stroke isn't visibly bent.** Its `bend=36@45` pulls almost along the line (heading about 46°), so it stays straight and only its taper changes. A plain line with the right ends, colour and thickness was scoring about 81% before the edge grace. Left as it is on purpose: changing it would make new runs of that card incomparable with old ones.
- **Spray is blurred before scoring.** Its speckle is seeded with the shape's number, so a perfect copy painted in a different order speckles differently. The sprayed item is box-blurred on both sides first (`Item.blur`).
- **Scored when saved, shown in the app.** A benchmark painting is scored as it's saved (off the event loop; a scoring failure never stops the save). The score goes in `meta.json` (`benchmark_score`, with `scorer`, a version number) and the difference picture in `benchmark-diff.png`. The finished card shows the score with the best and weakest items. The gallery tile shows a score badge, and the painting's page has the score, a table of items and the difference picture. Saving rather than scoring live (as the cost is) because scoring takes about a second a painting, too slow for every gallery listing. The trade-off is that a change to the scorer leaves old scores stale until `python -m app.benchmark_score --rescore` writes them again. `scorer` says which version made each one.
- Not built yet: a side-by-side comparison of runs, and a gallery filter for benchmark paintings.

### 5.7 Planning

Without it, the only planning instruction is one line ("Before painting, plan the composition in pixels"). Whatever plan the model makes lives in its hidden reasoning. Reasoning items probably go back in with the history, but they can't be seen or checked, and the only written trace of a plan was each paint call's short `note`. Paint is permanent, so a part painted in the wrong order can't be fixed by moving it, only by painting over. The question: does writing a plan down first, especially what is in front of what, make paintings better?

- **A toggle, default off**, so the plain painter stays the baseline and runs can be compared.
- **A forced first turn of the same agent.** With planning on, the first model call is the painter itself, cloned with `tool_choice="plan"`, the planning reasoning effort, and `StopAtTools(["plan"])` (`painter.planning_agent`). The normal painter then carries on from that history, the way keep going does. The instructions, tool list and first message are identical, so the second call reuses the prompt cache the first one filled (exemplar and reference images included). A separate planner agent was rejected. It would send the images again, break "the same model does the work", and its plan would be someone else's.
- **Its own reasoning effort** (`planning_effort`, default high), because planning may want more thought than each paint call.
- **The plan is written into the conversation**, as the `plan` tool's argument. So every later call, and every keep going, has it in front of it, and it is saved (`plan.txt`) for study. The tool asks for five parts: composition; depth (each prominent area's parts from furthest to nearest, naming every opening, hollow or frame and what is inside or seen through it); parts in paint order with rough boxes and colours; an overlap check (for each pair that touches or overlaps, which is nearer, and that it comes later); and stages. The prompt section is `prompts/planning.md`.
- **Depth is reasoned about in three dimensions, not by importance.** In the first test runs the model planned and still painted a frame-like part before what sits inside it, so the inside looked stuck on top. The planning section and the tool now ask for a furthest-to-nearest list per area, single out openings and frames (what is inside or seen through goes first, the rim after so it overlaps), ask for a pairwise check that the later part really is nearer, and ask the painter to check each batch against the plan's depth order. The examples given are generic (a window, a cup, a picture frame), never the held-out subjects.
- **Revisions allowed.** The painter keeps the `plan` tool and may call it again if the plan itself turns out wrong. Each revision is saved with how many shapes were on the canvas then. Only the start segment's first turn is forced; keep going never re-plans by itself.
- **A turn without a plan doesn't stop the drawing.** If the model answers in text, it is told "Now paint it." and paints without a plan, and the feed says so. An empty plan is refused with an error, and it may plan again.
- **Held-out tests.** The prompt and the tool's wording stay general about layering. They never name the specific subjects used to test whether planning fixes layering mistakes, so a fix can't come from the wording itself.
- **Models that refuse forced tool calls.** Claude Sonnet 5.5 and Opus 5.5 answer a forced `tool_choice` with a 400, on every host tried, though the OpenRouter catalogue lists `tool_choice` for Sonnet 5.5, so it can't be known in advance. A 400 that mentions `tool_choice` on the planning turn repeats the turn unforced, with only the prompt's "your first call is `plan`" asking for it. The feed says so, and `plan_forced` in `meta.json` records it, because a requested plan and a forced plan aren't quite the same experiment.

### 5.8 Models through OpenRouter

OpenRouter makes hundreds of models available through one API, including Claude, Gemma and Qwen. That widens the research question from OpenAI's models to almost any multimodal model.

**One rule picks the route: a model id with a `/` in it goes through OpenRouter, and anything else goes direct to OpenAI.** Every OpenRouter id has one and no OpenAI id does. A model stays one plain string, so the gallery, colours, filters, Paint again and saved settings all work unchanged. A separate `provider` setting would have touched all of them to say the same thing. `gpt-6-luna` and `openai/gpt-6-luna` count as separate models, which allows a comparison of the two routes.

**Same framework, same API shape.** The SDK's `OpenAIResponsesModel` runs with an `AsyncOpenAI` client pointed at OpenRouter's Responses API (`app/providers.py`). OpenRouter's `function_call_output` takes images, so `look`, the first message and the history needed no changes. Rejected:

- **Chat Completions or LiteLLM.** The SDK's Chat Completions converter strips images from tool results by default, so `look` could quietly return nothing.
- **A model name string.** The SDK reads the prefix itself: it would strip `openai/` and refuse `anthropic/`. So OpenRouter models are passed as `Model` objects.
- **`set_default_openai_client`.** It would also move tracing and the native models.

**Tested live on 8 Nov 2026** (a few cents, through the real SDK path). A hidden random code in an image returned by a tool was read back by Gemma 4 31B, Gemma 4 26B A4B, Claude Haiku 5.5 and `openai/gpt-6-luna`. Qwen 3.8 Flash read it with 1 of 6 characters wrong, and read it correctly in later tests of images in the first message. Gemma, Qwen and Haiku all ran whole paintings with planning, a look and keep going, replaying their reasoning. Sonnet 5.5 painted but returned no reasoning items, so replaying its reasoning is untested.

**The model library (`app/model_library.py`).** Prices and models were `.env` variables, which doesn't scale to hundreds. A browser in the New painting dialog lists OpenRouter's public catalogue (`GET /api/v1/models`, no key needed, cached for an hour), and adding a model saves it and its price to `DATA_DIR/models.json`.

- Models with `tools` and the Responses endpoint are listed, which excludes the `:batch` variants: 317 of 461 on 10 Nov 2026. Of those, 212 take images and 105 are text-only. Text-only models were left out at first, since looks, the exemplar and references need images. They are now listed alongside the rest, each marked with an eye or a crossed-out eye, with a Sees images only filter (off by default) to hide them, because a model such as GLM 5.3 may still paint well blind (see Capabilities below). Negative prices mark routers such as `openrouter/auto`, which are left out because a comparison needs a known model.
- `PAINTY_MODELS` still comes first, so the default doesn't change, and `PAINTY_PRICE_*` overrides the catalogue's price.
- Removing a model hides it but keeps its price, so older paintings stay priced.
- Prices refresh with the catalogue, in line with "costs use the current prices".

**Sorting by score.** With hundreds of models on offer, the browser can sort by how good a model is likely to be. The chosen score shows on each row.

- **Most popular.** OpenRouter has no popularity number, only `sort=most-popular`. The catalogue is fetched in that order, which returns the same models as the unsorted list, so one request gives both, and a model's place among the usable models is its rank. Everything else is sorted in the browser.
- **Intelligence, Agentic and Design Arena (SVG).** These come with each catalogue entry (`benchmarks`). Design Arena rates a model per category with no overall number, and SVG is the closest to painting from code. On 8 Nov 2026, of 208 usable models, 45 had an Intelligence score, 66 Agentic and 55 a Design Arena SVG rating. Models without the chosen score go last, in popularity order, and the status line says how many have it. These scores measure general ability, and nothing yet shows they predict good painting.
- **Painty score.** The model's best score on your own runs of the combined card (`benchmark_score.best_scores`), measuring painting directly. One card, because cards differ in difficulty. The best rather than the mean, because many runs are deliberately handicapped (no vision, say), though it does favour models with more runs, so the row gives the count. Only scores from the current scorer count. A direct model such as `gpt-6-luna` isn't in OpenRouter's catalogue and `openai/gpt-6-luna` counts as a separate model, so direct runs don't show here.

**Capabilities: what each model can see and how it reasons.** The catalogue says whether a model takes images (`architecture.input_modalities`) and how it reasons (`supported_parameters` and `reasoning`: `supported_efforts`, `default_effort`, `mandatory`, `default_enabled`). `model_library.reasoning_options` turns that into the efforts the dialog offers, lowest first, the default marked "(default)":

- 148 models list their efforts and a default (Claude Sonnet 5.5: low to max, default high). A model that must reason is never offered "Off". A model that only reasons when asked defaults to Off.
- About 100 reason but list no efforts (Gemma 4, Qwen 3.8 Flash). They get Low, Medium and High, which OpenRouter turns into a thinking budget, plus their default sent as no effort at all ("On (default)" or "Off (default)").
- 68 can't reason. The dialog locks the control as "Unsupported" and no effort is sent.
- Choosing a model chooses its default. A model run directly on OpenAI takes its capabilities from the same model on OpenRouter (`gpt-6-luna` from `openai/gpt-6-luna`); while the catalogue hasn't been fetched, it gets the old choices.
- The server fits each drawing to its model (`studio._fit_model`), for that drawing only: an effort the model doesn't take becomes its default, and a text-only model paints blind with no exemplar, saying so in the feed. A reference image or benchmark card with a text-only model is refused.

Why: one list of efforts for every model hid that they differ, and the shared default ("medium") didn't mean the same thing everywhere. It is OpenAI's default but one level below Claude Sonnet and Opus 5.5's (high). For Claude, effort sets how thorough it is overall, not only how much it thinks. On a reference recreation at medium, Sonnet 5.5 didn't think at all, painted 12 shapes in one call with the note "dog" and finished. At high it thought first, painted 25 shapes, looked, and corrected. That was the only technical cause found for Claude's weak results. Instructions, earlier tool calls and their results all reach it through OpenRouter, checked with a hidden word, recalled numbers and a receipt code. Even at its default, Claude painted well below GPT-6.1-Sol and Gemini 4 Argon. Opus paints over its own work deliberately (its notes say so), correcting depth-order slips after looking. The capabilities say only what the catalogue says: `gpt-5.4` defaults to Off there, which may not be OpenAI's own default.

**Billed cost.** OpenRouter returns `usage.cost` on every response, kept through `preserve_raw_usage` and added up in the `on_llm_end` hook as `billed_usd`. The estimate from listed prices matched the bill for Qwen, Haiku and Sonnet, to the cent, cache writes included, but not for models whose hosts charge different prices: a Gemma 4 31B painting estimated at $0.0096 was billed $0.0056, and its hosts charge $0.10 to $0.75 per million input tokens. So billed is shown when present. The estimate stays for native models, whose API doesn't report a charge.

**Caching.** Anthropic's cache is opt-in, so `anthropic/` models send a top-level `cache_control`. Gemma and Qwen cache without asking. The existing `prompt_cache_key` is also OpenRouter's fallback for sticky routing, keeping a drawing on one host. On Claude, going from the planning turn to the painter misses the cache and pays one extra write. Gemma's cache hits are patchy.

**One tool at a time.** The catalogue doesn't list `parallel_tool_calls` for any of the models tested. No model has sent two calls in one turn so far, but paint order is the picture, so the runner's `max_function_tool_concurrency=1` runs any such calls in order. `provider.require_parameters` is deliberately not set, because it could leave no host to route to.

## 6. Budgets and limits

The book's Ch 9 says every agentic loop needs a goal, an iteration strategy and termination conditions. Painty's termination is layered:

| Limit | Default | Enforced by |
|---|---|---|
| Shapes per drawing | 60 | `paint`; the run pauses at 0 |
| Looks per drawing | 5 | `look` returns an error at 0, and the run pauses to ask for more (a benchmark carries on) |
| Shapes per `paint` call | 15 | `paint`. Stops the agent painting everything in one blind batch, which matters for the vision comparison |
| Hidden turn limit | shapes left + looks left + 10 | the SDK's `max_turns`. A runaway guard: one model call per shape and look is the most a sensible run needs |

All the visible limits are set on screen (not env vars), so they can be changed while experimenting: the drawing's budgets in the New painting dialog, and what to add each time a drawing pauses on the paused card.

### 6.1 Keep going

When the shape budget runs out, the drawing **pauses** rather than halting, because the agent may be nearly done. It also pauses when the agent tries to look with no looks left: it wanted to check something, so you decide whether it gets to. Running out of looks alone doesn't pause it, since it may not want another. The paused card asks how many shapes and looks to add, filled in with a suggestion for whatever ran out (20 shapes, 2 looks) and 0 for the rest, then offers "Keep going" or "Finish here" (saves as is).

The amounts are typed each time, not fixed settings. Fixed top-ups in a settings drawer were easy to forget to change, and the right amount depends on the painting. Either may be 0 (keep going without more looks, say), as long as the agent has a shape left. With 0 looks left, the keep going message tells it to carry on from what it already knows. A benchmark never pauses on a refused look: its budget is fixed, so it carries on.

How it works: the agent's `tool_use_behavior` ends the run cleanly when the shape budget hits 0 or a look was refused, so the SDK returns the complete conversation history. "Keep going" tops up the budget and runs the agent again with that history plus a message saying how much more it has. Dynamic instructions show the new totals. Hitting the hidden turn limit also pauses, with the history rebuilt from the exception's run data.

### 6.2 Settings are frozen per drawing

A drawing copies the settings when it starts, so changing them never changes the model, vision or budget under a running agent. Settings are locked while painting.

## 7. Prompting

- **One prompt per drawing** (v0.1). Free text, up to 500 characters. Each new prompt starts a fresh canvas.
- The instructions describe the drawing's own canvas: its shape, width, height, corners and centre.
- The system prompt (`app/prompts/painter.md`) teaches the paint system, coordinates, layering, budget, a working method (plan in pixels, paint back to front in stages, look after each main stage but not after the final touches) and to always end with `finish`. It has a vision-on and a vision-off section.
- The agent works on its own and never asks questions.
- Each `paint` call carries a short `note` naming the parts of the picture it paints, shown in the feed so viewers can follow the plan.
- **The canvas record.** Every paint result ends with a list of what is on the canvas, batch by batch in paint order, from the agent's own notes (`shapes 1 to 3: sky and sea`, and `(cut short by a problem)` when a bad shape stopped a batch; long notes shortened to 120 characters). Before it, the agent's only record of what was done was its earlier paint calls, scattered back through the history as raw shape lines, so working out which parts were finished meant reading them back. The record puts it in one place, as the most recent thing in the conversation. It is built from `ctx.stages`, adds a line per batch to each result (a few hundred tokens over a drawing, all cached after their first call), and is always on, not a toggle. The gallery keeps the plain notes.
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
- **Paused card** asking how many shapes and looks to add (looks hidden when painting blind), with Keep going and Finish here. **Finished card** with title, note, shape and token counts, and a link to the gallery.
- **Model browser.** A Browse OpenRouter models button by the model choice opens a dialog over the New painting dialog: search (every word must match the name or id), Free only and Added only toggles, and sorting by newest, most popular, cheapest, name or a score (section 5.8). Each row has the prices, the context length and Add or Remove. Adding a model also chooses it. The model choice groups models into OpenAI (direct) and OpenRouter. Free models carry a note about rate limits and retained prompts, because reference images go with them.
- **Settings where you use them.** The New painting dialog holds the model, reasoning effort, vision, the exemplar toggle, planning and its effort, the grid mode and the drawing's budgets, so a comparison run is set up in the same place it's asked for. The looks field is greyed out with vision off. There is no separate settings drawer: its last job, the keep going top-ups, moved onto the paused card. The settings are one saved set: every change is saved straight away and is remembered for the next painting, and the dialog sends its values again just before `paint`, so a drawing starts with exactly what was shown.
- **New painting dialog.** The top bar has a New painting button (Stop while painting), then a short status line saying what Painty is doing (thinking, painting, looking) or that the connection dropped; the feed has the detail. The button opens a dialog with the prompt (a textarea: Enter paints, Shift+Enter starts a new line), an optional reference image (choose, drop or paste; shown cropped to the canvas) and the canvas shape. It replaced the single-line prompt box in the top bar, which had no room for more, and leaves space for style suggestions. The canvas choice is kept until the page reloads; the prompt and image are cleared once a painting starts.
- **Paint again.** The finished card and each gallery painting have a Paint again button. It opens the New painting dialog filled in with that painting's prompt, reference image, canvas shape and painter and budget settings (the starting budgets, not the top-ups), rather than painting straight away, so one thing can be changed for a comparison. The filled-in settings are only a suggestion until Paint is pressed: closing the dialog clears the prompt and image and puts the saved settings back. Paintings from before grid modes and the exemplar are re-run with the ruler frame and no exemplar, as they were made. A model no longer in `PAINTY_MODELS` is swapped for the current one, and the dialog says so. Front end only: it sends the same `settings` and `paint` messages as a new painting, and the gallery's `reference.png` is already canvas-sized, so it isn't cropped again.
- **Replay.** Each gallery painting has a Replay button beside Paint again. It closes the gallery and paints the painting again on the studio canvas, the one new paintings use, rather than in a separate viewer, so it looks exactly as it did live. Shapes go down one by one at the live pace (`SHAPE_GAP_MS`), with a pause before each batch and after each look. The feed is rebuilt as it went: each batch with its note, each look with what Painty saw (`looks/look-N.png`), each plan (`plan.txt`) and the finish. The meters count up. A replay card under the canvas offers Skip to end, Replay again and View in gallery, and a playback speed (1×, 2×, 4×, 8× or 16×) that divides every delay, so a 300-shape painting takes about 3 seconds at 16× instead of 45. Speed changes apply from the next shape, never touch live painting, and are remembered in the browser's local storage (a per-viewer convenience; blocked storage just means 1×). Front end only: `painting.svg` already holds every shape as its own self-contained `<g data-n>` fragment, exactly as it was painted, so the browser splits it and needs no new endpoint. It replays the original even if the renderer has changed since (re-rendering `shapes.json` would not), and older paintings without stages replay their shapes alone. A test checks every fragment stands alone. Replay is refused while Painty is painting or paused, and when a stopped drawing hasn't been saved, because the canvas would hide it. A new painting ends a replay.
- **The reference on screen.** Under the canvas, next to the prompt, a small copy of the reference (click to enlarge). In the gallery, its tile shows the reference small in the corner, and the stage viewer can swap the preview to the reference for comparison.
- **Canvas shapes on screen.** The canvas is the biggest of its shape that fits the column. Gallery tiles stay square, with landscape and portrait paintings shown whole inside them.
- **Gallery navigation:** "← All paintings" sits in the header beside ×, so the two can't be confused. × always closes the gallery; Escape steps back one level (enlarged picture, painting, gallery).
- **Gallery** with each painting's prompt, settings, budgets used, top-ups, tokens, time, and links to the PNG, SVG and shape log (as the agent wrote it, and as JSON).
- **Model colours and filters.** Each model has its own colour, shown on its name under every gallery tile and on the painting's page, so you can tell at a glance which model made what. Colours follow the order of `PAINTY_MODELS`, then any other models in the gallery alphabetically, so they stay put while the list does (10 colours, then they repeat). Filter chips above the grid show one or more developers (none chosen means all), favourites only and benchmarks only. A flat chip per model stopped working once OpenRouter made hundreds available, so developers are the top level, alphabetical so they're easy to find. Choosing one adds a row of chips for its models, with their colours; none chosen means all of that developer's. Each developer narrows separately, so one OpenAI model can be compared with everything from Anthropic. The developer comes from the model id: the part before the `/` (without the `~` of OpenRouter's "latest" aliases), or OpenAI when there isn't one, so `gpt-6-luna` and `openai/gpt-6-luna` sit under OpenAI as two models and the two routes can still be compared. The filters stack, and the developers are either-or among themselves. Each chip's count says how many paintings it would show alongside the other active filters (faceted counts): with Favourites on, a model's chip counts that model's favourites. Developer and model chips' counts ignore what else is chosen among them, since choosing one adds to it. A developer or model whose count drops to 0 stays listed, so the chips don't jump about. The filters are kept while the page is open.
- **Favourites.** A star on each tile and beside the title on a painting's page marks the good ones. "Favourite" rather than "starred": it says what it means. Saved as `favourite` in the painting's `meta.json` (written atomically through `PUT /api/gallery/{id}/favourite`), so it travels with the folder. It and a benchmark score written again by `--rescore` are the only parts of a painting that change after it's saved.
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
- `plan.txt`: every plan the agent wrote, in order, if planning was on (section 5.7)
- `benchmark.json`: the benchmark card, if the reference was one (section 5.6), for scoring
- `benchmark-diff.png`: card, painting and difference side by side, with each item's score
- `stages/stage-N.png`: the canvas after each paint call that painted something (canvas size, no grid). Rendered at save time from the first shapes of the canvas, so painting gets no slower. A look at a blank canvas adds a `stage-0.png`
- `meta.json`: prompt, title, note, ending, `canvas` (shape, width and height; missing on older paintings, which are 512 × 512 square), `exemplar_sheets` (the exemplar files saved with it; empty when it wasn't shown, missing on older paintings), `reference` (true when a reference image was given; the prompt may then be empty, meaning recreate it), `benchmark` (the card's name, when the reference was a benchmark card; missing otherwise), `benchmark_score` (its score, item by item, worked out when saved and written again by `--rescore`), `favourite` (set from the gallery; missing means no), settings, budgets used, top-ups, `shape_format` (`notation`; missing on paintings from before it, which have no `shapes.txt`), tokens (input with the cached and cache write shares, output with the reasoning share; `cache_write_tokens` is missing on older paintings), time, version, `plans` (each plan's number and how many shapes were on the canvas when it was written; empty when planning was off, missing on older paintings), `plan_forced` (planning on only: false when the model refused a forced tool call), `billed_usd` (what OpenRouter charged; missing on native models and older paintings), `stages` (each with its first and last shape, note and the looks taken there) and `looks` (each look's focus and how many shapes it saw). Paintings from before stages have neither

**Estimated cost.** Each painting's page shows a cost, worked out from its tokens and the model's price in `.env` (`PAINTY_PRICE_<MODEL>=input/cached/output`, US dollars per million tokens; `app/pricing.py`) or, for a model added from OpenRouter, the catalogue's price. A painting made through OpenRouter shows what was billed instead, with the estimate on hover (section 5.8).

- One variable per model, named from the model (`gpt-6.1-sol` → `PAINTY_PRICE_GPT_6_1_SOL`), so any model in the gallery can be priced, even one no longer offered.
- Worked out live by `/api/gallery`, never saved in `meta.json`, so older paintings get a cost too. The trade-off: changing a price changes every painting's cost.
- Exact from the totals: pricing is linear. Input tokens include images, cache reads and cache writes, and each input token is billed at one rate: plain input, cached or cache write. Output includes reasoning.
- Cache writes: models from GPT-5.6 on (all the gpt-6 models) bill tokens written to the prompt cache at 1.25 times input (the default 30-minute cache), so the price takes an optional fourth number. Painty saves `cache_write_tokens` per painting; older paintings have none, so their writes count as plain input, a slight underestimate.
- Not modelled: the long-context rates (double, for a request over 272K input tokens). A Painty request is normally far smaller.
- Tokens are counted as each model call ends (the `on_llm_end` hook), not from the run's result. A stopped or failed run has no result, so before this a stopped-then-saved drawing lost every call in its last segment.

**Export and import.** A painting can leave the gallery three ways, from an Export button with a download symbol on its gallery page and on the finished card: the painting alone (`painting.png`), a card, or a zip of every file. Every download is named from the title Painty gave it and the model, like `Sunny Face - openai-gpt-6-luna.png` (the `/` in an OpenRouter id becomes `-`), because a folder of exports is otherwise a pile of `painting.png`s. The server builds each one (`GET /api/gallery/{id}/export/{png|card|zip}`, `app/transfer.py`) and names it in `Content-Disposition`.

- **The card** (`app/card.py`) is for sharing: the painting full size (2×) in a dark frame with a paper mount, hung on the cutting mat, with a paper label under it like a gallery's: title, "Painted by" the model and effort, the prompt in quotes, then shapes, cost and any benchmark score, and under another line the tokens in (with the share cached) and out (with the reasoning) in the same columns, as the gallery shows them. A painting made from a reference image shows it small over the frame's bottom right corner with a white border, as a gallery tile does, sitting mostly on the frame so it hides little of the painting. The card is as wide as the framed painting, so each canvas shape gets a card of its own shape, and the label wraps to it. Prompts run to 500 characters, so the prompt is cut at 4 lines with an ellipsis; a long prompt never makes the card taller. Cost is the billed amount, or the estimate marked `~`. Drawn with Pillow on the server rather than a browser canvas: it can be tested and checked by eye without a browser, and works for any painting. The trade-off is Pillow's built-in font rather than the app's Recursive and Pixelify Sans, which would mean shipping font files.
- **The zip** holds every file in the folder, inside one folder named like the download. **Import** (a button in the gallery's header, several zips at once) adds it back as a gallery painting, here or in another Painty, keeping its id unless that is taken, and adding `imported_at` to its meta. A painting already in the gallery (same title, saved at the same moment) is refused and shown instead.
- **Imports are someone else's files**, and the gallery serves them from Painty's own address, so a script in an SVG would run as Painty. So only the files a painting folder holds are taken (anything else is skipped, and no path can leave the folder); sizes and the file count are capped as it unpacks; every PNG must really be a PNG; and `painting.svg` may hold only the elements and attributes Painty's renderer writes, with `url()` only to its own gradients and filters. The allow lists match what `canvas.shape_svg` writes (tested), and every painting in the gallery when it was added passed. It is unpacked beside the gallery and moved in whole, so it's never listed half done. Imported benchmark paintings count towards the model browser's Painty score like your own.
- **The menu floats over the page** (`position: fixed`, placed from the button), because the painting page's details column scrolls on its own and clipped a menu inside it. It opens below the button unless there's more room above (the finished card sits low under the canvas), moves left when it would run off the right, never makes anything scroll, and closes on scroll.

**Deleting.** A Delete button on a painting's page (`DELETE /api/gallery/{id}`) removes its folder and every file in it, after a second press to confirm, which suggests exporting it first. There is no bin: the folder is moved out of the gallery, so it's never listed half deleted, then removed.

Endings: `finished` (the agent called finish), `ended` (it stopped without calling finish, so its last words become the note) and `saved` (you saved it by hand).

## 11. Experiments to try

- The same reference with grid `frame`, `overlay` and `none`: does the offset in recreations follow the frame?
- The exemplar on and off, especially for subjects that need lines, bends and trails ("a tiger with a curling striped tail", "an isometric house with a winding path"). Does it cut the paint-then-repaint loop?

- The benchmark (section 5.6): every card with each model, then the same model with vision, the exemplar and each grid mode in turn. Which levels does each model pass, and does the global offset follow the grid mode?
- Planning on and off (section 5.7), same prompt and model, especially subjects where one part must be painted before another that partly covers it. Does a written plan get the layering right more often, and is it worth its extra call? Try the planning effort at low and high too.
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
- A draggable crop for the reference image, instead of always the middle.
- Free three-point triangles, polygons or curves.
- A background colour setting.
- A side-by-side comparison view. 
- Local models (for example a vision model on local hardware) through the SDK's model providers.
- Recording or pinning which OpenRouter host served a drawing. Hosts can differ for open models, and nothing records which one ran.

## 13. Known limitations and risks

- Each look adds an image to the conversation, and the whole conversation is resent on every model call, so token use grows with each look.
- Small models may not follow the coordinate system well; that's part of what's being measured.
- Reasoning efforts come from OpenRouter's catalogue. A model it describes wrongly may reject an effort it was offered; choose its default if so.
- The cost is an estimate: it uses today's prices in `.env`, not what was charged at the time, and a model call still running when you press Stop isn't counted.
- Tracing uploads tool outputs and inputs, including look images and reference images, to OpenAI. This includes runs through OpenRouter whenever `OPENAI_API_KEY` is set. Without the key, the SDK skips the upload and logs a warning.
- Models from OpenRouter vary widely. A weak model may end a drawing early by answering in text (saved with the ending `ended`). Free models have rate limits, and some may retain prompts and images.
- The native OpenAI route was checked by the scripted tests, not a live run, when OpenRouter support was added.
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
| 6 Nov 2026 | Paint again, from the finished card and the gallery: opens the New painting dialog filled in with the painting's prompt, reference, canvas and settings, to change before painting |
| 6 Nov 2026 | Estimated cost per painting from per-model prices in `.env` (one `PAINTY_PRICE_<MODEL>` each), worked out live so older paintings are priced too; tokens counted per model call so stopped drawings keep theirs |
| 6 Nov 2026 | Benchmark: graded cards written in the notation and painted by the real renderer, picked in the New painting dialog, painted by the server as the reference, saved with the painting and scored item by item from the command line (score, shape, colour, offset) |
| 6 Nov 2026 | Gallery: a Benchmarks filter, and chip counts that stack on the other active filters |
| 6 Nov 2026 | Benchmark paintings scored when saved: score in meta.json, difference picture saved, shown on the finished card, the gallery tile and the painting's page; `--rescore` to update |
| 6 Nov 2026 | Benchmark budgets fixed per card and locked: three times the card's shapes plus 10 (rounded up to 5) and 3 looks, no top-ups; vision stays a choice |
| 6 Nov 2026 | Keep going amounts typed on the paused card each time, not fixed settings; a refused look pauses to ask for more looks (not in a benchmark); the settings drawer removed |
| 7 Nov 2026 | Optional planning: a forced first turn of the same agent calls a `plan` tool at its own reasoning effort; the plan stays in the conversation, can be revised, and is saved as `plan.txt`. Default off, for comparison |
| 7 Nov 2026 | Every paint result ends with the canvas record, built from the agent's notes; `note` now names the parts each batch paints |
| 7 Nov 2026 | Replay: a gallery painting painted again on the studio canvas, shape by shape, with its feed rebuilt; split from `painting.svg` in the browser |
| 8 Nov 2026 | Benchmark scorer v2: 1 px of grace on edges, for shape and colour, because exact pixels marked thin shapes down far too hard; wrong colour shown in the difference picture. The combined card is unchanged, so runs stay comparable |
| 8 Nov 2026 | Gallery filters: developers as the top level, with each chosen developer's models as sub-filters |
| 8 Nov 2026 | Model browser sorts: most popular, Intelligence, Agentic, Design Arena (SVG) and Painty score, with the score on each row |
| 10 Nov 2026 | Reasoning effort choices per model from OpenRouter's catalogue, the model's default marked and chosen with it, "Unsupported" when it can't reason; text-only models offered, painting blind with no exemplar or reference. Claude's weak results traced to the shared "medium" being below its default |
| 11 Nov 2026 | Export a painting as a PNG, a card (framed, with its details, drawn with Pillow) or a zip of every file, named from its title and model; import zips back into the gallery, checked file by file; delete paintings from their page, after confirming |
| 8 Nov 2026 | OpenRouter alongside OpenAI: a `/` in the model id picks the route, through OpenRouter's Responses API with the same SDK. Models and prices added in the app from its catalogue, saved in `models.json`; billed cost recorded; tools run one at a time; planning falls back when a model refuses forced tool calls |
