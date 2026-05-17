# -*- coding: utf-8 -*-
"""
Batch Image Generator
Reads character templates (JSON) and variation sheets (CSV), then mass-generates
images via OpenAI or fal.ai (FLUX) in parallel or batch mode.

Usage:
  python batch_image_generator.py parallel  --character char.json --variations shots.csv
  python batch_image_generator.py batch     --character char.json --variations shots.csv
  python batch_image_generator.py batch-status --batch-id batch_abc123 --character char.json --variations shots.csv
"""

import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import urllib.request
from configparser import ConfigParser
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

FAL_MODELS = {
    "flux-pro":     "fal-ai/flux-pro/v1.1",
    "flux-dev":     "fal-ai/flux/dev",
    "flux-2-flash": "fal-ai/flux-2/flash",
    "flux-schnell": "fal-ai/flux/schnell",
}

FAL_WEBP_SUPPORT = {"flux-2-flash"}

OPENAI_MODELS = {"gpt-image-1", "gpt-image-1.5", "gpt-image-1-mini"}

ALL_MODELS = set(FAL_MODELS.keys()) | OPENAI_MODELS


def is_fal_model(model: str) -> bool:
    return model in FAL_MODELS


def fal_endpoint(model: str) -> str:
    return FAL_MODELS[model]


# ---------------------------------------------------------------------------
# Cost tables
# ---------------------------------------------------------------------------

# OpenAI: output tokens per image
TOKEN_COUNTS = {
    ("low",    "1024x1024"): 272,
    ("low",    "1024x1536"): 408,
    ("low",    "1536x1024"): 400,
    ("medium", "1024x1024"): 1056,
    ("medium", "1024x1536"): 1584,
    ("medium", "1536x1024"): 1568,
    ("high",   "1024x1024"): 4160,
    ("high",   "1024x1536"): 6240,
    ("high",   "1536x1024"): 6208,
}

COST_PER_TOKEN = {
    "gpt-image-1":      0.000040,
    "gpt-image-1.5":    0.000040,
    "gpt-image-1-mini": 0.000010,
}

# fal.ai: dollars per megapixel
FAL_COST_PER_MP = {
    "flux-pro":     0.050,
    "flux-dev":     0.025,
    "flux-2-flash": 0.005,
    "flux-schnell": 0.003,
}

BATCH_DISCOUNT = 0.50

# fal.ai size mapping: our WxH -> fal image_size parameter
FAL_SIZE_MAP = {
    "1024x1024": {"width": 1024, "height": 1024},
    "1024x1536": {"width": 1024, "height": 1536},
    "1536x1024": {"width": 1536, "height": 1024},
}

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"
CHARACTERS_DIR = Path(__file__).resolve().parent / "characters"
MAX_RETRIES = 3
BACKOFF_BASE = 2.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_api_key(provider: str = "openai") -> str:
    """Load API key for the given provider ('openai' or 'fal')."""
    settings_path = Path(__file__).resolve().parent / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if provider == "fal":
        key = os.environ.get("FAL_KEY") or settings.get("fal_key", "")
        if key:
            return key
        raise ValueError("FAL_KEY not found in environment or settings.json.")
    else:
        key = os.environ.get("OPENAI_API_KEY") or settings.get("api_key", "")
        if key:
            return key
        raise ValueError("OPENAI_API_KEY not found in environment or settings.json.")


def load_character(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = CHARACTERS_DIR / p
    if not p.exists():
        raise FileNotFoundError(f"Character file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "core" not in data and "identity" in data:
        identity = data["identity"]
        parts = []
        if "age" in identity:
            parts.append(f"A {identity['age']}-year-old person")
        for field in ("hair", "eyes", "skin", "body", "face"):
            if field in identity:
                parts.append(str(identity[field]))
        data["core"] = ", ".join(parts) if parts else ""
    if not data.get("core"):
        raise ValueError("Character template must have a 'core' description or an 'identity' block.")
    data.setdefault("name", p.stem)
    data.setdefault("style", "")
    data.setdefault("negative", "")
    return data


def load_variations(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_absolute():
        p = CHARACTERS_DIR / p
    if not p.exists():
        raise FileNotFoundError(f"Variations CSV not found: {p}")
    rows = []
    with open(p, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if not row.get("scene") or not row.get("outfit"):
                print(f"WARNING: Row {i + 1} missing required 'scene' or 'outfit' — skipping.")
                continue
            rows.append(row)
    if not rows:
        raise ValueError("No valid rows found in variations CSV.")
    return rows


STYLE_TIERS = {
    "candid": {
        "categories": {"morning", "cozy", "selfies", "errands"},
        "style": (
            "Natural smartphone photo, believable social-media candid. "
            "Real skin texture with visible pores, soft flyaway hairs, "
            "slightly off-center framing, authentic casual photography."
        ),
        "realism": (
            "This must look like a real smartphone photo, not AI-generated. "
            "Natural skin texture, real environment with everyday objects, "
            "authentic phone camera quality with slight lens distortion."
        ),
    },
    "natural": {
        "categories": {"lifestyle", "social", "fitness", "beach", "travel"},
        "style": (
            "Natural photography with good composition. Shot on a modern "
            "smartphone or decent camera. Real skin texture, natural colors, "
            "authentic depth of field, warm natural lighting."
        ),
        "realism": (
            "This must look like a real photograph, not AI-generated. "
            "Natural skin texture, authentic environment, real depth of field, "
            "genuine ambient lighting."
        ),
    },
    "professional": {
        "categories": {"professional"},
        "style": (
            "High-quality professional photography. Shot on a DSLR with a "
            "prime lens. Shallow depth of field, intentional flattering lighting, "
            "sharp focus on eyes, professional color grading."
        ),
        "realism": (
            "This must look like a real professional photograph, not AI-generated. "
            "Natural skin texture even under professional lighting, authentic lens "
            "bokeh, real catch-lights in eyes."
        ),
    },
}

_CATEGORY_TO_TIER = {}
for _tier_name, _tier_data in STYLE_TIERS.items():
    for _cat in _tier_data["categories"]:
        _CATEGORY_TO_TIER[_cat] = _tier_name


def _get_style_tier(category: str) -> dict:
    tier_name = _CATEGORY_TO_TIER.get(category.strip().lower(), "candid")
    return STYLE_TIERS[tier_name]


def build_prompt(char: dict, row: dict) -> str:
    """Construct prompt: identity -> scene -> technical -> style -> realism."""
    core = char["core"].rstrip(".")
    parts = [core + "."]

    scene_block = []
    scene_block.append(row["scene"] + ".")
    scene_block.append(f"Wearing {row['outfit']}.")
    if row.get("pose"):
        scene_block.append(row["pose"] + ".")
    if row.get("location"):
        scene_block.append(row["location"] + ".")
    parts.append(" ".join(scene_block))

    tech_lines = []
    if row.get("camera"):
        tech_lines.append(f"Camera: {row['camera']}.")
    if row.get("emotion"):
        tech_lines.append(f"Emotion: {row['emotion']}.")
    if row.get("lighting"):
        tech_lines.append(f"Lighting: {row['lighting']}.")
    if tech_lines:
        parts.append(" ".join(tech_lines))

    category = (row.get("category") or "").strip().lower()
    tier = _get_style_tier(category)
    parts.append(f"Style: {tier['style']}")
    parts.append(f"Realism: {tier['realism']}")

    if char.get("negative"):
        parts.append(f"Avoid: {char['negative']}.")

    return "\n\n".join(parts)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def make_custom_id(char_name: str, row: dict, idx: int, img_n: int = 0) -> str:
    cat = row.get("category", "general").strip() or "general"
    base = f"{char_name}_{cat}_{idx:04d}"
    if img_n > 0:
        base += f"_{img_n}"
    return base


def estimate_cost(model: str, quality: str, size: str, count: int, is_batch: bool) -> float:
    if is_fal_model(model):
        s = size if size != "auto" else "1024x1024"
        dims = FAL_SIZE_MAP.get(s, {"width": 1024, "height": 1024})
        megapixels = (dims["width"] * dims["height"]) / 1_000_000
        per_mp = FAL_COST_PER_MP.get(model, 0.012)
        return per_mp * megapixels * count

    q = quality if quality != "auto" else "medium"
    s = size if size != "auto" else "1024x1024"
    tokens = TOKEN_COUNTS.get((q, s), TOKEN_COUNTS[("medium", "1024x1024")])
    per_token = COST_PER_TOKEN.get(model, COST_PER_TOKEN["gpt-image-1"])
    cost = tokens * per_token * count
    if is_batch:
        cost *= (1.0 - BATCH_DISCOUNT)
    return cost


def display_cost(args, total_images: int):
    direct = estimate_cost(args.model, args.quality, args.size, total_images, False)
    batch = estimate_cost(args.model, args.quality, args.size, total_images, True)
    mode_label = "batch" if args.command == "batch" else "direct"
    active_cost = batch if args.command == "batch" else direct
    print(f"\n{'='*50}")
    print(f"  Rows: {total_images // args.n}  |  Images/row: {args.n}  |  Total: {total_images}")
    print(f"  Model: {args.model}  |  Quality: {args.quality}  |  Size: {args.size}")
    print(f"  Estimated cost (direct): ${direct:.2f}")
    print(f"  Estimated cost (batch):  ${batch:.2f}")
    print(f"  This run ({mode_label}):        ${active_cost:.2f}")
    print(f"{'='*50}\n")
    return active_cost


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class Manifest:
    def __init__(self, output_dir: Path):
        self.path = output_dir / "manifest.json"
        self.data: dict = {}
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)

    def has_hash(self, ph: str) -> bool:
        return any(entry.get("prompt_hash") == ph for entry in self.data.values())

    def add(self, key: str, entry: dict):
        self.data[key] = entry
        self._save()

    def add_batch_ids(self, batch_ids: list[str]):
        meta = self.data.setdefault("_meta", {})
        existing = set(meta.get("pending_batches", []))
        existing.update(batch_ids)
        meta["pending_batches"] = list(existing)
        self._save()

    def get_pending_batches(self) -> list[str]:
        return self.data.get("_meta", {}).get("pending_batches", [])

    def remove_batch_id(self, batch_id: str):
        meta = self.data.get("_meta", {})
        pending = meta.get("pending_batches", [])
        if batch_id in pending:
            pending.remove(batch_id)
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Image saving
# ---------------------------------------------------------------------------

def save_image(b64_data: str, output_dir: Path, char_name: str, category: str,
               file_idx: int, fmt: str) -> Path:
    cat = category.strip() or "general"
    folder = output_dir / char_name / cat
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{char_name}_{file_idx:04d}.{fmt}"
    filepath = folder / filename
    with open(filepath, "wb") as f:
        f.write(base64.b64decode(b64_data))
    return filepath


def save_image_from_url(url: str, output_dir: Path, char_name: str, category: str,
                        file_idx: int, fmt: str) -> Path:
    """Download image from a URL and save it locally."""
    cat = category.strip() or "general"
    folder = output_dir / char_name / cat
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{char_name}_{file_idx:04d}.{fmt}"
    filepath = folder / filename
    req = urllib.request.Request(url, headers={"User-Agent": "CharacterGenerator/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        with open(filepath, "wb") as f:
            f.write(resp.read())
    return filepath


def fal_output_format(model: str, requested_fmt: str) -> str:
    """FLUX 1 models only support jpeg/png. FLUX 2 Flash supports webp too."""
    if model in FAL_WEBP_SUPPORT:
        return requested_fmt if requested_fmt in ("png", "jpeg", "webp") else "png"
    return requested_fmt if requested_fmt in ("png", "jpeg") else "jpeg"


def generate_fal(prompt: str, model: str, size: str, fmt: str,
                 num_images: int = 1, safety: bool = False) -> tuple[list[str], str]:
    """Generate image(s) via fal.ai FLUX. Returns (list of image URLs, actual format used)."""
    import fal_client

    endpoint = fal_endpoint(model)
    dims = FAL_SIZE_MAP.get(size, {"width": 1024, "height": 1024})
    actual_fmt = fal_output_format(model, fmt)

    arguments = {
        "prompt": prompt,
        "image_size": dims,
        "num_images": num_images,
        "output_format": actual_fmt,
        "enable_safety_checker": safety,
        "guidance_scale": 2.5,
    }

    result = fal_client.subscribe(endpoint, arguments=arguments)

    urls = []
    for img in result.get("images", []):
        url = img.get("url", "")
        if url:
            urls.append(url)
    return urls, actual_fmt


# ---------------------------------------------------------------------------
# Parallel mode
# ---------------------------------------------------------------------------

async def generate_one(client, prompt: str, args, semaphore, rate_event,
                       min_interval: float) -> list[str]:
    """Generate image(s) for a single prompt with retry and rate limiting."""
    async with semaphore:
        await rate_event.wait()

        for attempt in range(MAX_RETRIES):
            try:
                rate_event.clear()
                asyncio.get_event_loop().call_later(min_interval, rate_event.set)

                params = {
                    "model": args.model,
                    "prompt": prompt,
                    "n": args.n,
                    "size": args.size,
                    "quality": args.quality,
                    "moderation": args.moderation,
                }
                if args.format != "png":
                    params["output_format"] = args.format
                if args.compression and args.format in ("webp", "jpeg"):
                    params["output_compression"] = args.compression

                result = await client.images.generate(**params)
                return [img.b64_json for img in result.data]

            except Exception as e:
                err_str = str(e).lower()
                if "content_policy" in err_str or "moderation" in err_str:
                    print(f"  MODERATION REFUSAL: {str(e)[:120]}")
                    return []
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE ** (attempt + 1)
                    print(f"  Retry {attempt + 1}/{MAX_RETRIES} in {wait:.1f}s: {str(e)[:100]}")
                    await asyncio.sleep(wait)
                else:
                    print(f"  FAILED after {MAX_RETRIES} attempts: {str(e)[:150]}")
                    return []
    return []


async def run_parallel(args, char: dict, variations: list[dict]):
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=load_api_key())

    output_dir = Path(args.output)
    manifest = Manifest(output_dir)

    prompts_and_meta = []
    for i, row in enumerate(variations):
        prompt = build_prompt(char, row)
        ph = prompt_hash(prompt)
        if manifest.has_hash(ph):
            print(f"  Skipping row {i + 1} (already generated)")
            continue
        prompts_and_meta.append((i, row, prompt, ph))

    if not prompts_and_meta:
        print("All rows already generated. Nothing to do.")
        return

    total_images = len(prompts_and_meta) * args.n
    display_cost(args, total_images)

    if not args.yes:
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    semaphore = asyncio.Semaphore(args.concurrency)
    rate_event = asyncio.Event()
    rate_event.set()

    file_counter = max(
        (int(k.split("_")[-1]) for k in manifest.data
         if k != "_meta" and "_" in k and k.split("_")[-1].isdigit()),
        default=0
    ) + 1

    completed = 0
    failed = 0

    async def process_row(idx, row, prompt, ph):
        nonlocal file_counter, completed, failed
        images = await generate_one(client, prompt, args, semaphore, rate_event, args.min_interval)
        if not images:
            failed += 1
            return

        category = row.get("category", "general")
        for img_i, b64 in enumerate(images):
            fid = file_counter
            file_counter += 1
            filepath = save_image(b64, output_dir, char["name"], category, fid, args.format)
            key = f"{char['name']}_{fid:04d}"
            manifest.add(key, {
                "custom_id": make_custom_id(char["name"], row, idx, img_i),
                "filename": str(filepath.relative_to(output_dir)),
                "prompt_hash": ph,
                "model": args.model,
                "quality": args.quality,
                "size": args.size,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "batch_id": None,
            })
        completed += 1
        print(f"  [{completed}/{len(prompts_and_meta)}] Row {idx + 1} done — {len(images)} image(s)")

    tasks = [process_row(idx, row, prompt, ph) for idx, row, prompt, ph in prompts_and_meta]
    await asyncio.gather(*tasks)

    print(f"\nDone. Generated: {completed * args.n} | Failed: {failed} | Output: {output_dir}")


# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------

def run_batch(args, char: dict, variations: list[dict]):
    from openai import OpenAI
    client = OpenAI(api_key=load_api_key())

    output_dir = Path(args.output)
    manifest = Manifest(output_dir)

    jobs = []
    for i, row in enumerate(variations):
        prompt = build_prompt(char, row)
        ph = prompt_hash(prompt)
        if manifest.has_hash(ph):
            print(f"  Skipping row {i + 1} (already generated)")
            continue
        jobs.append((i, row, prompt, ph))

    if not jobs:
        print("All rows already generated. Nothing to do.")
        return

    total_images = len(jobs) * args.n
    display_cost(args, total_images)

    if not args.yes:
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    chunks = [jobs[i:i + args.chunk_size] for i in range(0, len(jobs), args.chunk_size)]
    print(f"Submitting {len(chunks)} batch(es) ({len(jobs)} total jobs, chunk size {args.chunk_size})...\n")

    batch_ids = []
    for ci, chunk in enumerate(chunks):
        lines = []
        for idx, row, prompt, ph in chunk:
            body = {
                "model": args.model,
                "prompt": prompt,
                "n": args.n,
                "size": args.size,
                "quality": args.quality,
                "moderation": args.moderation,
            }
            if args.format != "png":
                body["output_format"] = args.format
            if args.compression and args.format in ("webp", "jpeg"):
                body["output_compression"] = args.compression

            custom_id = make_custom_id(char["name"], row, idx)
            lines.append(json.dumps({
                "custom_id": custom_id,
                "method": "POST",
                "url": "/v1/images/generations",
                "body": body,
            }))

        jsonl_content = "\n".join(lines)
        jsonl_bytes = jsonl_content.encode("utf-8")

        uploaded = client.files.create(
            file=("batch_input.jsonl", io.BytesIO(jsonl_bytes)),
            purpose="batch",
        )
        print(f"  Chunk {ci + 1}/{len(chunks)}: Uploaded {len(chunk)} jobs (file {uploaded.id})")

        batch = client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/images/generations",
            completion_window="24h",
            metadata={"character": char["name"], "chunk": str(ci)},
        )
        batch_ids.append(batch.id)
        print(f"  Chunk {ci + 1}/{len(chunks)}: Batch submitted — {batch.id}")

        for idx, row, prompt, ph in chunk:
            custom_id = make_custom_id(char["name"], row, idx)
            key = f"pending_{custom_id}"
            manifest.add(key, {
                "custom_id": custom_id,
                "prompt_hash": ph,
                "model": args.model,
                "quality": args.quality,
                "size": args.size,
                "batch_id": batch.id,
                "status": "pending",
            })

    manifest.add_batch_ids(batch_ids)
    print(f"\nAll batches submitted. IDs: {', '.join(batch_ids)}")
    print(f"Check status with:\n  python batch_image_generator.py batch-status --character {args.character} --variations {args.variations}")


# ---------------------------------------------------------------------------
# Batch status / download
# ---------------------------------------------------------------------------

def run_batch_status(args, char: dict, variations: list[dict]):
    from openai import OpenAI
    client = OpenAI(api_key=load_api_key())

    output_dir = Path(args.output)
    manifest = Manifest(output_dir)

    batch_ids = []
    if args.batch_id:
        batch_ids = [args.batch_id]
    else:
        batch_ids = manifest.get_pending_batches()
        if not batch_ids:
            print("No pending batches found in manifest. Use --batch-id to specify one.")
            return

    print(f"Checking {len(batch_ids)} batch(es)...\n")

    for bid in batch_ids:
        batch = client.batches.retrieve(bid)
        print(f"  Batch {bid}: {batch.status}")
        print(f"    Total: {batch.request_counts.total} | "
              f"Completed: {batch.request_counts.completed} | "
              f"Failed: {batch.request_counts.failed}")

        if batch.status == "completed" and batch.output_file_id:
            print(f"    Downloading results...")
            content = client.files.content(batch.output_file_id)
            results = content.text.strip().split("\n")

            file_counter = max(
                (int(k.split("_")[-1]) for k in manifest.data
                 if k != "_meta" and "_" in k and k.split("_")[-1].isdigit()),
                default=0
            ) + 1

            downloaded = 0
            for line in results:
                result_obj = json.loads(line)
                custom_id = result_obj.get("custom_id", "")
                response = result_obj.get("response", {})
                error = result_obj.get("error")

                if error:
                    print(f"    FAILED [{custom_id}]: {error}")
                    continue

                body = response.get("body", {})
                images = body.get("data", [])

                parts = custom_id.split("_")
                category = parts[1] if len(parts) > 1 else "general"

                for img_i, img in enumerate(images):
                    b64 = img.get("b64_json", "")
                    if not b64:
                        continue
                    fid = file_counter
                    file_counter += 1
                    filepath = save_image(b64, output_dir, char["name"], category, fid, args.format)

                    key = f"{char['name']}_{fid:04d}"
                    pending_key = f"pending_{custom_id}"
                    pending_entry = manifest.data.pop(pending_key, {})

                    manifest.add(key, {
                        "custom_id": custom_id,
                        "filename": str(filepath.relative_to(output_dir)),
                        "prompt_hash": pending_entry.get("prompt_hash", ""),
                        "model": pending_entry.get("model", args.model),
                        "quality": pending_entry.get("quality", args.quality),
                        "size": pending_entry.get("size", args.size),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "batch_id": bid,
                    })
                    downloaded += 1

            manifest.remove_batch_id(bid)
            print(f"    Downloaded {downloaded} image(s)")

        elif batch.status == "failed":
            manifest.remove_batch_id(bid)
            if batch.error_file_id:
                err_content = client.files.content(batch.error_file_id)
                print(f"    Error details:\n{err_content.text[:500]}")

        elif batch.status in ("expired", "cancelled"):
            manifest.remove_batch_id(bid)
            print(f"    Batch {batch.status}. Removed from tracking.")

        else:
            elapsed = ""
            if batch.in_progress_at:
                mins = (time.time() - batch.in_progress_at) / 60
                elapsed = f" (running ~{mins:.0f} min)"
            print(f"    Still processing{elapsed}. Check again later.")


# ---------------------------------------------------------------------------
# Preview / dry-run
# ---------------------------------------------------------------------------

def run_preview(args, char: dict, variations: list[dict]):
    n = args.preview if args.preview else len(variations)
    for i, row in enumerate(variations[:n]):
        prompt = build_prompt(char, row)
        ph = prompt_hash(prompt)
        print(f"\n{'-'*60}")
        print(f"Row {i + 1} | Category: {row.get('category', 'general')} | Hash: {ph}")
        print(f"{'-'*60}")
        print(prompt)
    print(f"\n{'-'*60}")
    total = len(variations) * args.n
    display_cost(args, total)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch Image Generator — generate images from character templates + CSV variations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # Shared arguments
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--character", "-c", required=True, help="Path to character template JSON")
    shared.add_argument("--variations", "-v", required=True, help="Path to variations CSV")
    shared.add_argument("--model", default="gpt-image-1",
                        choices=["gpt-image-1", "gpt-image-1.5", "gpt-image-1-mini"],
                        help="OpenAI image model (default: gpt-image-1)")
    shared.add_argument("--quality", "-q", default="medium",
                        choices=["low", "medium", "high", "auto"],
                        help="Image quality (default: medium)")
    shared.add_argument("--size", "-s", default="1024x1536",
                        choices=["1024x1024", "1024x1536", "1536x1024", "auto"],
                        help="Image size (default: 1024x1536 portrait)")
    shared.add_argument("--format", "-f", default="webp",
                        choices=["webp", "png", "jpeg"],
                        help="Output format (default: webp)")
    shared.add_argument("--compression", type=int, default=90,
                        help="Compression 0-100 for webp/jpeg (default: 90)")
    shared.add_argument("--moderation", default="low",
                        choices=["low", "auto"],
                        help="Moderation level (default: low)")
    shared.add_argument("--n", type=int, default=1,
                        help="Images per variation row (default: 1)")
    shared.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT_DIR),
                        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    shared.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    shared.add_argument("--dry-run", action="store_true",
                        help="Print all prompts without generating")
    shared.add_argument("--preview", type=int, metavar="N",
                        help="Print first N prompts for quick validation")

    # parallel
    par = sub.add_parser("parallel", parents=[shared], help="Real-time parallel generation")
    par.add_argument("--concurrency", type=int, default=5,
                     help="Max concurrent requests (default: 5)")
    par.add_argument("--min-interval", type=float, default=0.5,
                     help="Min seconds between requests (default: 0.5)")

    # batch
    bat = sub.add_parser("batch", parents=[shared], help="Submit batch job (50%% cheaper)")
    bat.add_argument("--chunk-size", type=int, default=500,
                     help="Max rows per batch job (default: 500)")

    # batch-status
    bs = sub.add_parser("batch-status", parents=[shared], help="Check/download batch results")
    bs.add_argument("--batch-id", help="Specific batch ID (otherwise checks all pending)")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    char = load_character(args.character)
    variations = load_variations(args.variations)

    print(f"\nCharacter: {char['name']} | Variations: {len(variations)} rows | Mode: {args.command}")

    if args.preview or args.dry_run:
        run_preview(args, char, variations)
        if args.dry_run:
            print("Dry run complete. No images generated.")
        return

    if args.command == "parallel":
        asyncio.run(run_parallel(args, char, variations))
    elif args.command == "batch":
        run_batch(args, char, variations)
    elif args.command == "batch-status":
        run_batch_status(args, char, variations)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)
