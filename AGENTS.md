# AGENTS.md

Guidance for AI coding agents working on Painty. Read this before changing code. For the why behind each design decision, read `docs/PDD.md`.

## What Painty is

- An experiment: can a language model adapt to a paint system it has never seen, and paint a recognisable picture from a text prompt?
- The agent paints with shape tool calls (line, rectangle, ellipse, triangle) on a fixed 512 × 512 canvas, and, with vision on, looks at PNG snapshots of its own work.
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
- When adding a setting to the on-screen panel: add it to `StudioSettings` and `LIMITS` in `app/settings_store.py`, the form in `static/index.html`, and a test.
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

# Housekeeping: each rebuild leaves an old untagged image behind
docker image prune
```

## Architecture

- `app/main.py`: FastAPI app, static files, `/api/gallery`, the `/gallery/...` files and the `/ws` WebSocket. One `Studio` per connection.
- `app/studio.py`: the drawing lifecycle (start, pause, keep going, stop, save) and the traced agent runs. Runs in a background task so `stop` can cancel it.
- `app/painter.py`: the painter agent, its dynamic instructions and `_stop_when_done` (the `tool_use_behavior` that ends a run on `finish` or an empty shape budget).
- `app/tools.py`: the three function tools: `paint`, `look`, `finish`.
- `app/canvas.py`: the shape models (also the tool schema), validation and SVG rendering. No AI here.
- `app/view.py`: SVG to PNG (resvg), and the agent's view with its ruler frame and grid (Pillow).
- `app/gallery.py`: saves finished paintings as plain files under `DATA_DIR/gallery`.
- `app/settings_store.py`: the settings panel's values, clamped and saved to `DATA_DIR/settings.json`.
- `app/context.py`: `PaintyContext` (canvas, budget, looks, event helpers), passed to tools and hooks, never sent to the model.
- `app/hooks.py`: `RunHooks` that feed the activity panel.
- `app/prompts/*.md`: prompts, loaded once at import time. `{{NAME}}` placeholders are filled by `prompts.fill()`.
- `app/static/`: `index.html`, `style.css`, `app.js` (socket, canvas, feed, settings, gallery).

## Key design decisions (keep these)

- **A custom paint system, not SVG or CSS.** The whole point is to test adaptation to something unfamiliar. Never give the agent raw SVG, CSS or HTML to write. New abilities are added as structured fields on `Shape` or `Fill`.
- **Shapes are defined by a drag box.** `start` and `end` are opposite corners, in any order (the box is normalised). Lines keep their direction.
- **Rotation is compass-style and shared.** Clockwise degrees around the box centre; a triangle at 0° points up. The same field works for rectangles and ellipses. Linear gradient `angle` uses the same compass convention and turns with the shape.
- **(0, 0) is the top-left, y grows downwards.** Matches SVG, PNG pixels and the agent's snapshot. Don't flip it without changing the prompt, the ruler and the tests together.
- **Paint is permanent.** Shapes are only ever added. No undo, erase, clear or move tools. Mistakes are fixed by painting over.
- **One renderer.** Each shape becomes one self-contained SVG fragment (`canvas.shape_svg`). The browser shows those fragments; the backend joins the same fragments and rasterises them for looks and the gallery. Never re-implement shape drawing in JavaScript.
- **Batches, one tool call per turn.** `paint` takes an ordered list, and `parallel_tool_calls=False`, so layering is always in the order the agent planned. `paint_lock` is a backstop.
- **All-or-nothing batches.** If any shape in a batch is invalid, nothing is painted and the batch costs no budget. The error lists every problem, numbered within the batch.
- **The same model sees its own work.** `look` returns the PNG to the painter itself via `ToolOutputImage`. Don't route images through a separate describing model (the book's Ch 7 critic pattern): that's what this project avoids.
- **The grid is only in the agent's view.** `view.agent_view_png` adds a numbered ruler frame and faint grid lines every 64 px. The painting, the browser canvas and the gallery PNG never have them.
- **Vision is a toggle, for comparison.** With vision off, the `look` tool is not registered at all and the prompt says it is painting blind.
- **Budgets live in tools, not prompts alone.** Shape and look budgets are enforced in code. The per-call cap stops the agent dumping everything down in one blind batch.
- **Pausing ends the run cleanly.** When the shape budget hits 0, `_stop_when_done` ends the run with complete history. "Keep going" tops up the budget and runs again with that history plus a user message. The hidden turn limit (`shapes_left + looks_left + 10`) is a runaway guard; hitting it also pauses, rebuilding history from the exception.
- **Keep the prompt cacheable.** The instructions must be identical on every model call within a run, because they come before the whole history and the cache only reuses an exact prefix. Never put live counts in them; they belong in tool results. Never edit earlier history items in place. The drawing id is the `prompt_cache_key`.
- **Settings are frozen per drawing.** A drawing copies the settings when it starts. Only the top-up sizes are read live, when "Keep going" is clicked. Settings are locked while painting.
- **State.** Drawing state is per connection and in memory (refresh = fresh studio). Only the gallery and settings persist, as plain files in `DATA_DIR`. No database.
- **Tracing.** Each run segment (start, each keep going) is one trace, all grouped by the drawing id. `OPENAI_AGENTS_DISABLE_TRACING=true` turns it off.
- **No front-end build step.** Plain ES modules, no framework, no bundler. Only external resource: Google Fonts.

## Conventions

- Tool docstrings and `Field(description=...)` text on the shape models are sent to the model. Treat vague wording as a bug.
- Tools return errors as `"Error: ..."` strings instead of raising, so the model can recover.
- Every successful state-changing tool must change the context (canvas or budget), emit its event (`shapes` or `look`) and call `send_budget()`.
- Tool results are plain English and always end with what is left in the budget.
- Never use module-level mutable state for anything about a drawing. Settings and gallery are app-wide on purpose (on `app.state`).
- Prompts longer than a few lines go in `app/prompts/*.md`. Short inline prompts use implicit string concatenation, not triple-quoted strings (avoids stray indentation).
- Version lives in three places, keep them in sync: `app/__init__.py`, `pyproject.toml`, and the image tag in `compose.yaml`.

## Adding a shape kind or paint option

1. `canvas.py`: add the field or `kind` value with a precise description, its checks in `check_shape`, and its rendering in `shape_svg`.
2. `prompts/painter.md`: explain how it works and when it helps.
3. `hooks.py`: only if the feed should describe it differently.
4. `tests/test_canvas.py`: a rendering test that checks real pixels, plus a validation test.
5. `docs/PDD.md`: record the decision.

## WebSocket protocol

- Browser to server:
  - `{"type": "paint", "prompt": "..."}`: start a new drawing (max 500 characters).
  - `{"type": "keep_going"}`: top up a paused drawing and carry on.
  - `{"type": "stop"}`: cancel the running drawing.
  - `{"type": "save"}`: save a paused, stopped or failed drawing to the gallery.
  - `{"type": "settings", "settings": {...}}`: update settings (refused while painting).
- Server to browser:
  - `hello`: `version`, `canvas_size`, `settings`, `options` (models, efforts, limits).
  - `canvas_reset`: a new drawing started (`prompt`, `settings`). Clear the canvas and feed.
  - `shapes`: `shapes` as `[{n, svg}]` in paint order, plus `key` (the tool call id).
  - `look`: `key`, `n`, `focus` and `image` (a PNG data URL of exactly what the agent saw).
  - `budget`: shapes and looks used, total and left, and `top_ups`.
  - `status`: `state` of `painting`, `paused` (`reason`, `top_up`), `finished` (`title`, `note`, `ending`, `saved_id`, `meta`, `tokens`), `stopped` or `error` (`reason`).
  - `phase`: `thinking`, `paint`, `look` or `finish`, for the status line.
  - `activity`: feed entries. Tool entries share a `key` and go from `running` to `done` or `error`.
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
- Some models reject `reasoning.effort`. Set Reasoning effort to "Model default" in the settings panel if a model errors.
- Models in `PAINTY_MODELS` must accept image input for vision mode.
- The CSS has `[hidden] { display: none !important; }` because component `display` rules otherwise override the `hidden` attribute.
- `/favicon.ico` needs its explicit route in `main.py`.
- Every look adds an image to the conversation, and the whole conversation is resent on each model call, so token use grows with each look.
