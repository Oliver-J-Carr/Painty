# Painty

**Can an AI agent learn to paint with a tool it has never seen?**

Painty gives an agent a blank canvas (square, landscape or portrait) and six shapes: line, rectangle, ellipse, triangle, diamond and trail. Click **New painting**, pick the canvas shape and type what you want, such as *"a ginger cat sitting in the sun"* or *"a stripy tiger with blue and green stripes"*. The agent paints it one tool call at a time, layering shapes like real paint. With vision on, it can stop and look at its own work, then fix what it doesn't like.

It's an experiment in agent adaptation. The agent can't fall back on SVG or CSS it memorised in training. It has to learn a new, custom paint system from its instructions and turn what it knows about cats, faces and tigers into coordinates and layers.

## Key features

- **A custom paint system.** Shapes are drawn by "dragging" from one corner to another, like Microsoft Paint. They can be turned to any angle or skewed, filled with flat colour or linear and radial blends, given outlines, sprayed on as speckle, feathered at the edges, and made see-through for shading and highlights. Lines can be bent into curves and tapered like brush strokes, and a trail walks a stroke leg by leg by compass heading.
- **An exemplar to learn from.** Before it paints, Painty can be shown two practice sheets of numbered examples, painted by the same renderer and labelled with their boxes and coordinates, each listed with the exact shape text that drew it.
- **Three canvas shapes.** Square (512 × 512), landscape (768 × 512) or portrait (512 × 768), chosen for each painting.
- **Reference images.** Add a picture to a new painting. On its own, Painty tries to recreate it; with text, it uses it the way you say ("my dog as a cartoon", "a seascape in these colours"). The image picks the canvas shape for you.
- **A benchmark.** Pick a benchmark card in the New painting dialog and Painty tries to reproduce it exactly. The cards are graded: basic shapes, rotation and skew, 3D solids, shading and effects, and letters made from lines, plus one combined card. Then score the result item by item, to compare models and settings.
- **Paint is permanent.** Every shape covers what's beneath it. There's no undo or eraser, so mistakes are fixed by painting over them.
- **The agent can see its own work.** Each look sends a PNG snapshot to the *same* model doing the painting, with a ruler and grid in its view to help it map what it sees to coordinates. You choose the grid: a ruler frame round the picture, a grid drawn on the picture (so it stays exactly canvas-sized), or none.
- **Vision on or off.** Run the same prompt with and without sight and compare the results.
- **Plan first, or not.** Turn on planning and Painty's first move is to write down a plan: the parts in paint order, back to front, and what sits in front of what. It has its own reasoning effort. The plan shows in the feed, stays in Painty's conversation for the whole drawing, and is saved with the painting. Painty can revise it as it goes.
- **A shape budget you control,** set in the New painting dialog alongside the model and vision. Painty can look at its canvas as often as it likes, and the feed counts its looks.
- **Paint again.** Re-run a painting from its finished card or from the gallery. The New painting dialog opens filled in with its prompt, reference image, canvas and settings, so you can change one thing and compare.
- **Export and import.** The Export button on a gallery painting, and on the finished card, downloads the painting as a PNG, as a card (the painting framed, with any reference image small on its corner, and its title, model, prompt, shapes, cost, and tokens in and out underneath, shaped to the canvas) or as a zip of every file. Each download is named from the painting's title and model. Import, in the gallery, adds exported zips back, here or in another Painty. Delete, on a painting's page, removes it and its files for good, after asking.
- **Replay.** Press Replay on a gallery painting and it's painted again on the studio canvas, shape by shape, with each batch's note, every look and any plan appearing in the feed at the point it happened. Speed it up to 2×, 4×, 8× or 16× while it plays (remembered in your browser), or skip to the end at any time.
- **Asking for more.** A paint call that goes past the budget is still painted in full (its shapes are already written and paid for). Near the end, with 10 or fewer shapes left, Painty can ask for more: how many (in tens) and what for. The drawing pauses and you give it the shapes (the amount is yours to change) or say no, and then it finishes and titles the painting.
- **Watch it work.** Shapes appear live, layer by layer. A feed shows every batch, the agent's notes, and a thumbnail of everything it looked at.
- **Gallery.** Every finished painting is saved with its prompt, settings, shapes, looks, token use and its cost (billed by OpenRouter, or estimated from prices), ready to compare or share. Step through a snapshot of every stage (one per paint call): the stages the agent looked at are marked with an eye, and can switch to exactly what it saw, grid and all. Each model has its own colour, and you can star your favourites. Filters for developers and favourites stack, and each one's count shows how many paintings it would add to what's showing. Choosing a developer shows its models as sub-filters.
- **Benchmarks.** Benchmark paintings have a section of their own, opened with Benchmarks in the top bar. It works like the gallery, with the same filters and a chip for each card, and each tile names its card and shows its score.
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
3. Edit `.env` and set `OPENAI_API_KEY`, `OPENROUTER_API_KEY` or both. See [Models](#models).
4. Build and start:
   ```bash
   docker compose up -d --build
   ```
5. Open <http://127.0.0.1:8091>, click **New painting** and ask Painty for a picture.

Paintings and settings are kept in the `painty-data` Docker volume, so they survive rebuilds. Export any painting from the gallery (a PNG, a card or a zip of every file), import a zip back with Import, or copy the whole gallery out with `docker cp painty:/srv/data/gallery ./gallery-export`.

To run the tests: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest -q`

## Settings

These are set on screen and saved between restarts. Changes apply to the next drawing. They're all in the **New painting** dialog, so you can change them as you ask for a picture. How many shapes to give when Painty asks for more isn't a setting: you type it on the paused card each time (it's filled in with what Painty asked for).

| Setting | Default | What it does |
|---|---|---|
| Model | First of `PAINTY_MODELS` | The model that paints. The choices are `PAINTY_MODELS` plus any you add with **Browse OpenRouter models** |
| Reasoning effort | The model's own | How hard the model thinks before each step. The choices are the chosen model's own, from OpenRouter's catalogue, with its default marked "(default)" and chosen when you pick the model. A model that can't reason shows "Unsupported". Models run directly on OpenAI take theirs from the same model on OpenRouter. Levels mean different things to different families: Claude's effort also sets how thorough it is overall, not just how long it thinks |
| Let Painty see the canvas | On | Gives the agent the `look` tool. Off means it paints blind. Locked off for a text-only model |
| Show Painty the exemplar sheet first | On | Locked off for a text-only model. Sends the two exemplar sheets and their shape text in the first message. See them at `/exemplar.png?sheet=1&grid=frame` and `/exemplar.txt` |
| Grid on what Painty sees | Grid on the image | How every image Painty sees (its looks, the reference and the exemplar) marks positions: **Grid on the image** (lines and small numbers drawn on the picture, exactly canvas-sized), **Ruler frame** (numbered margin round the picture, 60 px bigger than the canvas) or **None**. Models, smaller ones especially, tend to do better when the image is exactly the canvas's size |
| Let Painty write a plan before painting | Off | Painty's first turn is forced to call `plan`: composition, parts in paint order with boxes and colours, which parts overlap and which is in front, and stages. The plan costs no shapes, stays in the conversation, can be revised later, and is saved as `plan.txt` in the gallery. Some models, such as Claude Sonnet 5.5 and Opus 5.5, refuse forced tool calls, so they are only asked to plan, and the feed and `plan_forced` in `meta.json` say so |
| Planning effort | The model's own | The reasoning effort for that planning turn only. Later turns use Reasoning effort. Set to the model's default whenever you pick a model, even while planning is off, so it's ready when you turn planning on. Greyed out while planning is off |
| Shapes | 60 | The shapes Painty aims to fit the painting in. It may ask for more near the end |

## Models

Painty can run a model two ways, and you can use both side by side:

- **Directly on OpenAI**, for a model id such as `gpt-6-luna`. Needs `OPENAI_API_KEY`.
- **Through OpenRouter**, for any model id with a `/` in it, such as `google/gemma-4-31b-it` or `anthropic/claude-sonnet-5.5`. Needs `OPENROUTER_API_KEY`.

To add an OpenRouter model, click **Browse OpenRouter models** in the New painting dialog, search, and click **Add**. Its prices come with it, so it needs nothing in `.env`. The browser lists models that can call tools and use OpenRouter's Responses API, with each one's reasoning levels and whether it sees images. Text-only models, such as GLM 5.3, are listed alongside the rest, marked with a crossed-out eye; tick **Sees images only** to hide them. A text-only model paints blind, with no exemplar sheets, reference image or benchmark card; the dialog locks those options and keeps your own for the next model. Added models are saved in `models.json` in the data folder.

You can sort the list by newest, most popular, cheapest or name, and by score:

- **Intelligence** and **Agentic**: the Artificial Analysis indices.
- **Design Arena (SVG)**: the model's ELO in Design Arena's SVG category.
- **Painty score**: the model's best score on your own runs of the combined benchmark card.

Many models have no third-party score, and those go last.

`gpt-6-luna` and `openai/gpt-6-luna` count as two models, so you can compare the two routes.

For a painting made through OpenRouter, the gallery shows what OpenRouter billed. Other paintings show an estimate from the prices. An estimate can differ from the bill for an open model, because its hosts charge different prices. A price set in `.env` overrides the catalogue's price for estimates.

Without `OPENAI_API_KEY`, traces aren't uploaded, and the SDK logs a warning about it. Set `OPENAI_AGENTS_DISABLE_TRACING=true` to turn tracing off cleanly. With the key set, OpenRouter runs are traced to OpenAI too, images included.

## Benchmark

Each benchmark card is a picture painted by Painty's own renderer from shapes in its own notation, so a perfect copy is possible with the tools Painty has. None of it repeats the exemplar.

| Card | What it tests |
|---|---|
| Basic shapes | Rectangles and circles of different sizes, a thin bar, an outline and a line |
| Rotation, skew and blends | Shapes turned to odd angles, two skews and a diagonal blend |
| Solids | An isometric cube, a cylinder and a cone |
| Effects | A shaded sphere, see-through circles, a glow, spray, a tapered stroke and a bent arc |
| Letters | HAZE in straight lines, and SOUP, which needs curves |
| Combined | A little of each, on one card |

To run one, open **New painting** and choose a card under **Reference image**. It sets the canvas shape and fills in a standard prompt (you can edit it). It also sets and locks the budget, the same on every run so scores compare fairly: three times the card's own shapes plus 10 (rounded up to 5), because every correction is a new shape painted over the mistake, and 3 looks. A benchmark can't go over its budget or ask for more: a paint call past it is refused, and when its shapes run out, Painty finishes. Vision is still your choice, so you can compare blind runs (no looks) with sighted ones. See a card at `/benchmark/<name>.png` and its shapes at `/benchmark/<name>.txt` (`basic`, `intermediate`, `solids`, `effects`, `letters`, `combined`).

Each benchmark painting is scored when it's saved. The score shows on the finished card and on the painting's gallery tile. The painting's page breaks it down item by item and shows the card, the painting and the difference side by side.

The same scores from the command line:

```bash
docker exec painty python -m app.benchmark_score                   # every benchmark painting, one line each
docker exec painty python -m app.benchmark_score <gallery id>      # item by item
docker exec painty python -m app.benchmark_score <gallery id> --diff /srv/data/diff.png --json
docker exec painty python -m app.benchmark_score --rescore           # save scores again (older paintings, or after the scorer changes)
```

Without Docker, run the same commands from the repo root with the venv active.

Each item gets these numbers:

- **score**: the share of painted pixels where the copy matches the card, place and colour. 1 is perfect. Edges may be 1 px off without losing anything, so thin strokes aren't marked down for tiny slips.
- **shape**: the same, ignoring colour.
- **colour**: how close the colours are where both have paint.
- **offset**: the shift that best lines the copy up with the card. Positive x means the copy sits to the right.
- **shifted**: the shape score at that offset. A high shifted score with a low shape score means the right shape in the wrong place.

The whole painting also gets a global offset, which shows whether the grid mode makes a copy drift.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | One of these two | Calls models directly on OpenAI and uploads traces |
| `OPENROUTER_API_KEY` | One of these two | Calls models with a `/` in their id through OpenRouter |
| `PAINTY_MODELS` | No | Models always offered in the New painting dialog, comma separated. The first is the default. They must accept image input for vision and reference images |
| `PAINTY_PRICE_<MODEL>` | No | A model's price, for the cost estimate in the gallery. Models added from OpenRouter don't need one. US dollars per million tokens as `input/cached/output/cache write`, for example `PAINTY_PRICE_GPT_6_1_SOL=2.00/0.10/10.00/2.50`. Leave off the cache write price for models before GPT-5.6, which don't bill writes. `.env.example` has current prices for the default models. The name is the model in capitals with other characters as `_`. Costs use the current prices, so older paintings are priced too |
| `TRACE_WORKFLOW_NAME` | No | Name shown for each drawing in OpenAI Traces |
| `OPENAI_AGENTS_DISABLE_TRACING` | No | Set to `true` to stop sending traces to OpenAI |
| `DATA_DIR` | No | Where the gallery and saved settings live. Defaults to `data` (`/srv/data` in the container, which is the `painty-data` volume) |

See `.env.example` for defaults.

## How it works

The design and the reasons behind it are in [`docs/PDD.md`](docs/PDD.md). In short:

- The agent has four tools: `paint` (an ordered batch of shapes, each written as one short line such as `ellipse 196 180 316 300 #f2c9a0`), `look` (a PNG of the canvas, if vision is on), `request_shapes` (more shapes near the end, not in a benchmark) and `finish` (a title and a note).
- Every shape becomes one SVG fragment. Your browser shows those fragments, and the server rasterises the very same SVG for the agent's eyes and the gallery, so what you see is exactly what the agent sees.
- The budget is enforced in code. Once it's used up nothing more is painted, and the agent either finishes or asks for more. Asking ends the run cleanly with its history kept, so it resumes the same conversation with the shapes you give, or with your no.

## Roadmap

- Multi-step scenes: add to a finished painting with a follow-up prompt ("now add people playing volleyball").
- A "hard mode" with an unfamiliar coordinate system, such as y pointing up.
- More shapes or paint options, such as free three-point triangles.
- A side-by-side comparison view for vision on and off.
- Style suggestions when starting a painting.

## Licence

MIT
