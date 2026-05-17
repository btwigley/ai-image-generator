# AI Image Generator

Batch image generation tool using **OpenAI** (GPT Image / DALL-E) and **fal.ai** (FLUX) models. Includes a Flask web UI for prompt management and a CLI for batch/parallel generation.

## Features

- **Multi-model support** — OpenAI (gpt-image-1, gpt-image-1.5, gpt-image-1-mini) and fal.ai FLUX (Pro, Dev, Flash, Schnell)
- **Character templates** — Define characters with JSON templates and CSV variation sheets
- **Batch generation** — Process dozens of variations in parallel or via OpenAI's batch API
- **Web UI** — Flask-based prompt manager for crafting and testing prompts
- **CLI** — Full command-line interface for automated pipelines
- **Manifest tracking** — Tracks what was generated, with which settings, for reproducibility

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

### CLI — Parallel Generation

```bash
python batch_image_generator.py parallel \
    --character characters/example_character.json \
    --variations characters/example_variations.csv
```

### CLI — Batch Mode (OpenAI Batch API)

```bash
# Submit batch
python batch_image_generator.py batch \
    --character characters/example_character.json \
    --variations characters/example_variations.csv

# Check status
python batch_image_generator.py batch-status \
    --batch-id batch_abc123 \
    --character characters/example_character.json \
    --variations characters/example_variations.csv
```

### CLI Options

```
--model          Model to use (default: from settings)
--quality        Image quality: low, medium, high (default: from settings)
--size           Image dimensions (default: from settings)
--format         Output format: webp, png (default: from settings)
--output-dir     Output directory (default: ./output)
--concurrency    Max parallel requests (default: 3)
```

## Character Templates

Characters are defined with a JSON file (base prompt, style, etc.) paired with a CSV variations file (one row per image to generate):

```json
{
  "name": "Example Character",
  "base_prompt": "A portrait of...",
  "style": "photorealistic, cinematic lighting"
}
```

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

## License

MIT — see [LICENSE](LICENSE).

---

Built by [Wigley Studios](https://wigleystudios.com)
