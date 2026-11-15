# Painty

**Can an AI model adapt to a creative system it's never seen before?**

![Seven paintings made by AI models using Painty: a sleeping fox, a cat as the Mona Lisa, a restored shopfront, a knight on a tricycle, an impressionist AI, a golden retriever and a wilting rose](docs/images/hero.jpg)

Ask a model for a drawing and it will usually reach for SVG, a format it has seen countless times in training. Painty takes that away. It gives the model a blank canvas, six shapes, and a paint system it can only learn from one page of instructions and two exemplar sheets. The model paints in stages, layering shapes like real paint, and can stop to look at its own work and paint over what it doesn't like.

Every picture on this page was painted that way, by a language model calling a few simple tools: `paint`, `look` and `finish`.

### **▶ [Watch the 8-minute demo](https://youtu.be/Cy3utIDBFF0)**

**[Gallery](docs/GALLERY.md)** · **[Set it up](docs/SETUP.md)** · **[How it's designed](docs/PDD.md)** · **[Suggest a painting](../../issues/new?template=painting-idea.md)**

## How it works

![The Painty studio part-way through a painting: the canvas on the left, and on the right a feed of each stage's note and the snapshots the model looked at](docs/images/studio.jpg)

- **Six shapes, one line each.** Line, rectangle, ellipse, triangle, diamond and trail. A shape is one short line of text such as `ellipse 196 180 316 300 #f2c9a0`: two corners mark out a box and the shape fills it. Optional modifiers, which the model can add to any shape, give rotation, skew, colour blends, spray, soft edges, and strokes that bend and taper.
- **Paint is permanent.** Each shape covers what's beneath it. There is no undo and no eraser, so a mistake can only be painted over.
- **Three tools do the painting.** `paint` lays down a batch of shapes with a note saying what they are. `look` sends the model a snapshot of its own canvas with a grid drawn on, so it can check its work. `finish` gives the painting a title and signs it off. A fourth, `request_shapes`, lets it ask for more shapes near the end.
- **Learnt from the prompt.** The model gets one page of instructions and two exemplar sheets that show the system in action. Nothing else.

It isn't total novelty: a model still needs to know what a tiger looks like. But it has to translate that knowledge into a system it is meeting for the first time.

## What it can do

**Follow a prompt.** Text in, painting out. The titles are the models' own.

<table><tr>
<td width="33%"><img src="docs/gallery/mona-meowsa-gpt-6-1-sol.jpg" alt="Mona Meowsa: the Mona Lisa as a mischievous cat"></td>
<td width="33%"><img src="docs/gallery/sir-pedalot-and-his-noble-steed-gpt-6-1-sol.jpg" alt="Sir Pedalot and His Noble Steed: a knight on a child's tricycle"></td>
<td width="33%"><img src="docs/gallery/fading-crimson-rose-gemini-4-argon.jpg" alt="Fading Crimson Rose: a slightly wilted red rose"></td>
</tr></table>

**Start from a picture.** What Painty does with it is set by the prompt: recreate it, borrow its colours, or change it. Below, GPT-6.1 Sol was shown a derelict building and asked to paint it brought back to life (the inset is what it was given). That one was an experiment: the prompt also told it to paint a test outline first and check the perspective before committing to the final painting.

<table><tr>
<td width="33%"><img src="docs/gallery/fleur-open-again-gpt-6-1-sol.jpg" alt="Fleur, Open Again: a derelict building repainted as a working florist"></td>
<td width="33%"><img src="docs/gallery/black-ceramic-latte-mug-gpt-6-1-sol.jpg" alt="Black Ceramic Latte Mug: a mug recreated from a picture, without its background"></td>
<td width="33%"><img src="docs/gallery/scarlet-world-gpt-6-1-sol.jpg" alt="Scarlet World: a planet painted in colours taken from a photo"></td>
</tr></table>

**Be guided by a style.** Cubism, Renaissance, Impressionism, including each style's "interpretation of an Artificial Intelligence".

<table><tr>
<td width="33%"><img src="docs/gallery/lesprit-artificiel-cubist-ai-gemini-4-argon.jpg" alt="L'Esprit Artificiel: a cubist interpretation of an AI"></td>
<td width="33%"><img src="docs/gallery/the-allegory-of-artificial-intellect-gpt-6-1-sol.jpg" alt="The Allegory of Artificial Intellect: a Renaissance interpretation of an AI"></td>
<td width="33%"><img src="docs/gallery/luminous-mind-impressionist-ai-gemini-4-argon.jpg" alt="Luminous Mind: an impressionist interpretation of an AI"></td>
</tr></table>

**Paint from a name alone.** Hojakdo (호작도) is a traditional Korean folk painting of a tiger and a magpie. Four models were given only the prompt.

<table><tr>
<td width="25%"><img src="docs/gallery/hojakdo-tiger-and-magpie-gpt-6-1-sol.jpg" alt="Hojakdo, Tiger and Magpie, painted by GPT-6.1 Sol"></td>
<td width="25%"><img src="docs/gallery/hojakdo-tiger-and-magpie-gpt-6-astra.jpg" alt="Hojakdo, Tiger and Magpie, painted by GPT-6 Astra"></td>
<td width="25%"><img src="docs/gallery/hojakdo-tiger-and-magpie-under-the-pine-gemini-4-argon.jpg" alt="Hojakdo Tiger and Magpie Under the Pine, painted by Gemini 4 Argon"></td>
<td width="25%"><img src="docs/gallery/hojakdo-tiger-and-magpie-under-the-pine-claude-fable-5-2.jpg" alt="Hojakdo, Tiger and Magpie under the Pine, painted by Claude Fable 5.2"></td>
</tr></table>

**Paint without looking.** Vision can be switched off, so the model never sees its canvas and has to paint from the numbers alone. Each of these four models still landed its tiger, though the style can shift.

![A stripy tiger painted by four models, each with vision on and with vision off](docs/images/vision-off.jpg)

There are more in the **[gallery](docs/GALLERY.md)**.

## What I found

These are results from my own runs, mostly one run per model, so treat them as observations and not a league table.

### Same dog, different models

To test reproduction, I gave many models the same challenge: the same black and white picture, and the prompt "Recreate this image, but with colour". Costs marked ≈ are estimated from the run's token counts at list prices; the rest are what OpenRouter billed.

**OpenAI.** The GPT models scale neatly: bigger model, better dog. Astra performed better than Sol, with better reproduction of the eyes, positioning of the teeth and tongue, and soft edges, but it did all this at over seven times the cost.

![The dog painted by GPT-6 Luna, GPT-6.1 Terra, GPT-6.1 Sol and GPT-6 Astra](docs/images/models-dog-openai.jpg)

| Model | Shapes | Looks | Time | Cost |
|---|---|---|---|---|
| GPT-6 Luna | 331 | 10 | 3 min 59 s | ≈ $0.02 |
| GPT-6.1 Terra | 242 | 5 | 2 min 0 s | ≈ $0.08 |
| GPT-6.1 Sol | 247 | 3 | 2 min 15 s | ≈ $0.14 |
| GPT-6 Astra | 292 | 4 | 5 min 29 s | ≈ $1.02 |

**Google.** Gemini 3.8 Flash kept painting over its own work, trying to correct it. Hydrogen's is simpler, but reasonably done. Argon and Carbon are both up with the best; Carbon places the teeth and tongue a little better, but its run cost about twice as much.

![The dog painted by Gemini 3.8 Flash, Gemini 4 Hydrogen, Gemini 4 Argon and Gemini 4 Carbon](docs/images/models-dog-google.jpg)

| Model | Shapes | Looks | Time | Cost |
|---|---|---|---|---|
| Gemini 3.8 Flash | 235 | 5 | 2 min 27 s | $0.18 |
| Gemini 4 Hydrogen | 120 | 4 | 1 min 42 s | $0.18 |
| Gemini 4 Argon | 235 | 3 | 3 min 22 s | $0.18 |
| Gemini 4 Carbon | 247 | 3 | 8 min 1 s | $0.40 |

**Anthropic.** Claude Haiku, Sonnet and Opus all struggled with this one. Fable 5.2, a very recent model, is up with the best, though it was the most expensive run here.

![The dog painted by Claude Haiku 5.5, Claude Sonnet 5.5, Claude Opus 5.5 and Claude Fable 5.2](docs/images/models-dog-anthropic.jpg)

| Model | Shapes | Looks | Time | Cost |
|---|---|---|---|---|
| Claude Haiku 5.5 | 19 | 0 | 18 s | $0.003 |
| Claude Sonnet 5.5 | 28 | 1 | 25 s | $0.02 |
| Claude Opus 5.5 | 96 | 4 | 88 s | $0.22 |
| Claude Fable 5.2 | 181 | 3 | 5 min 0 s | $1.35 |

**Other labs.** Muse Spark and Qwen fell short, and Kimi's is detailed, but messy. Gemma, a small open model, struggled most, as you might expect at its size.

![The dog painted by Meta Muse Spark 1.3, Qwen3.8 Max, Kimi K3.1 Pro and Gemma 4 31B](docs/images/models-dog-other.jpg)

| Model | Shapes | Looks | Time | Cost |
|---|---|---|---|---|
| Meta Muse Spark 1.3 | 163 | 5 | 86 s | $0.10 |
| Qwen3.8 Max | 74 | 3 | 7 min 28 s | $0.26 |
| Kimi K3.1 Pro | 250 | 4 | 1 min 56 s | $0.29 |
| Gemma 4 31B | 145 | 2 | 3 min 59 s | $0.03 |

### A simpler test

"Paint a doughnut. Only use solid fills." Here are eighteen models in order of cost. Spending more usually helps, but not always.

![The doughnut painted by the nine cheapest runs](docs/images/models-doughnut-1.jpg)

![The doughnut painted by the nine more expensive runs](docs/images/models-doughnut-2.jpg)

| Model | Shapes | Looks | Time | Cost |
|---|---|---|---|---|
| GPT-6 Luna | 31 | 1 | 32 s | ≈ $0.002 |
| Claude Haiku 5.5 | 17 | 0 | 19 s | $0.002 |
| Gemma 4 31B | 23 | 1 | 22 s | $0.006 |
| GLM 5.3 | 24 | 0 | 48 s | $0.02 |
| GPT-6.1 Terra | 81 | 2 | 36 s | ≈ $0.02 |
| Claude Sonnet 5.5 | 20 | 0 | 10 s | $0.03 |
| GPT-6.1 Sol | 54 | 1 | 28 s | ≈ $0.03 |
| Meta Muse Spark 1.3 | 58 | 2 | 24 s | $0.03 |
| Claude Opus 5.5 | 34 | 1 | 27 s | $0.09 |
| Kimi K3.1 Pro | 81 | 2 | 71 s | $0.10 |
| Gemini 4 Argon | 138 | 2 | 2 min 10 s | $0.10 |
| Qwen3.8 Max | 38 | 2 | 3 min 19 s | $0.13 |
| Gemini 3.8 Flash | 224 | 6 | 3 min 19 s | $0.17 |
| Gemini 4 Hydrogen | 106 | 4 | 97 s | $0.19 |
| GPT-6 Astra | 64 | 1 | 60 s | ≈ $0.19 |
| Gemini 4 Carbon | 124 | 2 | 3 min 17 s | $0.20 |
| Xiaomi MiMo v2.6 Pro | 30 | 2 | 44 s | $0.24 |
| Claude Fable 5.2 | 55 | 1 | 63 s | $0.30 |

### High SVG scores didn't always carry over

[Design Arena](https://www.designarena.ai/leaderboard/svgs) runs a public leaderboard where people vote on SVGs made by AI models. These are its Illustrations sub-category scores for some of the models I tested, as read on 14 November 2026, when 133 models were ranked.

![Design Arena Illustrations scores for ten models, each beside its Painty dog and doughnut](docs/images/design-arena.jpg)

| Rank | Model | Illustrations score |
|---|---|---|
| 2 | Gemini 4 Carbon | 1,486 |
| 3 | GPT-6 Astra | 1,465 |
| 5 | Claude Fable 5.2 | 1,434 |
| 7 | Gemini 4 Argon | 1,379 |
| 8 | GPT-6.1 Terra | 1,365 |
| 9 | Kimi K3.1 Pro | 1,364 |
| 10 | GPT-6.1 Sol | 1,363 |
| 12 | Claude Opus 5.5 | 1,316 |
| 13 | Muse Spark 1.3 (listed there as "1.3 Max") | 1,315 |
| 14 | Claude Sonnet 5.5 | 1,310 |

Kimi, Muse Spark, Claude Opus and Claude Sonnet all rank in the top fifteen. But despite those scores for SVG illustration, in these runs they showed a poor ability to adapt what they know to a new system.

### Putting a number on it

Painty has benchmark cards: pictures made with Painty's own shapes, which a model has to copy exactly. The copy is scored pixel by pixel.

![The combined benchmark card, a model's copy, the difference between them, and four models' scores](docs/images/benchmark-scores.jpg)

| Model | Combined card |
|---|---|
| GPT-6 Luna | 82.6% |
| Gemini 4 Argon | 91.9% |
| GPT-6.1 Terra | 99.1% |
| GPT-6.1 Sol | 99.9% |

One run each, with vision on. [How the benchmark and its scoring work](docs/SETUP.md#benchmark).

### Is this an efficient way to make a picture?

Almost certainly not. If a finished image is all you're after, an image model is the obvious choice. I gave three of the prompts above to two image models through OpenRouter, one generation each:

![The Mona Lisa cat, the knight on a tricycle and the wilted rose, as painted by Painty with GPT-6.1 Sol and as generated by two image models](docs/images/image-models.jpg)

| | Average cost per image | Average time |
|---|---|---|
| Painty with GPT-6.1 Sol | ≈ $0.07 | about 100 s |
| Gemini Nano Banana 2.1 | $0.04 | about 8 s |
| GPT-5.4 Image 2 | $0.23 | about 100 s |

And with SVG a model can lean on everything it saw in training, so it would probably be more accurate, especially with tricky perspective. Painty makes the model reason its way through an unfamiliar system, and more reasoning means more tokens, more time and more cost. I haven't measured an SVG baseline.

Efficiency was never the point. These models weren't trained on Painty. They read the instructions, picked up an unfamiliar brush and painted with it. That's adaptation, and that's what Painty is built to show.

## The app

<table><tr>
<td width="50%"><img src="docs/images/new-painting.jpg" alt="The New painting dialog: prompt, canvas shape, reference image, model and budgets"><br><sub>New painting: prompt, canvas, reference image, model and budgets</sub></td>
<td width="50%"><img src="docs/images/gallery.jpg" alt="The gallery, with a filter chip for each model developer"><br><sub>The gallery, filtered by developer or favourites</sub></td>
</tr><tr>
<td width="50%"><img src="docs/images/painting-page.jpg" alt="A painting's page, with a snapshot of every stage and its full details"><br><sub>A painting's page: every stage, every look, tokens and cost</sub></td>
<td width="50%"><img src="docs/images/model-browser.jpg" alt="The OpenRouter model browser sorted by Design Arena SVG rating"><br><sub>Browse OpenRouter models and sort them by benchmark scores</sub></td>
</tr><tr>
<td width="50%"><img src="docs/images/benchmarks.jpg" alt="The Benchmarks section, each tile showing its card and score"><br><sub>Benchmarks: each run with its card and score</sub></td>
<td width="50%"><img src="docs/images/benchmark-page.jpg" alt="A benchmark painting's page with its score broken down"><br><sub>A benchmark run, scored item by item</sub></td>
</tr></table>

## Key features

- **A custom paint system.** Each shape is placed by giving two opposite corners of a box, and it fills that box. They can be turned to any angle or skewed, filled with flat colour or linear and radial blends, given outlines, sprayed on as speckle, feathered at the edges, and made see-through for shading and highlights. Lines can be bent into curves and tapered like brush strokes, and a trail walks a stroke leg by leg by compass heading.
- **An exemplar to learn from.** Before it paints, Painty can be shown two exemplar sheets of numbered examples, painted by the same renderer and labelled with their boxes and coordinates, each listed with the exact shape text that drew it.
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

## Try it

You need Docker with the Compose plugin, and an OpenAI or OpenRouter API key.

```bash
git clone https://github.com/Oliver-J-Carr/Painty.git && cd Painty
cp .env.example .env          # then set OPENAI_API_KEY, OPENROUTER_API_KEY or both
docker compose up -d --build
```

Open <http://127.0.0.1:8091>, click **New painting** and ask for a picture.

Models, settings, the benchmark and environment variables are covered in the **[setup guide](docs/SETUP.md)**. The design and the reasons behind it are in **[`docs/PDD.md`](docs/PDD.md)**.

## What should Painty paint next?

[Suggest a painting](../../issues/new?template=painting-idea.md). If I try it, the result goes in the [gallery](docs/GALLERY.md).

## Roadmap

- Multi-step scenes: add to a finished painting with a follow-up prompt ("now add people playing volleyball").
- A "hard mode" with an unfamiliar coordinate system, such as y pointing up.
- More shapes or paint options, such as free three-point triangles.
- A side-by-side comparison view for vision on and off.
- Style suggestions when starting a painting.

## Licence

MIT
