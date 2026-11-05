# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""
The one-OpenAI-key line-up (configs/studio/providers.openai.yaml): OpenAI images / voices / judge + the local
motion-still clip provider. Offline: OpenAI clients are stubbed, ffmpeg is real. No network, no spend.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.studio import Studio, avtools
from src.studio.providers import Registry, UnsupportedParameter, load_provider_config
from src.studio.providers.live import OpenAIImageProvider, OpenAITTSProvider
from src.studio.providers.motion import MotionStillVideoProvider, resolve_move
from tests.studio_helpers import fixture_config, render_all, small_project

ROOT = Path(__file__).resolve().parents[1]
OPENAI_YAML = ROOT / "configs" / "studio" / "providers.openai.yaml"


def _still(path: Path, w=1024, h=1536) -> str:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d=1",
                    "-frames:v", "1", str(path)], check=True)
    return str(path)


@pytest.mark.parametrize("movement,move", [("static", "breathe"), ("push_in", "push_in"), ("slow dolly in", "push_in"),
                                           ("Pull-Out", "pull_out"), ("pan_left", "pan_left"),
                                           ("tracking", "pan_right"), ("tilt_up", "tilt_up"),
                                           ("crane_down", "tilt_down"), ("", "breathe"), ("orbit", "breathe")])
def test_camera_vocabulary(movement, move):
    assert resolve_move(movement) == move


@pytest.mark.parametrize("movement", ["static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down"])
def test_motion_clip_has_planned_size_duration_and_moves(tmp_path, movement):
    img = _still(tmp_path / "kf.png")
    prov = MotionStillVideoProvider(cfg={"fps": 12})
    req, notes = prov.adapt({"prompt": "x", "duration": 2.5, "size": "720x1280", "image": img,
                             "camera_movement": movement, "negative_prompt": "blur"})
    assert any("negative_prompt" in n for n in notes)              # dropped explicitly, not silently
    sr = prov.submit(req, "k" * 16, tmp_path / "out")
    info = avtools.probe(sr.output.path)
    assert (info["width"], info["height"]) == (720, 1280)
    assert abs(info["duration"] - 2.5) < 0.15 and info["audio_streams"] == 0
    assert prov.actual_cost(req, sr.output.usage) == (0.0, "local ffmpeg (no API call)")
    # the camera actually moves: first and last frame differ
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    avtools.run(["-i", str(sr.output.path), "-frames:v", "1", str(a)])
    avtools.run(["-sseof", "-0.1", "-i", str(sr.output.path), "-frames:v", "1", str(b)])
    assert a.read_bytes() != b.read_bytes()


def test_motion_refuses_without_keyframe(tmp_path):
    with pytest.raises(UnsupportedParameter):
        MotionStillVideoProvider().submit({"prompt": "x", "duration": 2, "size": "720x1280"}, "k" * 16, tmp_path)


def test_openai_profile_builds_one_key_lineup(tmp_path, monkeypatch):
    for k in ("T2I_API_KEY", "TTS_API_KEY", "LLM_API_KEY", "JUDGE_MODEL", "HUOBAO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reg = Registry("live", tmp_path, load_provider_config(OPENAI_YAML))
    img, vid, tts, jdg = (reg.get(k) for k in ("image", "video", "tts", "judge"))
    assert (img.name, img.model, img.api_key, img.quality) == ("openai-image", "gpt-image-2", "sk-test", "high")
    assert (vid.name, vid.is_fixture, vid.review_clips) == ("motion-still", False, False)
    assert (tts.name, tts.model) == ("vieneu-tts", "v3turbo")          # local Vietnamese voice, $0
    assert (jdg.name, jdg.model) == ("vlm-judge", "gpt-6.1-terra")
    assert reg.size_for("video", "9:16") == "1080x1920"
    # every paid kind has a ceiling price (no null = no blocked call), video is free
    assert img.estimate_cost({}) == 0.20 and vid.estimate_cost({"duration": 8}) == 0.0
    assert tts.estimate_cost({"text": "x" * 1000}) == 0.0 and jdg.estimate_cost({}) == 0.03
    # gpt-image-2 settles from the tokens the API reports
    usd, basis = img.actual_cost({}, {"input_tokens": 2000, "output_tokens": 4000})
    assert usd == pytest.approx(2000 / 1e6 * 4 + 4000 / 1e6 * 15) and "usage" in basis


def test_openai_image_and_tts_request_shapes(tmp_path):
    import base64
    calls = []

    class Images:
        def generate(self, **kw):
            calls.append(("generate", kw))
            return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"png").decode(), revised_prompt=None)],
                                   usage=None)

        def edit(self, **kw):
            calls.append(("edit", {k: v for k, v in kw.items() if k != "image"} | {"n_images": len(kw["image"])}))
            return self.generate(**{k: v for k, v in kw.items() if k != "image"})

    img = OpenAIImageProvider("gpt-image-2", "sk", None, {"unit": "image", "usd": 0.1}, quality="medium")
    img._client = lambda: SimpleNamespace(images=Images())
    ref = tmp_path / "r.png"
    ref.write_bytes(b"x")
    img.submit({"prompt": "p", "size": "1024x1536"}, "a" * 16, tmp_path)
    img.submit({"prompt": "p", "size": "1024x1536", "reference_images": [{"path": str(ref)}]}, "b" * 16, tmp_path)
    assert calls[0] == ("generate", {"model": "gpt-image-2", "prompt": "p", "n": 1, "size": "1024x1536",
                                     "quality": "medium"})
    assert calls[1][0] == "edit" and calls[1][1]["n_images"] == 1 and calls[1][1]["model"] == "gpt-image-2"

    sent = {}

    class Speech:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(write_to_file=lambda p: Path(p).write_bytes(b"RIFF"))

    import openai
    orig = openai.OpenAI
    openai.OpenAI = lambda **kw: SimpleNamespace(audio=SimpleNamespace(speech=Speech()))
    try:
        OpenAITTSProvider("gpt-4o-mini-tts", "sk").submit(
            {"text": "Bà ơi, cho cháu hỏi", "voice": "verse", "instructions": "Giọng Hà Nội, lễ phép"}, "c" * 16, tmp_path)
    finally:
        openai.OpenAI = orig
    assert sent == {"model": "gpt-4o-mini-tts", "voice": "verse", "input": "Bà ơi, cho cháu hỏi",
                    "response_format": "wav", "instructions": "Giọng Hà Nội, lễ phép"}


def test_engine_one_key_episode_exports_final_with_free_clips(tmp_path):
    """Live-mode project: keyframes / voices / judge from the offline fixtures (stand-ins for the OpenAI calls),
    clips from the REAL motion-still provider. One clip per shot, no clip judge call, $0 for video."""
    s = Studio(tmp_path / "s", provider_config=fixture_config())
    p, ep = small_project(s, n_shots=3, mode="live", budget=5.0)
    fx = Studio(tmp_path / "fx", provider_config=fixture_config())
    pf, _ = small_project(fx, n_shots=1)
    s.engine.overrides[p["id"]] = {k: fx.engine.provider_for(pf, k) for k in ("image", "tts", "judge")}
    s.engine.overrides[p["id"]]["video"] = MotionStillVideoProvider(cfg={"fps": 12})
    r = render_all(s, p, ep, timeout=240)
    assert r["status"] == "final", r
    clips = s.store.q("SELECT * FROM attempts WHERE provider = 'motion-still' AND purpose = 'generate'")
    assert len(clips) == 3
    for a in clips:
        req = a["request"] if isinstance(a["request"], dict) else json.loads(a["request"])
        assert req["params"]["camera_movement"]                        # FilmDSL camera move reached the request
        assert (a["cost_actual_usd"] or 0) == 0
    clip_jobs = [j for j in s.store.q("SELECT * FROM jobs WHERE episode_id = ?", (ep["id"],)) if j["kind"] == "video"]
    reviews = s.store.q("SELECT * FROM attempts WHERE purpose = 'review'")
    assert not [a for a in reviews if a["job_id"] in {j["id"] for j in clip_jobs}]   # no paid judge on clips
    ex = s.export(ep["id"])
    assert ex["status"] == "final" and ex["manifest"]["inspection"]["all_pass"], ex["manifest"]["inspection"]


def test_estimate_cli_on_openai_profile(monkeypatch, capsys, tmp_path):
    from src.studio.__main__ import main
    monkeypatch.setenv("STUDIO_PROVIDERS", str(OPENAI_YAML))
    main(["--home", str(tmp_path), "estimate", "--minutes", "10", "--budget", "50"])
    out = json.loads(capsys.readouterr().out)
    assert out["unit_usd"]["video"] == 0.0 and out["low_usd"] > 0 and out["max_minutes_worst_case"] > 10


def test_subtitle_split_is_balanced_and_prefers_sentence_ends():
    from src.studio.subtitles import wrap
    cues = wrap("Máy vẫn tốt. Chỉ là lâu không ai lên dây thôi. Nhưng trong nắp có cái này.", 32, 2)
    assert [" ".join(c) for c in cues] == ["Máy vẫn tốt. Chỉ là lâu không ai lên dây thôi.", "Nhưng trong nắp có cái này."]
    cues = wrap("Ông xem giúp cháu ạ. Nó đứng ở năm giờ bốn mươi bảy, ba tháng nay rồi.", 32, 2)
    assert " ".join(cues[0]) == "Ông xem giúp cháu ạ."
    assert all(len(c) <= 2 and all(len(line) <= 32 for line in c) for c in cues)
    assert wrap("Bà ơi, cho cháu hỏi nhà số 47 ở đâu ạ?", 32, 2) == [["Bà ơi, cho cháu hỏi nhà số 47 ở", "đâu ạ?"]]
