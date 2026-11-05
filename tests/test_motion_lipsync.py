# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""
Moving, lip-synced shots without a closed video API: VieNeu-TTS voices, the shot router, ComfyUI workflows
(talking / action) and the dialogue track the engine hands to lip-sync models. Offline: ComfyUI is an
httpx.MockTransport speaking its documented routes, VieNeu is a stub engine, ffmpeg is real.
"""

import json
import math
import struct
import threading
import wave
from pathlib import Path

import httpx
import pytest

from src.studio import Studio, avtools
from src.studio.providers import NotConfigured, ProviderError, Registry, UnsupportedParameter, load_provider_config
from src.studio.providers.comfyui import ComfyUIVideoProvider, fill, frames_for
from src.studio.providers.motion import MotionStillVideoProvider
from src.studio.providers.router import ShotRouterVideoProvider
from src.studio.providers.vieneu import VieNeuTTSProvider
from tests.studio_helpers import fixture_config, render_all, small_project
from tests.test_upstream_regressions import _tiny_mp4

ROOT = Path(__file__).resolve().parents[1]
GPU_YAML = ROOT / "configs" / "studio" / "providers.openai_gpu.yaml"

TALKING = {"1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
           "2": {"class_type": "LoadAudio", "inputs": {"audio": "{{audio}}"}},
           "3": {"class_type": "TalkSampler", "inputs": {"seed": "{{seed}}", "frames": "{{frames}}", "width": "{{width}}",
                                                         "height": "{{height}}", "text": "{{prompt}}, {{fps}} fps"}},
           "9": {"class_type": "SaveVideo", "inputs": {"video": ["3", 0]}}}
ACTION = {"1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
          "3": {"class_type": "I2VSampler", "inputs": {"seed": "{{seed}}", "frames": "{{frames}}", "text": "{{prompt}}",
                                                       "negative": "{{negative_prompt}}"}},
          "9": {"class_type": "SaveVideo", "inputs": {"video": ["3", 0]}}}


def _wav(path: Path, seconds: float, rate: int = 24000, silent: bool = False) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", 0 if silent else int(9000 * math.sin(2 * math.pi * 220 * i / rate)))
                               for i in range(n)))
    return path


class FakeComfy:
    """ComfyUI server: /upload/image, /prompt, /history/{id} (empty until done), /view, /queue."""

    def __init__(self, tmp_path, polls_needed=1, fail=False, node_errors=False):
        self.mp4 = tmp_path / "comfy_out.mp4"
        _tiny_mp4(self.mp4)
        self.uploads, self.prompts, self.polls = {}, [], {}
        self.polls_needed, self.fail, self.node_errors = polls_needed, fail, node_errors
        self.lock = threading.Lock()

    def handler(self, r: httpx.Request):
        with self.lock:
            path = r.url.path
            if path == "/upload/image":
                body = r.content
                name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                self.uploads[name] = body
                return httpx.Response(200, json={"name": name, "subfolder": "", "type": "input"})
            if path == "/prompt":
                d = json.loads(r.content)
                if self.node_errors:
                    return httpx.Response(200, json={"prompt_id": "x", "node_errors": {"3": {"errors": ["bad"]}}})
                self.prompts.append(d)
                return httpx.Response(200, json={"prompt_id": f"p{len(self.prompts)}", "number": len(self.prompts),
                                                 "node_errors": {}})
            if path.startswith("/history/"):
                pid = path.rsplit("/", 1)[-1]
                self.polls[pid] = self.polls.get(pid, 0) + 1
                if self.polls[pid] <= self.polls_needed:
                    return httpx.Response(200, json={})
                if self.fail:
                    return httpx.Response(200, json={pid: {"status": {"status_str": "error", "completed": False,
                                                                      "messages": [["execution_error", {
                                                                          "exception_message": "CUDA out of memory"}]]},
                                                           "outputs": {}}})
                return httpx.Response(200, json={pid: {"status": {"status_str": "success", "completed": True},
                                                       "outputs": {"9": {"gifs": [{"filename": f"{pid}.mp4",
                                                                                   "subfolder": "", "type": "output"}]}}}})
            if path == "/view":
                return httpx.Response(200, content=self.mp4.read_bytes())
            if path == "/queue":
                return httpx.Response(200, json={})
            return httpx.Response(404)


def _comfy(tmp_path, monkeypatch, wf, remote, **cfg):
    monkeypatch.setenv("COMFYUI_BASE_URL", "http://gpu.test:8188")
    p = tmp_path / f"wf_{len(list(tmp_path.glob('wf_*')))}.json"
    p.write_text(json.dumps(wf))
    return ComfyUIVideoProvider("", {"workflow": str(p), "fps": 25, "frames_rule": "4n+1", "max_duration": 60, **cfg},
                                {"unit": "second", "usd": 0.0}, transport=httpx.MockTransport(remote.handler))


# ── ComfyUI ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dur,fps,rule,n", [(5, 16, "4n+1", 81), (3.2, 25, "4n+1", 81), (1, 24, "8n+1", 25),
                                            (2, 24, None, 48), (0.01, 25, "4n+1", 1)])
def test_frames_rule(dur, fps, rule, n):
    assert frames_for(dur, fps, rule) == n


def test_fill_types_and_unknown_placeholder():
    out = fill({"a": "{{seed}}", "b": ["{{prompt}} at {{fps}} fps"], "c": 3}, {"seed": 7, "prompt": "hi", "fps": 25})
    assert out == {"a": 7, "b": ["hi at 25 fps"], "c": 3}
    with pytest.raises(NotConfigured):
        fill({"a": "{{nope}}"}, {})


def test_comfy_talking_round_trip(tmp_path, monkeypatch):
    remote = FakeComfy(tmp_path)
    prov = _comfy(tmp_path, monkeypatch, TALKING, remote)
    assert prov.caps.lip_sync and "audio" in prov.caps.accepted_fields
    img = tmp_path / "kf.png"
    avtools.run(["-f", "lavfi", "-i", "color=c=blue:s=64x64", "-frames:v", "1", str(img)])
    aud = _wav(tmp_path / "line.wav", 1.0)
    req, notes = prov.adapt({"prompt": "Minh asks", "duration": 3.2, "size": "720x1280", "image": str(img),
                             "audio": str(aud), "seed": 5, "camera_movement": "static"})
    assert any("camera_movement" in n for n in notes)            # not a ComfyUI field: dropped, recorded
    sr = prov.submit(req, "k" * 16, tmp_path / "w")
    inputs = remote.prompts[0]["prompt"]
    assert inputs["1"]["inputs"]["image"] == "kf.png" and inputs["2"]["inputs"]["audio"] == "line.wav"
    assert inputs["3"]["inputs"] == {"seed": 5, "frames": 81, "width": 720, "height": 1280, "text": "Minh asks, 25 fps"}
    assert prov.poll(sr.remote_id).state == "running"
    assert prov.poll(sr.remote_id).state == "completed"
    out = prov.fetch(sr.remote_id, tmp_path / "w")
    assert out.path.exists() and out.meta["duration"] > 0


def test_comfy_refusals(tmp_path, monkeypatch):
    remote = FakeComfy(tmp_path, polls_needed=0, fail=True)
    prov = _comfy(tmp_path, monkeypatch, TALKING, remote)
    img = tmp_path / "kf.png"
    avtools.run(["-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(img)])
    with pytest.raises(ProviderError, match="dialogue track"):            # lip-sync workflow without a line
        prov.submit({"prompt": "x", "duration": 2, "size": "720x1280", "image": str(img)}, "a" * 16, tmp_path)
    sr = prov.submit({"prompt": "x", "duration": 2, "size": "720x1280", "image": str(img),
                      "audio": str(_wav(tmp_path / "a.wav", 0.5))}, "b" * 16, tmp_path)
    pr = prov.poll(sr.remote_id)
    assert pr.state == "failed" and "out of memory" in pr.error
    bad = _comfy(tmp_path, monkeypatch, ACTION, FakeComfy(tmp_path, node_errors=True))
    with pytest.raises(ProviderError, match="node errors"):
        bad.submit({"prompt": "x", "duration": 2, "size": "720x1280", "image": str(img)}, "c" * 16, tmp_path)
    with pytest.raises(NotConfigured, match="Save \\(API\\)"):
        ComfyUIVideoProvider("", {"workflow": "configs/studio/comfyui/does_not_exist.json"}).submit(
            {"prompt": "x", "duration": 2, "size": "720x1280", "image": str(img)}, "d" * 16, tmp_path)
    monkeypatch.delenv("COMFYUI_BASE_URL")
    p = tmp_path / "wf.json"
    p.write_text(json.dumps(ACTION))
    with pytest.raises(NotConfigured, match="COMFYUI_BASE_URL"):
        ComfyUIVideoProvider("", {"workflow": str(p)}).submit(
            {"prompt": "x", "duration": 2, "size": "720x1280", "image": str(img)}, "e" * 16, tmp_path)


# ── VieNeu ─────────────────────────────────────────────────────────────────

class FakeVieNeu:
    def __init__(self):
        self.calls = []

    def infer(self, text, **kw):
        self.calls.append((text, kw))
        return len(text)

    def save(self, audio, path):
        _wav(Path(path), 0.05 * audio, rate=48000)


def test_vieneu_sdk_presets_clones_and_refusals(tmp_path):
    clip = _wav(tmp_path / "ba_tu.wav", 4.0)
    prov = VieNeuTTSProvider("v3turbo", {"voice_map": {"verse": "Quốc Tuấn", "sage": f"clone:{clip}", "alloy": None}})
    prov._sdk = FakeVieNeu()
    req, notes = prov.adapt({"text": "Bà ơi, cho cháu hỏi nhà số 47 ở đâu ạ?", "voice": "verse",
                             "instructions": "Giọng Hà Nội", "format": "wav", "language": "vi"})
    assert "instructions" not in req and any("instructions" in n for n in notes)
    assert any("Quốc Tuấn" in n for n in notes)
    out = prov.submit(req, "a" * 16, tmp_path).output
    assert avtools.probe(out.path)["duration"] > 1 and prov._sdk.calls[-1][1] == {"voice": "Quốc Tuấn"}
    assert prov.actual_cost(req, {}) == (0.0, "local VieNeu-TTS (no API call)")
    prov.submit({"text": "Số 47 hả cháu?", "voice": "sage"}, "b" * 16, tmp_path)
    assert prov._sdk.calls[-1][1] == {"ref_audio": str(clip)}
    for bad in ({"text": "x", "voice": "alloy"}, {"text": "x", "voice": "nova"},
                {"text": "x", "voice": "verse", "language": "ja"}):
        with pytest.raises(UnsupportedParameter):
            prov.validate(bad)


def test_vieneu_server_route_fixes_streamed_wav_header(tmp_path, monkeypatch):
    monkeypatch.setenv("VIENEU_BASE_URL", "http://tts.test/v1")
    good = _wav(tmp_path / "g.wav", 1.5, rate=48000).read_bytes()
    streamed = good[:4] + struct.pack("<I", 0xFFFFFFFF) + good[8:40] + struct.pack("<I", 0xFFFFFFFF) + good[44:]
    seen = {}

    def handler(r):
        seen.update(json.loads(r.content))
        return httpx.Response(200, content=streamed)
    prov = VieNeuTTSProvider("v3turbo", {"route": "server", "voice_map": {"alloy": "Hải Đăng"}})
    prov._transport = httpx.MockTransport(handler)
    out = prov.submit({"text": "Xin chào", "voice": "alloy"}, "c" * 16, tmp_path).output
    assert seen == {"model": "vieneu-v3-turbo", "input": "Xin chào", "voice": "Hải Đăng", "response_format": "wav"}
    assert abs(avtools.probe(out.path)["duration"] - 1.5) < 0.05


# ── router + engine ────────────────────────────────────────────────────────

def test_router_dispatch_and_review_policy(tmp_path, monkeypatch):
    remote = FakeComfy(tmp_path, polls_needed=0)
    r = ShotRouterVideoProvider({"talking": _comfy(tmp_path, monkeypatch, TALKING, remote),
                                 "action": _comfy(tmp_path, monkeypatch, ACTION, remote, max_duration=5),
                                 "still": MotionStillVideoProvider()},
                                {"dialogue": "talking", "action": "action", "establishing": "still"}, "still")
    assert r.caps.lip_sync and {"audio", "camera_movement", "shot_kind"} <= set(r.caps.accepted_fields)
    req, notes = r.adapt({"prompt": "p", "duration": 8, "size": "720x1280", "shot_kind": "action", "audio": "x.wav",
                          "camera_movement": "pan_left"})
    assert req["route"] == "action" and req["duration"] == 5 and "audio" not in req
    assert notes[0].startswith("route action -> action (comfyui:")
    assert r.skips_clip_review({"route": "still"}) and not r.skips_clip_review({"route": "talking"})
    assert r.route_for({"shot_kind": "nonsense"}) == "still"


def test_gpu_profile_builds_router(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reg = Registry("live", tmp_path, load_provider_config(GPU_YAML))
    v = reg.get("video")
    assert v.name == "shot-router" and set(v.routes) == {"talking", "action", "still"}
    assert v.routes["still"].name == "motion-still" and v.routes["talking"].name == "comfyui"
    assert reg.get("tts").name == "vieneu-tts" and reg.size_for("video", "9:16") == "720x1280"


def test_engine_routes_shots_and_hands_lipsync_track_at_mix_offset(tmp_path, monkeypatch):
    """3 shots: establishing (narration only) -> still, dialogue -> talking (gets the line, placed where the export
    mixes it), action (characters, no line) -> action. Export is final; every clip says which route made it."""
    def mutate(spec):
        spec["shots"][0]["characters"] = {}
        spec["shots"][2].pop("dialogue", None)
    remote = FakeComfy(tmp_path, polls_needed=1)
    router = ShotRouterVideoProvider({"talking": _comfy(tmp_path, monkeypatch, TALKING, remote),
                                      "action": _comfy(tmp_path, monkeypatch, ACTION, remote, max_duration=5),
                                      "still": MotionStillVideoProvider(cfg={"fps": 12})},
                                     {"dialogue": "talking", "action": "action", "establishing": "still"}, "still")
    s = Studio(tmp_path / "s", provider_config=fixture_config())
    p, ep = small_project(s, n_shots=3, mode="live", budget=5.0, mutate=mutate)
    fx = Studio(tmp_path / "fx", provider_config=fixture_config())
    pf, _ = small_project(fx, n_shots=1)
    s.engine.overrides[p["id"]] = {k: fx.engine.provider_for(pf, k) for k in ("image", "tts", "judge")}
    s.engine.overrides[p["id"]]["video"] = router
    r = render_all(s, p, ep, timeout=240)
    assert r["status"] == "final", r
    clips = {a["request"]["params"]["shot_kind"]: a for a in
             s.store.q("SELECT * FROM attempts WHERE provider = 'shot-router' AND purpose = 'generate'")}
    assert {k: c["request"]["params"]["route"] for k, c in clips.items()} == \
        {"establishing": "still", "dialogue": "talking", "action": "action"}
    assert len(remote.prompts) == 2                                     # talking + action; the still is local
    # the dialogue clip got a shot-length track with the line at the export's offset
    dlg = clips["dialogue"]
    track = s.media.path(dlg["request"]["params"]["audio_sha"])
    from src.studio.export import build_timeline
    tl = build_timeline(s, ep["id"])
    shot = next(x for x in tl["shots"] if x["shot_id"] == dlg["request"]["params"]["label"].split(" · ")[1].split()[0])
    line = next(x for x in tl["lines"] if x["shot_id"] == shot["shot_id"] and x["kind"] == "dialogue")
    offset, end = line["start"] - shot["start"], line["end"] - shot["start"]
    assert abs(avtools.probe(track)["duration"] - shot["duration"]) < 0.05

    def peak(a, b):
        err = avtools.run_capture(["-i", str(track), "-af", f"atrim={a}:{b},volumedetect", "-f", "null", "-"])
        import re
        m = re.search(r"max_volume: (-?[\d.]+) dB", err)
        return float(m.group(1)) if m else -999.0
    assert peak(0, max(0.01, offset - 0.05)) < -60              # silent before the line starts
    assert peak(offset + 0.05, end - 0.05) > -30                # the line itself
    reviews = {a["job_id"] for a in s.store.q("SELECT * FROM attempts WHERE purpose = 'review'")}
    clip_jobs = {a["job_id"]: k for k, a in clips.items()}
    assert {clip_jobs[j] for j in reviews if j in clip_jobs} == {"dialogue", "action"}   # still: file check only
    ex = s.export(ep["id"])
    assert ex["status"] == "final" and ex["manifest"]["inspection"]["all_pass"], ex["manifest"]["inspection"]
