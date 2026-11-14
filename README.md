# AI Image Generator

Batch image generation tool using **OpenAI** (GPT Image) and **fal.ai** (FLUX) models. Includes a Flask web UI for prompt management and generation (OpenAI and fal.ai) and a CLI for batch/parallel generation (OpenAI only).

## Features

- **Multi-model support** — OpenAI (gpt-image-1, gpt-image-1.5, gpt-image-1-mini) in the web UI and the CLI; fal.ai FLUX (Pro, Dev, Flash, Schnell) in the web UI
- **Character templates** — Define characters with JSON templates and CSV variation sheets
- **Batch generation** — Process dozens of variations in parallel or via OpenAI's batch API
- **Web UI** — Flask-based prompt manager for crafting and testing prompts
- **CLI** — Full command-line interface for automated pipelines
- **Manifest tracking** — Tracks what was generated, with which settings, for reproducibility; a row already generated (or in a batch still being tracked) is skipped, and an existing image file is never overwritten

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Configure API keys (choose one method):

**Option A: Environment variables** (recommended)

```bash
export OPENAI_API_KEY=your_openai_key
export FAL_KEY=your_fal_key
```

**Option B: settings.json**

```bash
cp settings.example.json settings.json
# Edit settings.json with your keys
```

## Usage

### Web UI (Prompt Manager)

```bash
python app.py
# Opens at http://localhost:5000
```

The web UI generates one job at a time: a second job started while one runs waits for it. The web UI and
CLI runs may share an output folder: every manifest update takes `manifest.json.lock`, re-reads the manifest and merges
its own entries, so no writer drops another's.

Each row makes one paid call. Every image it returns is then saved, and a failed download or write is retried without
paying again. If an image still cannot be saved, the row is reported as failed, the images it did save are kept
(marked `partial` in the manifest), any unsaved download URL is recorded under an `unsaved_*` entry, and the row can be
generated again. Each saved image's manifest key is its path under the output folder (`name/category/name_0001.png`, extension included),
so images in different categories or formats never share a key.

### CLI

The CLI talks to **OpenAI only** (fal.ai models are available in the web UI). Relative
`--character` and `--variations` paths are read from the `characters/` folder, so pass the
file name (or an absolute path). The CLI uses its own defaults below, not the web UI's saved
settings.

```bash
# Preview the first 3 prompts (no API call, no key needed)
python batch_image_generator.py parallel \
    --character example_character.json \
    --variations example_variations.csv \
    --preview 3

# Parallel generation
python batch_image_generator.py parallel \
    --character example_character.json \
    --variations example_variations.csv

# Batch mode (OpenAI Batch API, 50% cheaper): submit, then check / download
python batch_image_generator.py batch \
    --character example_character.json \
    --variations example_variations.csv
python batch_image_generator.py batch-status \
    --character example_character.json \
    --variations example_variations.csv
```

`batch-status` checks every batch it is tracking (or one with `--batch-id`). When a batch
finishes, fails, expires or is cancelled, every image it did produce is saved, and the rows
that produced none can be generated again by the next run.

### CLI Options

```
--character, -c   Character JSON (file name in characters/, or absolute path)   required
--variations, -v  Variations CSV (file name in characters/, or absolute path)   required
--model           gpt-image-1 | gpt-image-1.5 | gpt-image-1-mini   (default: gpt-image-1)
--quality, -q     low | medium | high | auto                       (default: medium)
--size, -s        1024x1024 | 1024x1536 | 1536x1024 | auto         (default: 1024x1536)
--format, -f      webp | png | jpeg                                (default: webp)
--compression     0-100 for webp/jpeg                              (default: 90)
--moderation      low | auto                                       (default: low)
--n               Images per variation row                         (default: 1)
--output, -o      Output directory                                 (default: ./output)
--yes, -y         Skip the cost confirmation prompt
--dry-run         Print every prompt without generating
--preview N       Print the first N prompts without generating
--concurrency     parallel only: max concurrent requests           (default: 5)
--min-interval    parallel only: min seconds between requests      (default: 0.5)
--chunk-size      batch only: max rows per batch job               (default: 500)
--batch-id        batch-status only: check one batch
```

Run `python batch_image_generator.py <command> --help` for the authoritative list.

## Character Templates

Characters are defined with a JSON file paired with a CSV variations file (one row per image to generate):

```json
{
  "name": "example_character",
  "core": "The description repeated in every image, e.g. an ancient forest dragon with emerald scales...",
  "style": "Painterly digital art, warm cinematic lighting",
  "negative": "text overlays, watermarks, low resolution"
}
```

- `core` (required, or an `identity` block of traits the core is built from) is the character's description.
- `style`, when set, is the style of every prompt. When it is empty, each row's `category` picks a
  built-in photographic style (`candid`, `natural` or `professional`).
- `negative` is appended as things to avoid.

Each CSV row needs `scene` and `outfit`; `pose`, `location`, `camera`, `category`, `emotion` and
`lighting` are optional. A `category` is also the output sub-folder name, so it may use letters,
digits, spaces, dots, dashes and underscores only (a row with any other category is skipped).

See `characters/example_character.json` and `characters/example_variations.csv` for the full format.

## Supported Models

| Provider | Model | Notes |
|----------|-------|-------|
| OpenAI | gpt-image-1.5 | Best quality, slowest |
| OpenAI | gpt-image-1 | Good balance |
| OpenAI | gpt-image-1-mini | Fastest, lowest cost |
| fal.ai | flux-pro | High quality FLUX |
| fal.ai | flux-dev | Development FLUX |
| fal.ai | flux-2-flash | Fast FLUX v2 |
| fal.ai | flux-schnell | Fastest FLUX |

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q
```

The tests stub every provider and write only to a temporary folder.

## License

MIT — see [LICENSE](LICENSE).

---

Built by [Wigley Studios](https://wigleystudios.com)
