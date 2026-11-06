# Painty

**Can an AI agent learn to paint with a tool it has never seen?**

Painty gives an agent a blank canvas (square, landscape or portrait) and six shapes: line, rectangle, ellipse, triangle, diamond and trail. Click **New painting**, pick the canvas shape and type what you want, such as *"a ginger cat sitting in the sun"* or *"a stripy tiger with blue and green stripes"*. The agent paints it one tool call at a time, layering shapes like real paint. With vision on, it can stop and look at its own work, then fix what it doesn't like.

It's an experiment in agent adaptation. The agent can't fall back on SVG or CSS it memorised in training. It has to learn a new, custom paint system from its instructions and turn what it knows about cats, faces and tigers into coordinates and layers.

## Key features

- **A custom paint system.** Shapes are drawn by "dragging" from one corner to another, like Microsoft Paint. They can be turned to any angle or skewed, filled with flat colour or linear and radial blends, given outlines, sprayed on as speckle, feathered at the edges, and made see-through for shading and highlights. Lines can be bent into curves and tapered like brush strokes, and a trail walks a stroke leg by leg by compass heading.
- **An exemplar to learn from.** Before it paints, Painty can be shown two practice sheets of numbered examples, painted by the same renderer and labelled with their boxes and coordinates, each listed with the exact shape text that drew it.
- **Three canvas shapes.** Square (512 × 512), landscape (768 × 512) or portrait (512 × 768), chosen for each painting.
- **Reference images.** Add a picture to a new painting. On its own, Painty tries to recreate it; with text, it uses it the way you say ("my dog as a cartoon", "a seascape in these colours"). The image picks the canvas shape for you.
- **Paint is permanent.** Every shape covers what's beneath it. There's no undo or eraser, so mistakes are fixed by painting over them.
- **The agent can see its own work.** Each look sends a PNG snapshot to the *same* model doing the painting, with a ruler and grid in its view to help it map what it sees to coordinates. You choose the grid: a ruler frame round the picture, a grid drawn on the picture (so it stays exactly canvas-sized), or none.
- **Vision on or off.** Run the same prompt with and without sight and compare the results.
- **Budgets you control.** Shape and look budgets, plus a cap on shapes per call, set in the New painting dialog alongside the model and vision.
- **Keep going.** When the budget runs out, the drawing pauses. Give it more and the agent carries on exactly where it stopped.
- **Watch it work.** Shapes appear live, layer by layer. A feed shows every batch, the agent's notes, and a thumbnail of everything it looked at.
- **Gallery.** Every finished painting is saved with its prompt, settings, shapes, looks and token use, ready to compare or share. Step through a snapshot of every stage (one per paint call): the stages the agent looked at are marked with an eye, and can switch to exactly what it saw, grid and all. Each model has its own colour, you can filter by model, and you can star your favourites and show only those.
- **Full tracing.** Every drawing appears in the OpenAI Traces dashboard.

## Tech stack

- Python, [OpenAI Agents SDK](https://github.com/openai/openai-agents-python), FastAPI, Uvicorn
- [resvg](https://github.com/linebender/resvg) (via `resvg-py`) to turn the canvas SVG into PNG, and Pillow for the agent's ruler-and-grid view
- WebSockets for live updates
- Vanilla HTML, CSS and JavaScript (no build step)
- Docker and Docker Compose
- pytest, using the SDK's scripted fake models (no API key needed)

## Quick start

You need Docker with the Compose plugin and an OpenAI API key.

1. Clone the repo:
   ```bash
   git clone https://github.com/Oliver-J-Carr/Painty.git && cd Painty
   ```
2. Create your settings file:
   ```bash
   cp .env.example .env
   ```
3. Edit `.env` and set `OPENAI_API_KEY`.
4. Build and start:
   ```bash
   docker compose up -d --build
   ```
5. Open <http://127.0.0.1:8091>, click **New painting** and ask Painty for a picture.

Paintings and settings are kept in the `painty-data` Docker volume, so they survive rebuilds. Download any painting from the gallery, or copy the whole gallery out with `docker cp painty:/srv/data/gallery ./gallery-export`.

To run the tests: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest -q`

## Settings

These are set on screen and saved between restarts. Changes apply to the next drawing. Everything but the keep going top-ups is in the **New painting** dialog, so you can change it as you ask for a picture; the top-ups are under **Settings**.

| Setting | Default | What it does |
|---|---|---|
| Model | `gpt-5.4-mini` | The model that paints. The choices come from `PAINTY_MODELS` |
| Reasoning effort | Medium | How hard the model thinks before each step. "Model default" leaves it unset |
| Let Painty see the canvas | On | Gives the agent the `look` tool. Off means it paints blind |
| Show Painty the exemplar sheet first | On | Sends the two exemplar sheets and their shape text in the first message. See them at `/exemplar.png?sheet=1&grid=frame` and `/exemplar.txt` |
| Grid on what Painty sees | Ruler frame | How every image Painty sees (its looks, the reference and the exemplar) marks positions: **Ruler frame** (numbered margin round the picture, 60 px bigger than the canvas), **Grid on the image** (lines and small numbers drawn on the picture, exactly canvas-sized) or **None** |
| Shapes | 60 | Shapes per drawing |
| Looks | 5 | Looks per drawing |
| Most shapes in one paint call | 15 | Stops the agent painting everything in one blind batch |
| Keep going adds (Settings) | 20 shapes, 2 looks | What "Keep going" gives a paused drawing |

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes | Calls the models and uploads traces |
| `PAINTY_MODELS` | No | Models offered in the settings panel, comma separated. The first is the default. They must accept image input for vision and reference images |
| `TRACE_WORKFLOW_NAME` | No | Name shown for each drawing in OpenAI Traces |
| `OPENAI_AGENTS_DISABLE_TRACING` | No | Set to `true` to stop sending traces to OpenAI |
| `DATA_DIR` | No | Where the gallery and saved settings live. Defaults to `data` (`/srv/data` in the container, which is the `painty-data` volume) |

See `.env.example` for defaults.

## How it works

The design and the reasons behind it are in [`docs/PDD.md`](docs/PDD.md). In short:

- The agent has three tools: `paint` (an ordered batch of shapes, each written as one short line such as `ellipse 196 180 316 300 #f2c9a0`), `look` (a PNG of the canvas, if vision is on) and `finish` (a title and a note).
- Every shape becomes one SVG fragment. Your browser shows those fragments, and the server rasterises the very same SVG for the agent's eyes and the gallery, so what you see is exactly what the agent sees.
- Budgets are enforced in code. When shapes run out, the run ends cleanly with its history kept, so "Keep going" can resume the same conversation.

## Roadmap

- Multi-step scenes: add to a finished painting with a follow-up prompt ("now add people playing volleyball").
- A "hard mode" with an unfamiliar coordinate system, such as y pointing up.
- More shapes or paint options, such as free three-point triangles.
- Deleting paintings from the gallery in the app.
- A side-by-side comparison view for vision on and off.
- Style suggestions when starting a painting.

## Licence

MIT
