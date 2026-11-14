"""Regression tests for the generator's output safety and job control.

No provider is contacted: the OpenAI client and fal.ai calls are stubbed, and
every file goes to a pytest tmp_path.
"""

import base64
import json
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as webapp  # noqa: E402
import batch_image_generator as big  # noqa: E402

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
CHAR = {"name": "audit", "core": "A small test robot.", "style": "", "negative": ""}


@pytest.fixture
def out(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    monkeypatch.setattr(webapp, "DEFAULT_OUTPUT_DIR", out_dir)
    monkeypatch.setattr(webapp.time, "sleep", lambda s: None)
    monkeypatch.setattr(webapp, "get_api_key", lambda provider="openai": "test-key")
    webapp._jobs.clear()
    return out_dir


def _new_job(job_id):
    with webapp._jobs_lock:
        webapp._jobs[job_id] = {"status": "starting", "total": 0, "completed": 0, "failed": 0,
                                "log": [], "last_images": [], "cancelled": False}


def _fake_openai(monkeypatch, on_call=None):
    calls = []

    class Images:
        def generate(self, **params):
            calls.append(params)
            if on_call:
                on_call()
            return types.SimpleNamespace(data=[types.SimpleNamespace(b64_json=PNG_B64)])

        edit = generate

    class OpenAI:
        def __init__(self, api_key=None):
            self.images = Images()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=OpenAI))
    return calls


def _row(scene, category="morning"):
    return {"scene": scene, "outfit": "a red scarf", "category": category}


# ── OPEN_SOURCE-01: concurrent jobs keep every paid image ────────

def test_two_simultaneous_jobs_keep_both_images(out, monkeypatch):
    # Without serialization both jobs reach the provider together and pick
    # the same file index; the barrier makes that overlap certain.
    barrier = threading.Barrier(2, timeout=0.3)

    def overlap():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass  # the second job is waiting for the first: no overlap

    _fake_openai(monkeypatch, on_call=overlap)
    settings = {"model": "gpt-image-1", "format": "png", "use_references": False}
    threads = []
    for job_id, scene in (("aaaa0001", "reading a map"), ("aaaa0002", "watering plants")):
        _new_job(job_id)
        t = threading.Thread(target=webapp._generation_worker,
                             args=(job_id, dict(CHAR), [_row(scene)], settings, []), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    files = sorted(p.name for p in (out / "audit" / "morning").iterdir())
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entries = {k: v for k, v in manifest.items() if k != "_meta"}
    assert len(files) == 2
    assert len(entries) == 2
    assert len({e["prompt_hash"] for e in entries.values()}) == 2
    assert sorted(Path(e["filename"]).name for e in entries.values()) == files


def test_save_image_never_overwrites_an_existing_file(tmp_path):
    first = big.save_image(PNG_B64, tmp_path, "audit", "morning", 1, "png")
    first.write_bytes(b"earlier paid result")
    second = big.save_image(PNG_B64, tmp_path, "audit", "morning", 1, "png")
    assert first != second
    assert first.read_bytes() == b"earlier paid result"
    assert big.file_index_of(second) == 2


# ── OPEN_SOURCE-04: cancel does not deadlock ─────────────────────

def test_cancel_lets_the_worker_exit_and_frees_the_lock(out, monkeypatch):
    _fake_openai(monkeypatch)
    _new_job("bbbb0001")
    webapp._jobs["bbbb0001"]["cancelled"] = True
    t = threading.Thread(target=webapp._generation_worker,
                         args=("bbbb0001", dict(CHAR), [_row("a"), _row("b")],
                               {"model": "gpt-image-1"}, []), daemon=True)
    t.start()
    t.join(timeout=3)
    assert not t.is_alive()
    assert webapp._jobs_lock.acquire(timeout=1)
    webapp._jobs_lock.release()
    assert webapp._jobs["bbbb0001"]["status"] == "cancelled"


# ── OPEN_SOURCE-10: output boundary ──────────────────────────────

def test_safe_resolve_uses_real_ancestry(tmp_path):
    base = tmp_path / "output"
    (base / "audit" / "morning").mkdir(parents=True)
    (tmp_path / "output-private").mkdir()
    assert webapp._safe_resolve(base, "../output-private/synthetic.txt") is None
    assert webapp._safe_resolve(base, str(tmp_path / "output-private" / "x")) is None
    assert webapp._safe_resolve(base, "audit/morning/audit_0001.png") is not None


def test_gallery_cannot_read_or_delete_a_sibling_folder(out):
    private = out.parent / "output-private"
    private.mkdir()
    secret = private / "synthetic.txt"
    secret.write_text("synthetic")
    (out / "audit").mkdir()
    (out / "audit" / "audit_0001.png").write_bytes(b"img")

    client = webapp.app.test_client()
    assert client.get("/api/gallery/image/../output-private/synthetic.txt").status_code == 404
    ok = client.get("/api/gallery/image/audit/audit_0001.png")
    assert ok.status_code == 200 and ok.data == b"img"
    ok.close()
    resp = client.post("/api/gallery/audit/delete", json={"paths": ["../output-private/synthetic.txt"]})
    assert resp.get_json()["deleted"] == 0
    assert secret.exists()


@pytest.mark.parametrize("category", ["../../escaped", "..", "a/b", "C:\\temp", "/abs"])
def test_traversal_categories_are_refused(tmp_path, category):
    with pytest.raises(ValueError):
        big.save_image(PNG_B64, tmp_path / "output", "audit", category, 1, "png")


def test_ordinary_categories_still_save(tmp_path):
    p = big.save_image(PNG_B64, tmp_path, "audit", "night market", 1, "png")
    assert p.parent.name == "night market"


@pytest.mark.parametrize("name", ["_draft", "-draft", "___"])
def test_names_the_web_ui_accepts_save_with_one_paid_call(out, monkeypatch, name):
    calls = _fake_openai(monkeypatch)
    _new_job("dddd0001")
    webapp._generation_worker("dddd0001", dict(CHAR, name=name), [_row("a")],
                              {"model": "gpt-image-1", "format": "png"}, [])
    job = webapp._jobs["dddd0001"]
    assert len(calls) == 1
    assert (job["completed"], job["failed"]) == (1, 0)
    assert len(list((out / name / "morning").iterdir())) == 1


@pytest.mark.parametrize("name", ["CON", "nul"])
def test_an_unusable_folder_name_fails_before_any_paid_call(out, monkeypatch, name):
    calls = _fake_openai(monkeypatch)
    _new_job("dddd0002")
    webapp._generation_worker("dddd0002", dict(CHAR, name=name), [_row("a")],
                              {"model": "gpt-image-1", "format": "png"}, [])
    assert calls == []
    assert webapp._jobs["dddd0002"]["status"] == "error"


# ── Review R1-03: separate writers keep each other's manifest entries ──

def test_two_manifest_writers_merge_instead_of_overwriting(tmp_path):
    first, second = big.Manifest(tmp_path), big.Manifest(tmp_path)   # both loaded before either saves
    first.add("a_0001", {"filename": "a/x/a_0001.png", "prompt_hash": "h1"})
    second.add("a_0002", {"filename": "a/x/a_0002.png", "prompt_hash": "h2"})
    second.add_batch_ids(["b2"])
    first.add_batch_ids(["b1"])
    fresh = big.Manifest(tmp_path)
    assert fresh.has_hash("h1") and fresh.has_hash("h2")
    assert sorted(fresh.get_pending_batches()) == ["b1", "b2"]
    first.remove_batch_id("b1")
    assert big.Manifest(tmp_path).get_pending_batches() == ["b2"]
    assert not list(tmp_path.glob("manifest.json.*"))   # no lock or temp file left behind


def test_two_writers_in_different_categories_keep_both_records(tmp_path):
    # Both writers start from the same snapshot, so both allocate index 1;
    # the images land in different category folders as audit_0001 each.
    first, second = big.Manifest(tmp_path), big.Manifest(tmp_path)
    assert first.next_index() == second.next_index() == 1
    for m, category, ph in ((first, "morning", "h-morning"), (second, "night", "h-night")):
        path = big.save_image(PNG_B64, tmp_path, "audit", category, m.next_index(), "png")
        m.add(big.manifest_key(tmp_path, path), {"filename": path.relative_to(tmp_path).as_posix(),
                                                 "prompt_hash": ph})
    fresh = big.Manifest(tmp_path)
    assert fresh.has_hash("h-morning") and fresh.has_hash("h-night")
    assert sorted(k for k in fresh.data if k != "_meta") == ["audit/morning/audit_0001.png",
                                                             "audit/night/audit_0001.png"]


def test_two_writers_in_one_category_with_different_formats_keep_both_records(tmp_path):
    first, second = big.Manifest(tmp_path), big.Manifest(tmp_path)
    for m, fmt, ph in ((first, "png", "h-png"), (second, "webp", "h-webp")):
        path = big.save_image(PNG_B64, tmp_path, "audit", "morning", m.next_index(), fmt)
        m.add(big.manifest_key(tmp_path, path), {"filename": path.relative_to(tmp_path).as_posix(),
                                                 "prompt_hash": ph})
    fresh = big.Manifest(tmp_path)
    assert fresh.has_hash("h-png") and fresh.has_hash("h-webp")
    assert len([k for k in fresh.data if k != "_meta"]) == 2


def test_next_index_ignores_keys_that_are_not_saved_images(tmp_path):
    m = big.Manifest(tmp_path)
    m.add("audit/morning/audit_0007", {"filename": "audit/morning/audit_0007.png", "prompt_hash": "h"})
    m.add("unsaved_audit_0003_1794687003", {"status": "unsaved", "prompt_hash": "h2", "urls": ["x"]})
    m.add("pending_audit_morning_0099", {"status": "pending", "prompt_hash": "h3", "batch_id": "b"})
    assert big.Manifest(tmp_path).next_index() == 8


def test_an_openai_item_without_bytes_makes_the_row_partial(out, monkeypatch):
    class Images:
        def generate(self, **params):
            return types.SimpleNamespace(data=[types.SimpleNamespace(b64_json=PNG_B64),
                                               types.SimpleNamespace(b64_json=None)])

    class OpenAI:
        def __init__(self, api_key=None):
            self.images = Images()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=OpenAI))
    _new_job("ffff0001")
    row = _row("a")
    webapp._generation_worker("ffff0001", dict(CHAR), [row], {"model": "gpt-image-1", "format": "png", "n": 2}, [])
    job = webapp._jobs["ffff0001"]
    assert (job["completed"], job["failed"]) == (0, 1)
    assert not big.Manifest(out).has_hash(big.prompt_hash(big.build_prompt(dict(CHAR), row)))


# ── Review R1-05: every image of one paid call is saved, or the row says so ──

def test_second_image_download_is_retried_without_paying_again(out, monkeypatch):
    calls, fails = [], {"left": 1}
    monkeypatch.setattr(webapp, "generate_fal",
                        lambda **kw: calls.append(kw) or (["https://x.invalid/1.png", "https://x.invalid/2.png"], "png"))

    def flaky_save(url, *args):
        if url.endswith("2.png") and fails["left"]:
            fails["left"] -= 1
            raise TimeoutError("synthetic transient download failure")
        return _fake_fal_save(url, *args)

    monkeypatch.setattr(webapp, "save_image_from_url", flaky_save)
    _new_job("eeee0001")
    webapp._generation_worker("eeee0001", dict(CHAR), [_row("a")], {"model": "flux-dev", "n": 2}, [])
    job = webapp._jobs["eeee0001"]
    assert len(calls) == 1
    assert (job["completed"], job["failed"]) == (1, 0)
    assert len(list((out / "audit" / "morning").iterdir())) == 2


def test_an_unsaved_image_reports_the_row_partial_and_retryable(out, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp, "generate_fal",
                        lambda **kw: calls.append(kw) or (["https://x.invalid/1.png", "https://x.invalid/2.png"], "png"))

    def broken_second(url, *args):
        if url.endswith("2.png"):
            raise TimeoutError("synthetic permanent download failure")
        return _fake_fal_save(url, *args)

    monkeypatch.setattr(webapp, "save_image_from_url", broken_second)
    _new_job("eeee0002")
    row = _row("a")
    webapp._generation_worker("eeee0002", dict(CHAR), [row], {"model": "flux-dev", "n": 2}, [])
    job = webapp._jobs["eeee0002"]
    assert len(calls) == 1
    assert (job["completed"], job["failed"]) == (0, 1)
    assert any("1 of 2 image(s) saved" in line for line in job["log"])
    manifest = big.Manifest(out)
    ph = big.prompt_hash(big.build_prompt(dict(CHAR), row))
    assert not manifest.has_hash(ph)                       # the row can be generated again
    unsaved = [e for k, e in manifest.data.items() if k.startswith("unsaved_")]
    assert unsaved and unsaved[0]["urls"] == ["https://x.invalid/2.png"]


# ── OPEN_SOURCE-15: a fal.ai success is one success ──────────────

def _fake_fal_save(url, output_dir, char_name, category, file_idx, fmt):
    folder = big.image_folder(output_dir, char_name, category)
    return big._write_new(folder, char_name, file_idx, fmt, b"fal image")


def test_fal_success_counts_once_and_no_refusal(out, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp, "generate_fal", lambda **kw: calls.append(kw) or (["https://x.invalid/i.png"], "png"))
    monkeypatch.setattr(webapp, "save_image_from_url", _fake_fal_save)
    _new_job("cccc0001")
    webapp._generation_worker("cccc0001", dict(CHAR), [_row("a")], {"model": "flux-dev"}, [])
    job = webapp._jobs["cccc0001"]
    assert len(calls) == 1
    assert (job["completed"], job["failed"], job["status"]) == (1, 0, "completed")
    assert not any("moderation" in line.lower() for line in job["log"])


def test_fal_refusal_counts_one_failure(out, monkeypatch):
    def refuse(**kw):
        raise RuntimeError("content_policy_violation: synthetic refusal")

    monkeypatch.setattr(webapp, "generate_fal", refuse)
    _new_job("cccc0002")
    webapp._generation_worker("cccc0002", dict(CHAR), [_row("a")], {"model": "flux-dev"}, [])
    job = webapp._jobs["cccc0002"]
    assert (job["completed"], job["failed"]) == (0, 1)
    assert not (out / "audit").exists()


def test_local_error_is_not_a_provider_refusal():
    try:
        edit_moderation_fail  # noqa: F821 - the exact shape of the original bug
    except NameError as e:
        assert not webapp._is_provider_refusal(e)
    assert webapp._is_provider_refusal(RuntimeError("Your request was rejected by the safety system"))


# ── OPEN_SOURCE-22: finished batches free their unresolved rows ──

class _Batch(types.SimpleNamespace):
    pass


def test_expired_batch_keeps_completed_images_and_frees_the_rest(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    m = big.Manifest(out_dir)
    m.add("pending_audit_morning_0000", {"custom_id": "audit_morning_0000", "prompt_hash": "h-done",
                                         "batch_id": "b1", "category": "morning", "status": "pending"})
    m.add("pending_audit_morning_0001", {"custom_id": "audit_morning_0001", "prompt_hash": "h-lost",
                                         "batch_id": "b1", "category": "morning", "status": "pending"})
    m.add_batch_ids(["b1"])
    assert m.has_hash("h-done") and m.has_hash("h-lost")   # tracked: not regenerated

    output = json.dumps({"custom_id": "audit_morning_0000",
                         "response": {"body": {"data": [{"b64_json": PNG_B64}]}}})

    class Client:
        class batches:
            @staticmethod
            def retrieve(bid):
                return _Batch(status="expired", output_file_id="file-out", error_file_id=None,
                              request_counts=_Batch(total=2, completed=1, failed=0))

        class files:
            @staticmethod
            def content(file_id):
                return _Batch(text=output)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=lambda api_key=None: Client()))
    monkeypatch.setattr(big, "load_api_key", lambda provider="openai": "test-key")
    args = types.SimpleNamespace(output=str(out_dir), batch_id=None, format="png",
                                 model="gpt-image-1", quality="medium", size="1024x1024")
    big.run_batch_status(args, {"name": "audit"}, [])

    after = big.Manifest(out_dir)
    assert after.get_pending_batches() == []
    assert after.has_hash("h-done")          # the paid image was saved and still dedupes
    assert not after.has_hash("h-lost")      # the unresolved row can be generated again
    assert len(list((out_dir / "audit" / "morning").iterdir())) == 1


def test_untracked_pending_rows_do_not_block_a_rerun(tmp_path):
    m = big.Manifest(tmp_path)
    m.add("pending_x", {"prompt_hash": "h", "batch_id": "gone", "status": "pending"})
    assert not m.has_hash("h")


# ── OPEN_SOURCE-25: the character's style reaches the prompt ─────

def test_character_style_is_used_and_replaces_the_photo_tier():
    char = dict(CHAR, style="Watercolor storybook illustration, soft paper texture")
    prompt = big.build_prompt(char, _row("flying a kite", "selfies"))
    assert "Watercolor storybook illustration" in prompt
    assert "smartphone" not in prompt.lower()
    assert "Realism:" not in prompt


def test_no_style_falls_back_to_the_category_tier():
    prompt = big.build_prompt(dict(CHAR), _row("flying a kite", "professional"))
    assert big.STYLE_TIERS["professional"]["style"] in prompt
