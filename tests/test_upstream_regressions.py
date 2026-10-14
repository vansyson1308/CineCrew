# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""
Regression tests for defects confirmed in the audit of upstream 3eac773
(docs/studio/AUDIT.md). Every test here failed on that commit.
"""

import io
import contextlib
from pathlib import Path
from typing import List

from src.adapters.video_client import VideoResult
from src.agents.production_operator import ProductionOperatorAgent
from src.schemas.critic import VisualReview

from .conftest import make_shot


def _quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# ── F2: one-segment dialogue shots lost their line ─────────────────────────

def test_short_dialogue_shot_keeps_line_and_voice(library, blueprint_factory):
    for total in (2.0, 6.0):  # the two (total, core) cases the brief reproduced
        bp = blueprint_factory([make_shot("s1", total, "Anh giao hàng đến rồi đây!")])
        batch = _quiet(ProductionOperatorAgent(fps=16).build_from_blueprint, bp, library)
        spoken = [j for j in batch.jobs if j.dialogue]
        assert len(spoken) == 1, f"total={total}: line dropped, roles={[j.extra['role'] for j in batch.jobs]}"
        assert spoken[0].voice == "alloy"
        assert spoken[0].extra["role"] in ("dialogue_core", "dialogue_single")
        assert "prelude" not in spoken[0].extra["role"]
        assert abs(sum(j.duration for j in batch.jobs) - total) < 1e-6


def test_every_example_dialogue_line_reaches_a_job(library):
    """All shipped examples: every dialogue shot must produce exactly one job carrying its line."""
    import glob
    from src.schemas.assets import AssetLibrary
    from src.schemas.blueprint import SceneBlueprint

    for ex in sorted(glob.glob("examples/*/*")):
        bp = SceneBlueprint.model_validate_json(Path(f"{ex}/shots/scene_blueprint_final.json").read_text())
        lib = AssetLibrary.model_validate_json(Path(glob.glob(f"{ex}/assets/*.json")[0]).read_text())
        batch = _quiet(ProductionOperatorAgent().build_from_blueprint, bp, lib)
        for s in bp.shots:
            d = s.narrative_layer.dialogue
            if d.has_dialogue and d.text:
                carrying = [j for j in batch.jobs if j.shot_id == s.shot_id and j.dialogue == d.text]
                assert len(carrying) == 1, f"{ex}:{s.shot_id} lost its line"


# ── fakes for the execution loop ───────────────────────────────────────────

_CLIP_CACHE = {}


def _tiny_mp4(dest: Path) -> None:
    """A real 1-second test clip (ffmpeg testsrc), so stitching is exercised for real."""
    import shutil
    import subprocess
    import tempfile

    if "clip" not in _CLIP_CACHE:
        src = Path(tempfile.mkdtemp()) / "tiny.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=160x90:r=8:d=1",
                        "-pix_fmt", "yuv420p", str(src)], check=True)
        _CLIP_CACHE["clip"] = src
    shutil.copy(_CLIP_CACHE["clip"], dest)


class FakeVideo:
    def __init__(self, statuses: List[str]):
        self.statuses = list(statuses)
        self.prompts = []

    def generate(self, job, out_dir):
        status = self.statuses.pop(0) if self.statuses else "completed"
        self.prompts.append(job.prompt)
        p = Path(out_dir) / f"{job.job_id}_{len(self.prompts)}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        if status == "completed":
            _tiny_mp4(p)
            return VideoResult(job_id=job.job_id, shot_id=job.shot_id, segment_id=job.segment_id,
                               video_id=f"v{len(self.prompts)}", status="completed", video_path=str(p))
        return VideoResult(job_id=job.job_id, shot_id=job.shot_id, segment_id=job.segment_id, status=status, error="boom")

    def sample_frames(self, video_id, video_path, out_dir, n=4):
        f = Path(out_dir) / f"{Path(video_path).stem}.jpg"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"jpg")
        return [f]


class FakeT2I:
    def __init__(self):
        self.prompts = []

    def render_to_file(self, path, **kw):
        self.prompts.append(kw["prompt"])
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"png")
        return Path(path)


class FakeJudge:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)  # True / False / "error"
        self.calls = []

    def _next(self, prompt):
        v = self.verdicts.pop(0) if self.verdicts else True
        if v == "error":
            raise RuntimeError("judge unavailable")
        return VisualReview(accepted=v, score=0.9 if v else 0.3, issues=[] if v else ["wrong"],
                            revised_prompt=None if v else f"REVISED({prompt})")

    def review_keyframe(self, image_path, shot, prompt, global_style="", used_references=False, reference_images=None):
        self.calls.append(("kf", image_path, reference_images))
        return self._next(prompt)

    def review_clip(self, frame_paths, shot, prompt, global_style="", reference_images=None):
        self.calls.append(("clip", frame_paths, reference_images))
        return self._next(prompt)


def _operator(video, t2i, judge):
    op = ProductionOperatorAgent(fps=16)
    op._video, op._t2i, op._judge = video, t2i, judge
    return op


# ── F4/F5: rejected, unreviewed or missing media must not become a final film ──

def test_rejected_clip_is_not_assembled_as_final(tmp_path, library, blueprint_factory):
    bp = blueprint_factory([make_shot("s1", 3.0), make_shot("s2", 3.0)])
    batch = _quiet(ProductionOperatorAgent(fps=16).build_from_blueprint, bp, library)
    judge = FakeJudge([True, True,          # keyframes
                       False, False,         # s1 clip rejected twice (max_retries=1)
                       True])                # s2 clip accepted
    op = _operator(FakeVideo([]), FakeT2I(), judge)
    _quiet(op.execute_jobs, batch, tmp_path, bp, judge=True, max_retries=1)
    assert bp.final_film_path is None, "a rejected clip was stitched into the final film"
    status = bp.shots[0].render_layer.visual_review["clips"][0]
    assert status["verdict"] == "rejected"


def test_judge_error_is_not_acceptance(tmp_path, library, blueprint_factory):
    bp = blueprint_factory([make_shot("s1", 3.0)])
    batch = _quiet(ProductionOperatorAgent(fps=16).build_from_blueprint, bp, library)
    op = _operator(FakeVideo([]), FakeT2I(), FakeJudge(["error", "error"]))
    _quiet(op.execute_jobs, batch, tmp_path, bp, judge=True, max_retries=0)
    vr = bp.shots[0].render_layer.visual_review
    assert vr["keyframe_verdict"] == "review_error"
    assert vr["clips"][0]["verdict"] == "review_error"
    assert bp.final_film_path is None


def test_failed_shot_blocks_final_assembly(tmp_path, library, blueprint_factory):
    bp = blueprint_factory([make_shot("s1", 3.0), make_shot("s2", 3.0)])
    batch = _quiet(ProductionOperatorAgent(fps=16).build_from_blueprint, bp, library)
    op = _operator(FakeVideo(["completed", "failed"]), FakeT2I(), None)
    _quiet(op.execute_jobs, batch, tmp_path, bp, judge=False, max_retries=0, keyframes=False)
    assert bp.final_film_path is None, "film assembled although shot s2 has no clip"


# ── F3: the identity judge must see the canonical reference images ─────────

def test_keyframe_judge_receives_reference_images(tmp_path, library, blueprint_factory):
    from src.schemas.blueprint import CharacterConsistencyControl, ConditioningSpec

    ref = tmp_path / "an_sheet.png"
    ref.write_bytes(b"png")
    shot = make_shot("s1", 3.0)
    shot.render_layer.image.resolved_ti2i = "Picture 1 is An. keyframe"
    shot.render_layer.image.conditioning = ConditioningSpec(
        character_consistency=[CharacterConsistencyControl(asset_id="char_an", reference_image_paths=[str(ref)])]
    )
    bp = blueprint_factory([shot])
    batch = _quiet(ProductionOperatorAgent(fps=16).build_from_blueprint, bp, library)
    judge = FakeJudge([True, True])
    _quiet(_operator(FakeVideo([]), FakeT2I(), judge).execute_jobs, batch, tmp_path, bp, judge=True)
    kind, _, refs = judge.calls[0]
    assert kind == "kf" and refs and str(ref) in refs["char_an"]


def test_visual_judge_attaches_reference_images_to_request(tmp_path, monkeypatch):
    from src.agents.dailies_reviewer import visual_judge as vj

    sent = {}

    class _Agent:
        def __init__(self, config_path):
            pass

        def run(self, *, images=None, **kw):
            sent["images"], sent["kw"] = images, kw
            return VisualReview(accepted=True, score=1.0)

    monkeypatch.setattr(vj, "ConfigurableAgent", _Agent)
    shot = make_shot("s1", 3.0)
    vj.VisualJudgeAgent().review_keyframe("gen.png", shot, "p", reference_images={"char_an": ["an.png"]})
    assert sent["images"][0] == "an.png" and sent["images"][-1] == "gen.png"
    assert "char_an" in sent["kw"]["reference_note"]


# ── F6: stored prompt must describe the media that was kept ────────────────

def test_kept_keyframe_prompt_is_the_prompt_that_rendered_it(tmp_path, library, blueprint_factory):
    bp = blueprint_factory([make_shot("s1", 3.0)])
    batch = _quiet(ProductionOperatorAgent(fps=16).build_from_blueprint, bp, library)
    t2i = FakeT2I()
    judge = FakeJudge([False, False, True])  # both keyframe attempts rejected; clip accepted
    _quiet(_operator(FakeVideo([]), t2i, judge).execute_jobs, batch, tmp_path, bp, judge=True, max_retries=1)
    img = bp.shots[0].render_layer.image
    kept = Path(img.keyframe_image_path).name
    idx = int(kept.split("_kf")[1].split(".")[0])
    assert img.resolved_t2i == t2i.prompts[idx], "stored prompt does not match the kept keyframe"
    attempts = bp.shots[0].render_layer.visual_review["keyframe_attempts"]
    assert all("prompt" in a for a in attempts)


def test_kept_clip_prompt_is_the_prompt_that_rendered_it(tmp_path, library, blueprint_factory):
    bp = blueprint_factory([make_shot("s1", 3.0)])
    batch = _quiet(ProductionOperatorAgent(fps=16).build_from_blueprint, bp, library)
    video = FakeVideo([])
    _quiet(_operator(video, FakeT2I(), FakeJudge([True, False, False])).execute_jobs,
           batch, tmp_path, bp, judge=True, max_retries=1)
    # The last attempt was rejected: the job's prompt must still be what produced the kept clip.
    assert batch.jobs[0].prompt == video.prompts[-1]


# ── F8: long scripts must not be silently truncated ────────────────────────

def test_script_truncation_is_reported(monkeypatch):
    from src import pipeline

    script = "\n\n".join(f"SCENE {i}. " + "x" * 300 for i in range(60))
    ctx = pipeline.build_script_context(script, max_len=2000)
    rep = ctx["coverage_report"]
    assert rep["covered_chars"] <= 2000
    assert rep["omitted_chars"] == len(script) - rep["covered_chars"]
    assert rep["omitted_segments"], "omitted material is not listed"
    assert rep["omitted_segments"][0]["start"] >= rep["covered_chars"] - 1
