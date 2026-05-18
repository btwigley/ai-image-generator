# -*- coding: utf-8 -*-
"""
AI Image Generator — Local Web App
Flask server that wraps the batch image generator with a visual UI,
reference image support, and real-time generation progress.

Usage:
  python app.py
  Open http://localhost:5000
"""

import base64
import csv
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import (Flask, jsonify, render_template, request,
                   send_from_directory)

from batch_image_generator import (
    BATCH_DISCOUNT, CHARACTERS_DIR, COST_PER_TOKEN, DEFAULT_OUTPUT_DIR,
    FAL_MODELS, MAX_RETRIES, BACKOFF_BASE, TOKEN_COUNTS,
    Manifest, build_prompt, estimate_cost, generate_fal, is_fal_model,
    make_custom_id, prompt_hash, save_image, save_image_from_url,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"
REFS_DIR = CHARACTERS_DIR / "refs"
ALLOWED_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _validate_name(name: str) -> bool:
    return bool(_SAFE_NAME_RE.match(name))


def _safe_resolve(base: Path, user_input: str) -> Path | None:
    """Resolve a user-provided path relative to base and verify containment."""
    try:
        resolved = (base / user_input).resolve()
    except (OSError, ValueError):
        return None
    if not str(resolved).startswith(str(base.resolve())):
        return None
    return resolved


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    defaults = {
        "api_key": "",
        "fal_key": "",
        "default_model": "gpt-image-1.5",
        "default_quality": "medium",
        "default_size": "1024x1536",
        "default_format": "webp",
        "default_compression": 90,
        "default_moderation": "low",
    }
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def save_settings(data: dict):
    _ALLOWED_SETTINGS = {
        "api_key", "fal_key", "default_model", "default_quality", "default_size",
        "default_format", "default_compression", "default_moderation",
    }
    current = load_settings()
    for k, v in data.items():
        if k in _ALLOWED_SETTINGS:
            current[k] = v
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)


def get_api_key(provider: str = "openai") -> str:
    settings = load_settings()
    if provider == "fal":
        return os.environ.get("FAL_KEY") or settings.get("fal_key", "")
    return os.environ.get("OPENAI_API_KEY") or settings.get("api_key", "")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/prompt-manager")
def prompt_manager():
    return render_template("prompt-manager.html")


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    s = load_settings()
    masked = {k: v for k, v in s.items() if k not in ("api_key", "fal_key")}

    openai_key = s.get("api_key", "")
    if openai_key:
        masked["api_key_display"] = openai_key[:7] + "..." + openai_key[-4:] if len(openai_key) > 12 else "***"
    else:
        masked["api_key_display"] = ""
    masked["has_openai_key"] = bool(openai_key or os.environ.get("OPENAI_API_KEY"))

    fal_key = s.get("fal_key", "")
    if fal_key:
        masked["fal_key_display"] = fal_key[:7] + "..." + fal_key[-4:] if len(fal_key) > 12 else "***"
    else:
        masked["fal_key_display"] = ""
    masked["has_fal_key"] = bool(fal_key or os.environ.get("FAL_KEY"))

    masked["has_key"] = masked["has_openai_key"] or masked["has_fal_key"]
    return jsonify(masked)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid request"}), 400
    save_settings(data)
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Characters API
# ---------------------------------------------------------------------------

@app.route("/api/characters", methods=["GET"])
def api_list_characters():
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    chars = []
    for f in sorted(CHARACTERS_DIR.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        ref_dir = REFS_DIR / f.stem
        ref_count = len(list(ref_dir.glob("*"))) if ref_dir.exists() else 0
        csv_path = CHARACTERS_DIR / f"{f.stem}_variations.csv"
        var_count = 0
        if csv_path.exists():
            with open(csv_path, "r", encoding="utf-8", newline="") as ch:
                var_count = max(0, sum(1 for _ in ch) - 1)
        chars.append({
            "name": data.get("name", f.stem),
            "filename": f.stem,
            "tags": data.get("tags", []),
            "ref_count": ref_count,
            "var_count": var_count,
        })
    return jsonify(chars)


@app.route("/api/characters/<name>", methods=["GET"])
def api_get_character(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    p = _safe_resolve(CHARACTERS_DIR, f"{name}.json")
    if not p or not p.exists():
        return jsonify({"error": "Not found"}), 404
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "Failed to parse character file"}), 500
    return jsonify(data)


@app.route("/api/characters", methods=["POST"])
def api_save_character():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid request"}), 400
    name = data.get("name", "").strip().lower().replace(" ", "_")
    if not name or not _validate_name(name):
        return jsonify({"error": "Name is required and must be alphanumeric/underscore/hyphen"}), 400
    p = _safe_resolve(CHARACTERS_DIR, f"{name}.json")
    if not p:
        return jsonify({"error": "Invalid name"}), 400
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return jsonify({"success": True, "filename": name})


@app.route("/api/characters/<name>", methods=["DELETE"])
def api_delete_character(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    p = _safe_resolve(CHARACTERS_DIR, f"{name}.json")
    if p and p.exists():
        p.unlink()
    csv_p = _safe_resolve(CHARACTERS_DIR, f"{name}_variations.csv")
    if csv_p and csv_p.exists():
        csv_p.unlink()
    ref_dir = REFS_DIR / name
    resolved_ref = ref_dir.resolve()
    if resolved_ref.exists() and str(resolved_ref).startswith(str(REFS_DIR.resolve())):
        shutil.rmtree(resolved_ref)
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# References API
# ---------------------------------------------------------------------------

@app.route("/api/characters/<name>/references", methods=["GET"])
def api_list_references(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    ref_dir = REFS_DIR / name
    if not ref_dir.exists():
        return jsonify([])
    refs = []
    for f in sorted(ref_dir.iterdir()):
        if f.suffix.lower() in ALLOWED_IMG_EXT:
            refs.append({"filename": f.name, "size": f.stat().st_size})
    return jsonify(refs)


@app.route("/api/characters/<name>/references", methods=["POST"])
def api_upload_references(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    ref_dir = REFS_DIR / name
    ref_dir.mkdir(parents=True, exist_ok=True)
    files = request.files.getlist("files")
    saved = []
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_IMG_EXT:
            continue
        idx = len(list(ref_dir.iterdir())) + 1
        filename = f"ref_{idx:03d}{ext}"
        f.save(str(ref_dir / filename))
        saved.append(filename)
    return jsonify({"success": True, "saved": saved})


@app.route("/api/characters/<name>/references/<filename>", methods=["DELETE"])
def api_delete_reference(name, filename):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    if not re.match(r"^ref_\d{3}\.(png|jpg|jpeg|webp)$", filename):
        return jsonify({"error": "Invalid filename"}), 400
    p = _safe_resolve(REFS_DIR / name, filename)
    if p and p.exists():
        p.unlink()
    return jsonify({"success": True})


@app.route("/api/refs/<name>/<filename>")
def api_serve_reference(name, filename):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    ref_dir = (REFS_DIR / name).resolve()
    if not str(ref_dir).startswith(str(REFS_DIR.resolve())):
        return jsonify({"error": "Invalid path"}), 400
    return send_from_directory(str(ref_dir), filename)


# ---------------------------------------------------------------------------
# Variations API
# ---------------------------------------------------------------------------

@app.route("/api/variations/<name>", methods=["GET"])
def api_get_variations(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    csv_path = _safe_resolve(CHARACTERS_DIR, f"{name}_variations.csv")
    if not csv_path or not csv_path.exists():
        return jsonify([])
    rows = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return jsonify(rows)


@app.route("/api/variations/<name>", methods=["POST"])
def api_save_variations(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    rows = request.get_json()
    if not rows or not isinstance(rows, list):
        return jsonify({"error": "No data"}), 400
    csv_path = _safe_resolve(CHARACTERS_DIR, f"{name}_variations.csv")
    if not csv_path:
        return jsonify({"error": "Invalid name"}), 400
    fieldnames = ["scene", "outfit", "pose", "location", "camera", "category", "emotion", "lighting"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if isinstance(row, dict):
                writer.writerow(row)
    return jsonify({"success": True, "count": len(rows)})


@app.route("/api/variations/<name>/import", methods=["POST"])
def api_import_variations(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        content = f.read().decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "File must be UTF-8 encoded"}), 400
    reader = csv.DictReader(io.StringIO(content))
    rows = [dict(r) for r in reader]
    csv_path = _safe_resolve(CHARACTERS_DIR, f"{name}_variations.csv")
    if not csv_path:
        return jsonify({"error": "Invalid name"}), 400
    fieldnames = ["scene", "outfit", "pose", "location", "camera", "category", "emotion", "lighting"]
    with open(csv_path, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return jsonify({"success": True, "count": len(rows), "rows": rows})


# ---------------------------------------------------------------------------
# Preview / Estimate API
# ---------------------------------------------------------------------------

@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400
    char = data.get("character", {})
    variations = data.get("variations", [])
    limit = min(data.get("limit", 5), 500)

    if not char.get("core") and char.get("identity"):
        identity = char["identity"]
        parts = []
        age = identity.get("age")
        if age is not None:
            parts.append(f"A {age}-year-old person")
        for field in ("hair", "eyes", "skin", "body", "face"):
            val = identity.get(field)
            if val:
                parts.append(str(val))
        char["core"] = ", ".join(parts) if parts else ""

    char.setdefault("core", "")
    char.setdefault("style", "")
    char.setdefault("negative", "")

    if not char.get("core"):
        return jsonify({"error": "Character must have a core description or identity traits"}), 400

    prompts = []
    for row in variations[:limit]:
        if not isinstance(row, dict) or not row.get("scene") or not row.get("outfit"):
            continue
        prompt = build_prompt(char, row)
        prompts.append({
            "prompt": prompt,
            "hash": prompt_hash(prompt),
            "category": row.get("category", "general"),
        })
    return jsonify(prompts)


@app.route("/api/estimate", methods=["POST"])
def api_estimate():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400
    model = data.get("model", "gpt-image-1")
    quality = data.get("quality", "medium")
    size = data.get("size", "1024x1536")
    count = max(0, min(data.get("count", 1), 10000))
    n = max(1, min(data.get("n", 1), 4))
    total = count * n
    direct = estimate_cost(model, quality, size, total, False)
    batch = estimate_cost(model, quality, size, total, True)
    return jsonify({
        "rows": count,
        "images_per_row": n,
        "total_images": total,
        "cost_direct": round(direct, 2),
        "cost_batch": round(batch, 2),
    })


# ---------------------------------------------------------------------------
# Generation API (background threaded)
# ---------------------------------------------------------------------------

def _generation_worker(job_id: str, char: dict, variations: list[dict],
                       gen_settings: dict, ref_paths: list[str]):
    """Run image generation in a background thread."""
    try:
        _generation_worker_inner(job_id, char, variations, gen_settings, ref_paths)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[GENERATION ERROR] Job {job_id}: {e}\n{tb}")
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)[:500]
                _jobs[job_id]["log"].append(f"Fatal: {str(e)[:200]}")


def _generation_worker_inner(job_id: str, char: dict, variations: list[dict],
                              gen_settings: dict, ref_paths: list[str]):
    """Core generation logic, called from the error-handling wrapper."""
    output_dir = DEFAULT_OUTPUT_DIR
    manifest = Manifest(output_dir)

    model = gen_settings.get("model", "gpt-image-1.5")
    quality = gen_settings.get("quality", "medium")
    size = gen_settings.get("size", "1024x1536")
    fmt = gen_settings.get("format", "webp")
    compression = gen_settings.get("compression", 90)
    moderation = gen_settings.get("moderation", "low")
    n_per_row = max(1, min(gen_settings.get("n", 1), 4))
    use_refs = bool(ref_paths) and gen_settings.get("use_references", True)
    using_fal = is_fal_model(model)

    if using_fal:
        fal_key = get_api_key("fal")
        if not fal_key:
            raise ValueError("No fal.ai API key configured. Set it in Settings.")
        os.environ["FAL_KEY"] = fal_key
        client = None
    else:
        from openai import OpenAI
        api_key = get_api_key("openai")
        if not api_key:
            raise ValueError("No OpenAI API key configured. Set it in Settings.")
        client = OpenAI(api_key=api_key)

    if not char.get("core") and char.get("identity"):
        identity = char["identity"]
        parts = []
        age = identity.get("age")
        if age is not None:
            parts.append(f"A {age}-year-old person")
        for field in ("hair", "eyes", "skin", "body", "face"):
            val = identity.get(field)
            if val:
                parts.append(str(val))
        char["core"] = ", ".join(parts) if parts else ""

    char.setdefault("core", "")
    char.setdefault("style", "")
    char.setdefault("negative", "")

    if not char.get("core"):
        raise ValueError("Character must have a core description or identity traits.")

    char_name = char.get("name", "character")
    if not _validate_name(char_name):
        char_name = re.sub(r"[^a-zA-Z0-9_-]", "_", char_name)[:64] or "character"

    valid_rows = [(i, row) for i, row in enumerate(variations)
                  if isinstance(row, dict) and row.get("scene") and row.get("outfit")]

    jobs_to_run = []
    for i, row in valid_rows:
        prompt = build_prompt(char, row)
        ph = prompt_hash(prompt)
        if manifest.has_hash(ph):
            continue
        jobs_to_run.append((i, row, prompt, ph))

    total = len(jobs_to_run)
    with _jobs_lock:
        if job_id not in _jobs:
            return
        _jobs[job_id]["total"] = total
        _jobs[job_id]["status"] = "running"

    if total == 0:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["status"] = "completed"
        return

    file_counter = max(
        (int(k.split("_")[-1]) for k in manifest.data
         if k != "_meta" and "_" in k and k.split("_")[-1].isdigit()),
        default=0
    ) + 1

    completed = 0
    failed = 0
    provider_label = f"fal.ai/{model}" if using_fal else model
    ref_info = f", refs={len(ref_paths)}, mode=edit" if use_refs else ", refs=off, mode=generate"
    print(f"[GEN] Starting {total} images for '{char_name}' (model={provider_label}, size={size}{ref_info})")

    for idx, row, prompt, ph in jobs_to_run:
        with _jobs_lock:
            if job_id not in _jobs or _jobs[job_id].get("cancelled"):
                with _jobs_lock:
                    if job_id in _jobs:
                        _jobs[job_id]["status"] = "cancelled"
                return

        for attempt in range(MAX_RETRIES):
            try:
                category = row.get("category", "general")

                if using_fal:
                    urls, actual_fmt = generate_fal(
                        prompt=prompt, model=model, size=size,
                        fmt=fmt, num_images=n_per_row, safety=False,
                    )
                    generated_paths = []
                    for img_i, url in enumerate(urls):
                        fid = file_counter
                        file_counter += 1
                        filepath = save_image_from_url(url, output_dir, char_name, category, fid, actual_fmt)
                        generated_paths.append(str(filepath.relative_to(output_dir)))
                        key = f"{char_name}_{fid:04d}"
                        manifest.add(key, {
                            "custom_id": make_custom_id(char_name, row, idx, img_i),
                            "filename": str(filepath.relative_to(output_dir)),
                            "prompt_hash": ph,
                            "model": model,
                            "quality": quality,
                            "size": size,
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "batch_id": None,
                        })
                else:
                    edit_moderation_fail = False
                    if use_refs:
                        ref_prompt = prompt + (
                            "\n\nThis is the same person shown in the reference images. "
                            "Match her exact face, body type, proportions, hair color, "
                            "and skin tone. She must look identical."
                        )
                        ref_files = [open(rp, "rb") for rp in ref_paths]
                        try:
                            params = {
                                "model": model,
                                "image": ref_files,
                                "prompt": ref_prompt,
                                "n": n_per_row,
                                "size": size,
                                "quality": quality,
                            }
                            if fmt != "png":
                                params["output_format"] = fmt
                            result = client.images.edit(**params)
                        except Exception as edit_err:
                            edit_err_str = str(edit_err).lower()
                            if "content_policy" in edit_err_str or "moderation" in edit_err_str or "safety" in edit_err_str:
                                edit_moderation_fail = True
                                print(f"[GEN] Row {idx+1}: Edit mode blocked by moderation (refs flagged), falling back to generate mode")
                                print(f"[GEN]   Error: {str(edit_err)[:150]}")
                            else:
                                raise edit_err
                        finally:
                            for rf in ref_files:
                                rf.close()

                    if not use_refs or edit_moderation_fail:
                        params = {
                            "model": model,
                            "prompt": prompt,
                            "n": n_per_row,
                            "size": size,
                            "quality": quality,
                            "moderation": moderation,
                        }
                        if fmt != "png":
                            params["output_format"] = fmt
                        if compression and fmt in ("webp", "jpeg"):
                            params["output_compression"] = compression
                        result = client.images.generate(**params)

                    images_b64 = [img.b64_json for img in result.data if img.b64_json]
                    generated_paths = []
                    gen_mode = "generate (fallback)" if edit_moderation_fail else ("edit" if use_refs else "generate")
                    for img_i, b64 in enumerate(images_b64):
                        fid = file_counter
                        file_counter += 1
                        filepath = save_image(b64, output_dir, char_name, category, fid, fmt)
                        generated_paths.append(str(filepath.relative_to(output_dir)))
                        key = f"{char_name}_{fid:04d}"
                        manifest.add(key, {
                            "custom_id": make_custom_id(char_name, row, idx, img_i),
                            "filename": str(filepath.relative_to(output_dir)),
                            "prompt_hash": ph,
                            "model": model,
                            "quality": quality,
                            "size": size,
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "batch_id": None,
                        })

                completed += 1
                with _jobs_lock:
                    if job_id in _jobs:
                        _jobs[job_id]["completed"] = completed
                        _jobs[job_id]["failed"] = failed
                        _jobs[job_id]["last_images"] = generated_paths
                        if edit_moderation_fail:
                            _jobs[job_id]["log"].append(f"Row {idx+1}: Refs blocked, used generate mode instead")
                print(f"[GEN] {completed}/{total} done - Row {idx+1} ({category}) [{gen_mode}]")
                break

            except Exception as e:
                err_str = str(e).lower()
                if "content_policy" in err_str or "moderation" in err_str or "safety" in err_str:
                    failed += 1
                    with _jobs_lock:
                        if job_id in _jobs:
                            _jobs[job_id]["failed"] = failed
                            _jobs[job_id]["log"].append(f"Row {idx+1}: Safety/moderation refusal")
                    print(f"[GEN] Row {idx+1}: Safety/moderation refusal - {str(e)[:150]}")
                    break
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE ** (attempt + 1)
                    print(f"[GEN] Row {idx+1}: Retry {attempt+1}/{MAX_RETRIES} in {wait}s - {str(e)[:80]}")
                    time.sleep(wait)
                else:
                    failed += 1
                    with _jobs_lock:
                        if job_id in _jobs:
                            _jobs[job_id]["failed"] = failed
                            _jobs[job_id]["log"].append(f"Row {idx+1}: {str(e)[:100]}")
                    print(f"[GEN] Row {idx+1}: FAILED after {MAX_RETRIES} retries - {str(e)[:80]}")

        time.sleep(0.3)

    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["completed"] = completed
            _jobs[job_id]["failed"] = failed
    print(f"[GEN] Job complete: {completed} succeeded, {failed} failed")


@app.route("/api/generate", methods=["POST"])
def api_start_generation():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400
    char = data.get("character", {})
    variations = data.get("variations", [])
    gen_settings = data.get("settings", {})
    char_name = char.get("name", "character").strip().lower().replace(" ", "_")
    if not _validate_name(char_name):
        char_name = re.sub(r"[^a-zA-Z0-9_-]", "_", char_name)[:64] or "character"

    char_id = data.get("character_id", "").strip()
    if not _validate_name(char_id):
        char_id = char_name

    ref_dir = REFS_DIR / char_id
    ref_paths = []
    if ref_dir.exists() and str(ref_dir.resolve()).startswith(str(REFS_DIR.resolve())):
        ref_paths = [str(f) for f in sorted(ref_dir.iterdir())
                     if f.suffix.lower() in ALLOWED_IMG_EXT]

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "starting",
            "total": 0,
            "completed": 0,
            "failed": 0,
            "log": [],
            "last_images": [],
            "cancelled": False,
        }

    thread = threading.Thread(
        target=_generation_worker,
        args=(job_id, char, variations, gen_settings, ref_paths),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/generate/<job_id>", methods=["GET"])
def api_generation_status(job_id):
    if not re.match(r"^[a-f0-9]{8}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/generate/<job_id>/cancel", methods=["POST"])
def api_cancel_generation(job_id):
    if not re.match(r"^[a-f0-9]{8}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["cancelled"] = True
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Gallery API
# ---------------------------------------------------------------------------

@app.route("/api/gallery/<name>", methods=["GET"])
def api_gallery(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    char_dir = _safe_resolve(DEFAULT_OUTPUT_DIR, name)
    if not char_dir or not char_dir.exists():
        return jsonify([])
    images = []
    for f in sorted(char_dir.rglob("*")):
        if f.suffix.lower() in ALLOWED_IMG_EXT:
            if not str(f.resolve()).startswith(str(DEFAULT_OUTPUT_DIR.resolve())):
                continue
            rel = f.relative_to(DEFAULT_OUTPUT_DIR)
            category = f.parent.name if f.parent != char_dir else "general"
            images.append({
                "filename": f.name,
                "path": str(rel).replace("\\", "/"),
                "category": category,
                "size": f.stat().st_size,
            })
    return jsonify(images)


@app.route("/api/gallery/image/<path:filepath>")
def api_serve_gallery_image(filepath):
    resolved = _safe_resolve(DEFAULT_OUTPUT_DIR, filepath)
    if not resolved or not resolved.exists():
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(str(resolved.parent), resolved.name)


@app.route("/api/gallery/<name>/delete", methods=["POST"])
def api_delete_gallery_images(name):
    if not _validate_name(name):
        return jsonify({"error": "Invalid name"}), 400
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400
    paths = data.get("paths", [])
    if not isinstance(paths, list):
        return jsonify({"error": "paths must be a list"}), 400
    deleted = 0
    for p in paths:
        if not isinstance(p, str):
            continue
        full = _safe_resolve(DEFAULT_OUTPUT_DIR, p)
        if full and full.exists() and full.is_file():
            full.unlink()
            deleted += 1
    return jsonify({"success": True, "deleted": deleted})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import webbrowser, threading as _tb
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n  AI Image Generator")
    print(f"  http://localhost:5000\n")
    _tb.Timer(1.0, webbrowser.open, args=["http://localhost:5000"]).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
