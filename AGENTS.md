# AGENTS.md

Guidance for AI coding agents working on Painty. Read this before changing code. For the why behind each design decision, read `docs/PDD.md`.

## What Painty is

- An experiment: can a language model adapt to a paint system it has never seen, and paint a recognisable picture from a text prompt?
- The agent paints with shape tool calls (line, rectangle, ellipse, triangle, diamond, trail) on a square (512 × 512), landscape (768 × 512) or portrait (512 × 768) canvas, and, with vision on, looks at PNG snapshots of its own work.
- FastAPI backend running the OpenAI Agents SDK. Vanilla HTML/CSS/JS front end. One container. A sibling project to Matey and built the same way.
- Browser and server talk over a single WebSocket (`/ws`).

## Rules for agents

- Never run `git commit` or `git push`. A human reviews and commits all changes.
- Never read, print or edit `.env`. Use `.env.example` to learn variable names.
- Never hard-code secrets, API keys or tokens.
- Ask before adding, removing or upgrading any dependency (Python or JS/CDN).
- Keep changes focused on the task. No drive-by refactors or reformatting.
- Update `README.md` when user-facing behaviour, setup or env vars change. Update `docs/PDD.md` when a design decision changes.
- When adding an env var: add it to `app/config.py`, `.env.example` (placeholder value only) and the README table.
- When adding a setting: add it to `StudioSettings` and `LIMITS` in `app/settings_store.py`, an input marked `data-setting="name"` in `static/index.html` (in the New painting dialog), and a test.
- Run `pytest -q` before saying a change is done.
- Ask before making live model calls beyond a quick smoke test. They cost API usage, and each look sends an image.

## Environment

- Linux shell (WSL2 works). Python 3.12+. The container uses `python:3.13-slim`.
- Always use a project virtual environment (`.venv`). Never install packages system-wide.
- Docker with the Compose plugin. Invoke it as `docker compose`, not `docker-compose`.
- The server must bind to `0.0.0.0`, not `127.0.0.1`, or it's unreachable from outside the container.
- The app is served on port 8091 (Matey uses 8090). Check the port is free before changing it.
- The `painty-data` named volume is mounted at `/srv/data` in the container (gallery and saved settings). Running outside Docker, they go in `./data`.
- Agents may have no browser. Verify with tests and `/healthz`, and leave visual UI checks to the human.

## Commands

```bash
# Setup (once, from the repo root)
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

# Tests: no API key or network needed (scripted fake models)
pytest -q

# Build and run (prompts and static files are baked into the image, so rebuild after any change)
docker compose up -d --build --force-recreate

# Check it works
curl -s http://127.0.0.1:8091/healthz      # expect {"ok":true,"version":"..."}
docker logs --tail 50 painty

# Run without Docker (from the repo root, venv active)
uvicorn app.main:app --reload --port 8091

# Score benchmark paintings (all of them, or one by gallery id)
docker exec painty python -m app.benchmark_score [<id> --diff /srv/data/diff.png]

# Housekeeping: each rebuild leaves an old untagged image behind
docker image prune
```

## Architecture

- `app/main.py`: FastAPI app, static files, `/api/gallery` (and `PUT /api/gallery/{id}/favourite`), the `/gallery/...` files and the `/ws` WebSocket. One `Studio` per connection.
- `app/studio.py`: the drawing lifecycle (start, pause, keep going, stop, save) and the traced agent runs. Runs in a background task so `stop` can cancel it.
- `app/painter.py`: the painter agent, its dynamic instructions, `_stop_when_done` (the `tool_use_behavior` that ends a run on `finish` or an empty shape budget) and `planning_agent` (the forced planning turn).
- `app/tools.py`: the function tools: `plan` (planning on only), `paint`, `look`, `finish`.
- `app/canvas.py`: the shape models (internal only), the shape notation parser, validation and SVG rendering. No AI here.
- `app/view.py`: SVG to PNG (resvg), the agent's view in its grid mode (`frame`, `overlay` or `none`, drawn with Pillow), and reference images (checked, cropped to the canvas, shown in the same grid mode).
- `app/exemplar.py`: the exemplar sheets. Examples written in the notation, painted by the real renderer, labelled in Pillow and cached per grid mode; `exemplar_text()` builds the matching text from the same lines.
- `app/benchmark.py`: the benchmark cards. Pictures written in the notation (items in cells, at graded levels), painted by the real renderer, served at `/benchmark/<name>.png` and `.txt`. A chosen card becomes the drawing's reference, painted by the server, and is saved as `benchmark.json`.
- `app/benchmark_score.py`: scores a benchmark painting against its card, item by item. Run when a benchmark painting is saved (`meta.benchmark_score`, `benchmark-diff.png`), and from the command line (`python -m app.benchmark_score`, `--rescore` to save scores again). Bump `SCORER_VERSION` when the scoring changes. Pillow only.
- `app/gallery.py`: saves finished paintings as plain files under `DATA_DIR/gallery`, including a snapshot of each stage (one per paint call that painted), rendered at save time.
- `app/pricing.py`: the estimated cost of a painting from its tokens and the `PAINTY_PRICE_<MODEL>` prices. Worked out live for `/api/gallery` (each item's `cost`) and the finished status.
- `app/settings_store.py`: the on-screen settings (in the New painting dialog), clamped and saved to `DATA_DIR/settings.json`.
- `app/context.py`: `PaintyContext` (canvas, budget, looks, event helpers), passed to tools and hooks, never sent to the model.
- `app/hooks.py`: `RunHooks` that feed the activity panel.
- `app/prompts/*.md`: prompts, loaded once at import time. `{{NAME}}` placeholders are filled by `prompts.fill()`.
- `app/static/`: `index.html`, `style.css`, `app.js` (socket, canvas, feed, settings, gallery).

## Key design decisions (keep these)

- **A custom paint system, not SVG or CSS.** The whole point is to test adaptation to something unfamiliar. Never give the agent raw SVG, CSS or HTML to write. New abilities are added as fields on `Shape` or `Fill` and an optional `name=value` extra (with a default) in the shape notation.
- **Shapes are written in a one-line notation, not JSON.** `kind x1 y1 x2 y2 fill [name=value extras]`, read by `canvas.parse_shape` into the internal `Shape`. It saves output tokens. Fixed positions only for what every shape has; everything optional is a named extra. Be forgiving about slips (case, commas), never about meaning.
- **Shapes are defined by a drag box.** (x1, y1) and (x2, y2) are opposite corners, in any order (the box is normalised). Lines keep their direction.
- **Rotation is compass-style and shared.** Clockwise degrees around the box centre; a triangle at 0° points up. The same field works for rectangles and ellipses. Linear gradient `angle` uses the same compass convention and turns with the shape.
- **(0, 0) is the top-left, y grows downwards.** Matches SVG, PNG pixels and the agent's snapshot. Don't flip it without changing the prompt, the ruler and the tests together.
- **Paint is permanent.** Shapes are only ever added. No undo, erase, clear or move tools. Mistakes are fixed by painting over.
- **One renderer.** Each shape becomes one self-contained SVG fragment (`canvas.shape_svg`). The browser shows those fragments; the backend joins the same fragments and rasterises them for looks and the gallery. Never re-implement shape drawing in JavaScript.
- **Batches, one tool call per turn.** `paint` takes an ordered list, and `parallel_tool_calls=False`, so layering is always in the order the agent planned. `paint_lock` is a backstop.
- **Batches are painted up to the first bad shape.** The shapes before it are painted; it and everything after it are not, and cost no budget, so a resend lands in the planned order. The result lists every problem in the batch, numbered within the batch. Never paint shapes after a bad one: that breaks the layering.
- **A reference image goes in the first user message, once.** In the same grid mode as `look`, cropped to the canvas size. Never through a tool (each call would add the image again). No text with it means recreate it.
- **The exemplar goes first in the first user message, once.** Its text and sheets, then the prompt, then any reference. Built from code (`app/exemplar.py`), never a saved PNG, so it can't drift from the renderer. Every example must pass `check_batch` on the sheet (tested). A toggle, for comparison.
- **The same model sees its own work.** `look` returns the PNG to the painter itself via `ToolOutputImage`. Don't route images through a separate describing model (the book's Ch 7 critic pattern): that's what this project avoids.
- **Benchmark budgets are fixed per card.** The card sets the shape and look budgets for that drawing (the saved settings keep your own) and there are no top-ups, so runs compare fairly.
- **Benchmark cards are written in the notation.** Painted by the real renderer, never a saved image, so a perfect copy is always possible and the score measures the model, not the paint system. No card line may repeat an exemplar line (tested). The server paints the chosen card itself, so the reference is exact, and saves the card's definition with the painting, so scoring uses the card as it was.
- **The grid is only in the agent's view.** `view.agent_view` applies the drawing's grid mode: `frame` (a numbered ruler frame and faint grid lines every 64 px; the image is 60 px bigger than the canvas), `overlay` (the same grid and small numbers drawn on the picture, exactly canvas-sized) or `none`. It applies to looks, the reference and the exemplar alike. The painting, the browser canvas and the gallery PNG never have a grid. Prompts describe the view through `GRID_NOTES`, never hard-coded ruler wording.
- **Vision is a toggle, for comparison.** With vision off, the `look` tool is not registered at all and the prompt says it is painting blind.
- **Planning is a toggle, for comparison.** With it on, the start segment's first turn is the same painter, cloned with `tool_choice="plan"` and the planning effort, stopping after `plan`. The painter then carries on from that history. Instructions and tools are identical in both, so the cache prefix is shared. With it off, the `plan` tool and the prompt section don't exist. Planning prompts and docstrings stay general about layering: never name the specific subjects used to test it.
- **Budgets live in tools, not prompts alone.** Shape and look budgets are enforced in code. The per-call cap stops the agent dumping everything down in one blind batch.
- **Pausing ends the run cleanly.** When the shape budget hits 0, or the agent calls `look` with no looks left (`wants_look`; not in a benchmark), `_stop_when_done` ends the run with complete history. "Keep going" adds the shapes and looks typed on the paused card (either may be 0, but there must be a shape left) and runs again with that history plus a user message. The hidden turn limit (`shapes_left + looks_left + 10`) is a runaway guard; hitting it also pauses, rebuilding history from the exception.
- **Keep the prompt cacheable.** The instructions must be identical on every model call within a run, because they come before the whole history and the cache only reuses an exact prefix. Never put live counts in them; they belong in tool results. Never edit earlier history items in place. The drawing id is the `prompt_cache_key`.
- **Settings are frozen per drawing.** A drawing copies the settings when it starts. Settings are locked while painting. Keep going top-ups are not settings: they're typed each time.
- **State.** Drawing state is per connection and in memory (refresh = fresh studio). Only the gallery and settings persist, as plain files in `DATA_DIR`. No database.
- **Tracing.** Each run segment (start, each keep going) is one trace, all grouped by the drawing id. `OPENAI_AGENTS_DISABLE_TRACING=true` turns it off.
- **No front-end build step.** Plain ES modules, no framework, no bundler. Only external resource: Google Fonts.

## Conventions

- Tool docstrings, `app/prompts/*.md` and the problem messages from `parse_shape` and `check_shape` are read by the model. Treat vague wording as a bug. The `Shape` model's field descriptions are internal only.
- Tools return errors as `"Error: ..."` strings instead of raising, so the model can recover.
- Every successful state-changing tool must change the context (canvas or budget), emit its event (`shapes` or `look`) and call `send_budget()`.
- Tool results are plain English and always say what is left in the budget. Paint results then end with the canvas record (`tools.canvas_record`): what is on the canvas, batch by batch, from the agent's own notes.
- Never use module-level mutable state for anything about a drawing. Settings and gallery are app-wide on purpose (on `app.state`).
- Prompts longer than a few lines go in `app/prompts/*.md`. Short inline prompts use implicit string concatenation, not triple-quoted strings (avoids stray indentation).
- Version lives in three places, keep them in sync: `app/__init__.py`, `pyproject.toml`, and the image tag in `compose.yaml`.

## Adding a shape kind or paint option

1. `canvas.py`: add the field or `kind` value, how `parse_shape` reads it (a new extra goes in `EXTRAS`, with a default), its checks in `check_shape`, and its rendering in `shape_svg`.
2. `prompts/painter.md`: explain how to write it, when it helps and its default.
3. `exemplar.py`: add an example to a sheet (12 cells per sheet), and look at it at `/exemplar.png`.
4. `hooks.py`: only if the feed should describe it differently.
5. `tests/test_canvas.py`: a rendering test that checks real pixels, a notation test, and a validation test.
6. `docs/PDD.md`: record the decision.

## WebSocket protocol

- Browser to server:
  - `{"type": "paint", "prompt": "...", "canvas": "square", "reference": "data:image/jpeg;base64,..."}`: start a new drawing (max 500 characters). `canvas` is `square`, `landscape` or `portrait`; anything else means square. `reference` is optional; with it, `prompt` may be empty (recreate the image). `benchmark` (a card name) instead makes that card the reference and sets the canvas shape and the shape and look budgets (fixed per card, no keep going); an unknown card is refused.
  - `{"type": "keep_going", "shapes": 20, "looks": 2}`: add that many shapes and looks to a paused drawing (clamped to `TOP_UP_LIMITS`; looks ignored when painting blind) and carry on. Refused if it would leave no shapes.
  - `{"type": "stop"}`: cancel the running drawing.
  - `{"type": "save"}`: save a paused, stopped or failed drawing to the gallery.
  - `{"type": "settings", "settings": {...}}`: update settings (refused while painting).
- Server to browser:
  - `hello`: `version`, `canvases` (`{shape: [width, height]}`), `settings`, `options` (models, efforts, grids, limits, top_up_limits), `benchmarks` (`[{name, title, about, shape, shapes, shape_budget, look_budget}]`) and `benchmark_prompt`.
  - `canvas_reset`: a new drawing started (`prompt`, `reference` (a PNG data URL or null), `benchmark` (a card name or null), `settings`, `shape`, `width`, `height`). Clear and resize the canvas, and clear the feed.
  - `shapes`: `shapes` as `[{n, svg}]` in paint order, plus `key` (the tool call id).
  - `look`: `key`, `n`, `focus` and `image` (a PNG data URL of exactly what the agent saw).
  - `budget`: shapes and looks used, total and left, and `top_ups`.
  - `status`: `state` of `painting`, `paused` (`reason`, `top_up`: the suggested `shapes` and `looks` to add (`looks` null when painting blind), or null for a benchmark, which can't keep going), `finished` (`title`, `note`, `ending`, `saved_id`, `meta`, `benchmark_score` (null unless a scored benchmark), `tokens`, `cost`), `stopped` or `error` (`reason`).
  - `phase`: `thinking`, `paint`, `look` or `finish`, shown in the top bar's status line while painting.
  - `activity`: feed entries. Tool entries share a `key` and go from `running` to `done` or `error`. An optional `image` (a data URL) shows as a thumbnail, such as the reference image as Painty sees it.
  - `settings`: the current settings after an update.
  - `error`: a short message for a toast.
- Changing an event's shape means updating `app.js` and the tests in the same change.

## Testing

- Tests use the SDK's `agents.testing.ScriptedModel`. Each step scripts the model's tool calls, so no API key is needed.
- `make_studio(tmp_path, steps, **settings)` in `tests/test_studio.py` builds a studio with a scripted model and a temporary gallery.
- Canvas tests render real PNGs and check pixels, so layering, rotation and gradients are tested for real.
- Add a scripted regression test for any agent-behaviour bug you fix in code.
- Prompt quality can't be unit-tested. Describe what to try manually, so a human can check the live app and traces.

## Known pitfalls

- Prompt or static edits do nothing until the image is rebuilt (`--build`). `.env` changes need `--force-recreate`.
- `docker run` fails if a container named `painty` already exists. Use `docker compose`.
- `docker compose down -v` deletes the `painty-data` volume, and with it the whole gallery. Use plain `docker compose down`.
- Some models reject `reasoning.effort`. Set Reasoning effort to "Model default" in the New painting dialog if a model errors.
- Models in `PAINTY_MODELS` must accept image input for vision mode.
- The CSS has `[hidden] { display: none !important; }` because component `display` rules otherwise override the `hidden` attribute.
- `/favicon.ico` needs its explicit route in `main.py`.
- Every look adds an image to the conversation, and the whole conversation is resent on each model call, so token use grows with each look.
