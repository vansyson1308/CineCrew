# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""
Contract tests for the huobao-drama backend line-up (src/studio/providers/multiref.py): Seedance 2.0,
MiniMax H3, Wan 3.0, Seedream, Gemini image, MiniMax TTS. Every HTTP call goes to an httpx.MockTransport;
the request shapes asserted here are the ones the providers document. No network, no spend.
"""

import base64
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from src.studio.hashing import hash_file
from src.studio.providers import Registry, load_provider_config
from src.studio.providers.base import AmbiguousSubmission, NotConfigured, ProviderError, RateLimited, UnsupportedParameter
from src.studio.providers.multiref import (
    GeminiImageProvider, MiniMaxTTSProvider, MiniMaxVideoProvider, SeedanceVideoProvider, SeedreamImageProvider,
    WanVideoProvider, join_url, resolve_endpoint,
)
from tests.test_upstream_regressions import _tiny_mp4

KEY_ENVS = ("ARK_API_KEY", "MINIMAX_API_KEY", "DASHSCOPE_API_KEY", "GEMINI_API_KEY", "HUOBAO_API_KEY",
            "HUOBAO_BASE_URL", "ARK_BASE_URL", "STUDIO_PROVIDERS")


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for k in KEY_ENVS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def png(tmp_path) -> Path:
    p = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1",
                    str(p)], check=True)
    return p


@pytest.fixture
def mp4(tmp_path) -> Path:
    p = tmp_path / "remote.mp4"
    _tiny_mp4(p)
    return p


def _mock(handler):
    return httpx.MockTransport(handler)


# ── endpoint resolution (direct key vs Huobao gateway) ──────────────────────

def test_direct_key_wins_over_gateway(monkeypatch):
    monkeypatch.setenv("HUOBAO_API_KEY", "hb")
    assert resolve_endpoint({}, "https://ark.example/api/v3", ("ARK_API_KEY",), "/volcengine") == \
        ("https://api.firemux.com/volcengine", "hb", "huobao-gateway")
    monkeypatch.setenv("ARK_API_KEY", "ark")
    assert resolve_endpoint({}, "https://ark.example/api/v3", ("ARK_API_KEY",), "/volcengine") == \
        ("https://ark.example/api/v3", "ark", "direct")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    assert resolve_endpoint({"base_url_env": "ARK_BASE_URL"}, "x", ("ARK_API_KEY",), "")[0] == \
        "https://ark.cn-beijing.volces.com/api/v3"


def test_gateway_can_be_disabled_and_mainland_node(monkeypatch):
    monkeypatch.setenv("HUOBAO_API_KEY", "hb")
    assert resolve_endpoint({"gateway": False}, "https://x", ("ARK_API_KEY",), "/volcengine")[2] == "unconfigured"
    monkeypatch.setenv("HUOBAO_BASE_URL", "https://api.firemux.cn/")
    assert resolve_endpoint({}, "https://x", ("ARK_API_KEY",), "/minimax")[0] == "https://api.firemux.cn/minimax"


def test_join_url_never_doubles_prefix():
    assert join_url("https://a/api/v3", "/api/v3", "/t") == "https://a/api/v3/t"
    assert join_url("https://gw/volcengine", "/api/v3", "/t") == "https://gw/volcengine/api/v3/t"


# ── Seedance 2.0 ────────────────────────────────────────────────────────────

def _seedance(handler, cfg=None, price=None):
    return SeedanceVideoProvider("seedance-2-0-fast-260128", cfg or {}, price or {"unit": "second", "usd": 0.1},
                                 transport=_mock(handler), api_key="ark-key", base_url="https://ark.test/api/v3")


def test_seedance_submit_poll_fetch(tmp_path, png, mp4):
    seen = {"polls": 0}

    def handler(r: httpx.Request):
        if r.method == "POST" and r.url.path == "/api/v3/contents/generations/tasks":
            seen["body"], seen["auth"] = json.loads(r.content), r.headers.get("authorization")
            return httpx.Response(200, json={"id": "cgt-1"})
        if r.url.path == "/api/v3/contents/generations/tasks/cgt-1":
            seen["polls"] += 1
            st = ["queued", "running", "succeeded"][min(seen["polls"] - 1, 2)]
            body = {"id": "cgt-1", "status": st}
            if st == "succeeded":
                body.update(content={"video_url": "https://cdn.test/v.mp4"},
                            usage={"completion_tokens": 108900, "total_tokens": 108900})
            return httpx.Response(200, json=body)
        if r.url.host == "cdn.test":
            seen["cdn_auth"] = r.headers.get("authorization")
            return httpx.Response(200, content=mp4.read_bytes())
        return httpx.Response(404)

    v = _seedance(handler, price={"unit": "second", "usd": 0.1, "usd_per_1m_tokens": 2.0})
    req, notes = v.adapt({"prompt": "p", "duration": 5.3, "size": "576x1024", "seed": 7, "negative_prompt": "x",
                          "image": str(png)})
    assert req["duration"] == 6.0 and req["size"] == "720x1280" and "negative_prompt" not in req
    assert any("negative_prompt" in n for n in notes) and any("duration" in n for n in notes)
    rid = v.submit(req, "k", tmp_path).remote_id
    b = seen["body"]
    assert seen["auth"] == "Bearer ark-key" and rid == "cgt-1"
    assert b["model"] == "seedance-2-0-fast-260128" and b["duration"] == 6 and b["resolution"] == "720p"
    assert b["ratio"] == "adaptive" and b["generate_audio"] is False and b["watermark"] is False and b["seed"] == 7
    assert [c.get("role") for c in b["content"]] == [None, "first_frame"]
    assert b["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(b["content"][1]["image_url"]["url"].split(",", 1)[1]) == png.read_bytes()
    assert v.poll(rid).state == "queued"
    assert v.poll(rid).state == "running"
    assert v.poll(rid).state == "completed"
    out = v.fetch(rid, tmp_path / "dl")
    assert out.path.exists() and out.usage["total_tokens"] == 108900 and out.usage["seconds"] > 0
    assert seen["cdn_auth"] is None                       # the key never goes to the result CDN
    assert v.actual_cost(req, out.usage) == (0.2178, "provider usage tokens x configured token price")


def test_seedance_text_only_sets_ratio_and_480p(tmp_path):
    seen = {}

    def handler(r):
        seen["body"] = json.loads(r.content)
        return httpx.Response(200, json={"id": "t"})
    v = _seedance(handler)
    req, _ = v.adapt({"prompt": "p", "duration": 3, "size": "864x480"})
    v.submit(req, "k", tmp_path)
    assert seen["body"]["ratio"] == "16:9" and seen["body"]["resolution"] == "480p" and seen["body"]["duration"] == 4


def test_seedance_cannot_mix_first_frame_and_references(tmp_path, png):
    refs = [{"label": "Minh <char_minh>", "sha": "a", "path": str(png)}]
    v = _seedance(lambda r: httpx.Response(500))
    assert v.video_asset_references is False             # default first_frame mode: the engine sends no sheets
    req, notes = v.adapt({"prompt": "p", "duration": 5, "size": "720x1280", "image": str(png), "reference_images": refs})
    assert "reference_images" not in req and req["image"] == str(png) and any("cannot mix" in n for n in notes)

    seen = {}

    def handler(r):
        seen["body"] = json.loads(r.content)
        return httpx.Response(200, json={"id": "t"})
    v2 = _seedance(handler, cfg={"video_mode": "references"})
    assert v2.video_asset_references is True
    req, notes = v2.adapt({"prompt": "walk", "duration": 5, "size": "720x1280", "image": str(png),
                           "reference_images": refs})
    assert "image" not in req and req["reference_images"][-1]["sha"] == hash_file(png)
    v2.submit(req, "k", tmp_path)
    roles = [c.get("role") for c in seen["body"]["content"]]
    assert roles == [None, "reference_image", "reference_image"] and seen["body"]["ratio"] == "9:16"
    text = seen["body"]["content"][0]["text"]
    assert text.startswith("Reference images: [Image 1] Minh <char_minh>; [Image 2] approved keyframe") and \
        text.endswith("\nwalk")


@pytest.mark.parametrize("status,exc,retryable", [(429, RateLimited, True), (503, ProviderError, True),
                                                  (400, ProviderError, False)])
def test_seedance_error_classes(tmp_path, status, exc, retryable):
    v = _seedance(lambda r: httpx.Response(status, headers={"retry-after": "3"},
                                           json={"error": {"code": "InvalidParameter", "message": "bad"}}))
    with pytest.raises(exc) as e:
        v.submit({"prompt": "p", "duration": 5.0, "size": "720x1280"}, "k", tmp_path)
    assert e.value.retryable is retryable
    if status == 400:
        assert "InvalidParameter" in str(e.value)
    if status == 429:
        assert e.value.retry_after == 3.0


def test_seedance_network_drop_is_ambiguous_and_missing_key(tmp_path):
    def handler(r):
        raise httpx.ConnectError("reset", request=r)
    with pytest.raises(AmbiguousSubmission):
        _seedance(handler).submit({"prompt": "p", "duration": 5.0, "size": "720x1280"}, "k", tmp_path)
    with pytest.raises(NotConfigured):
        SeedanceVideoProvider("m", {}, None).submit({"prompt": "p", "duration": 5.0, "size": "720x1280"}, "k", tmp_path)


def test_seedance_failed_and_urlless_tasks(tmp_path):
    def handler(r):
        if r.url.path.endswith("/bad"):
            return httpx.Response(200, json={"status": "failed", "error": {"code": "OutputVideoSensitiveContentDetected",
                                                                           "message": "filtered"}})
        return httpx.Response(200, json={"status": "succeeded", "content": {}})
    v = _seedance(handler)
    bad = v.poll("bad")
    assert bad.state == "failed" and "OutputVideoSensitiveContentDetected" in bad.error
    assert v.poll("nourl").state == "failed"
    with pytest.raises(ProviderError):
        v.fetch("nourl", tmp_path)


def test_seedance_1080p_only_on_standard_model_and_cancel(tmp_path):
    seen = {}

    def handler(r):
        seen[r.method] = (str(r.url), json.loads(r.content) if r.content else None)
        return httpx.Response(200, json={"id": "t"})
    fast = _seedance(handler)
    assert fast.adapt({"prompt": "p", "duration": 5, "size": "1080x1920"})[0]["size"] == "720x1280"
    std = SeedanceVideoProvider("dreamina-seedance-2-0-260128", {}, {"unit": "second", "usd": 0.2},
                                transport=_mock(handler), api_key="k", base_url="https://ark.test/api/v3")
    req, _ = std.adapt({"prompt": "p", "duration": 5, "size": "1080x1920"})
    std.submit(req, "k", tmp_path)
    assert seen["POST"][1]["resolution"] == "1080p"
    assert std.cancel("t") is True and seen["DELETE"][0].endswith("/api/v3/contents/generations/tasks/t")


def test_seedance_unsupported_duration_is_refused_not_forwarded(tmp_path):
    v = _seedance(lambda r: httpx.Response(200, json={"id": "x"}))
    with pytest.raises(UnsupportedParameter):
        v.submit({"prompt": "p", "duration": 20.0, "size": "720x1280"}, "k", tmp_path)
    req, notes = v.adapt({"prompt": "p", "duration": 20.0, "size": "720x1280"})
    assert req["duration"] == 15.0 and any("15" in n for n in notes)


def test_seedance_through_huobao_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("HUOBAO_API_KEY", "hb-key")
    seen = {}

    def handler(r):
        seen["url"], seen["auth"] = str(r.url), r.headers["authorization"]
        return httpx.Response(200, json={"id": "t"})
    v = SeedanceVideoProvider("m", {}, {"unit": "second", "usd": 0.1}, transport=_mock(handler))
    v.submit({"prompt": "p", "duration": 5.0, "size": "720x1280"}, "k", tmp_path)
    assert seen["url"] == "https://api.firemux.com/volcengine/api/v3/contents/generations/tasks"
    assert seen["auth"] == "Bearer hb-key" and v.describe()["route"] == "huobao-gateway"


# ── MiniMax H3 ──────────────────────────────────────────────────────────────

def _minimax(handler):
    return MiniMaxVideoProvider("MiniMax-H3", {}, {"unit": "second", "usd": 0.1}, transport=_mock(handler),
                                api_key="mm", base_url="https://mm.test")


def test_minimax_reference_mode_and_cancel(tmp_path, png, mp4):
    seen = {}

    def handler(r):
        if r.method == "DELETE":
            seen["deleted"] = r.url.path
            return httpx.Response(200, json={"base_resp": {"status_code": 0}})
        if r.method == "POST":
            seen["path"], seen["body"] = r.url.path, json.loads(r.content)
            return httpx.Response(200, json={"task_id": "176", "base_resp": {"status_code": 0}})
        if r.url.path == "/v2/query/video_generation/176":
            return httpx.Response(200, json={"task": {"status": "succeeded", "content": {"url": "https://cdn.test/m.mp4"}}})
        return httpx.Response(200, content=mp4.read_bytes())

    v = MiniMaxVideoProvider("MiniMax-H3", {"video_mode": "references"}, {"unit": "second", "usd": 0.08},
                             transport=_mock(handler), api_key="mm", base_url="https://mm.test")
    assert v.video_asset_references is True
    refs = [{"label": "Minh <char_minh>", "sha": "a", "path": str(png)},
            {"label": "Phố cổ <loc_pho_co>", "sha": "b", "path": str(png)}]
    req, notes = v.adapt({"prompt": "walk", "duration": 7.2, "size": "720x1280", "image": str(png),
                          "reference_images": refs, "seed": 3})
    assert req["duration"] == 8.0 and req["size"] == "768x1366" and "seed" not in req and "image" not in req
    rid = v.submit(req, "k", tmp_path).remote_id
    b = seen["body"]
    assert seen["path"] == "/v2/video_generation" and rid == "176"
    assert [c.get("role") for c in b["content"]] == [None, "reference_image", "reference_image", "reference_image"]
    assert b["ratio"] == "9:16" and b["resolution"] == "768P" and b["duration"] == 8
    assert b["content"][0]["text"].startswith("Reference images: [Image 1] Minh <char_minh>; [Image 2] Phố cổ")
    assert v.poll(rid).state == "completed"
    assert v.fetch(rid, tmp_path / "dl").path.exists()
    assert v.cancel("176") is True and seen["deleted"] == "/v2/video_generation/176"


def test_minimax_default_mode_is_first_frame_only(tmp_path, png):
    v = MiniMaxVideoProvider("MiniMax-H3", {}, None, api_key="mm", base_url="https://mm.test")
    assert v.video_asset_references is False
    req, notes = v.adapt({"prompt": "p", "duration": 6, "size": "720x1280", "image": str(png),
                          "reference_images": [{"label": "x", "sha": "a", "path": str(png)}]})
    assert "reference_images" not in req and req["image"]


def test_minimax_prompt_limit_and_failure(tmp_path):
    v = _minimax(lambda r: httpx.Response(200, json={"task": {"status": "failed",
                                                              "error": {"code": 1026, "message": "sensitive"}}}))
    with pytest.raises(UnsupportedParameter):
        v.validate({"prompt": "x" * 7001, "duration": 6.0, "size": "768x1366"})
    p = v.poll("9")
    assert p.state == "failed" and "1026" in p.error


def test_minimax_http200_refusals(tmp_path):
    def handler(r):
        code = 1002 if b"limit" in r.content else 1008
        return httpx.Response(200, json={"task_id": "", "base_resp": {"status_code": code, "status_msg": "no"}})
    v = _minimax(handler)
    with pytest.raises(RateLimited):
        v.submit({"prompt": "limit", "duration": 6.0, "size": "768x1366"}, "k", tmp_path)
    with pytest.raises(ProviderError, match="1008") as e:
        v.submit({"prompt": "p", "duration": 6.0, "size": "768x1366"}, "k", tmp_path)
    assert e.value.retryable is False


def test_minimax_text_to_video_sends_ratio(tmp_path):
    seen = {}

    def handler(r):
        seen["body"] = json.loads(r.content)
        return httpx.Response(200, json={"task_id": "1"})
    v = _minimax(handler)
    req, _ = v.adapt({"prompt": "p", "duration": 6, "size": "1280x720"})
    v.submit(req, "k", tmp_path)
    assert seen["body"]["ratio"] == "16:9"


# ── Wan 3.0 ─────────────────────────────────────────────────────────────────

def _wan(handler, cfg=None):
    return WanVideoProvider("wan3.0-video", cfg or {}, {"unit": "second", "usd": 0.1}, transport=_mock(handler),
                            api_key="ds", base_url="https://ds.test")


def test_wan_submit_poll_fetch(tmp_path, png, mp4):
    seen = {}

    def handler(r):
        if r.method == "POST":
            seen["url"], seen["async"], seen["body"] = r.url.path, r.headers.get("x-dashscope-async"), json.loads(r.content)
            return httpx.Response(200, json={"output": {"task_id": "w1", "task_status": "PENDING"}, "request_id": "q"})
        if r.url.path == "/api/v1/tasks/w1":
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", "video_url": "https://oss.test/w.mp4"},
                                             "usage": {"output_video_duration": 12}})
        return httpx.Response(200, content=mp4.read_bytes())

    v = _wan(handler)
    req, notes = v.adapt({"prompt": "p", "duration": 11.4, "size": "1080x1920", "seed": 5, "image": str(png)})
    assert req["duration"] == 12.0
    rid = v.submit(req, "k", tmp_path).remote_id
    b = seen["body"]
    assert seen["url"] == "/api/v1/services/aigc/video-generation/video-synthesis" and seen["async"] == "enable"
    assert b["input"]["media"][0]["type"] == "first_frame" and b["input"]["media"][0]["url"].startswith("data:image/png")
    assert b["parameters"] == {"resolution": "1080P", "ratio": "adaptive", "duration": 12, "audio": False,
                               "prompt_extend": False, "watermark": False, "seed": 5}
    assert v.poll(rid).state == "completed"
    assert v.fetch(rid, tmp_path / "dl").usage["billed_seconds"] == 12


def test_wan_states_and_errors(tmp_path):
    def handler(r):
        st = r.url.path.rsplit("/", 1)[-1]
        if st == "unknown":
            return httpx.Response(200, json={"output": {"task_status": "UNKNOWN"}})
        if st == "failed":
            return httpx.Response(200, json={"output": {"task_status": "FAILED", "code": "DataInspectionFailed",
                                                        "message": "blocked"}})
        if st == "gone":
            return httpx.Response(404, json={"code": "NotFound", "message": "no"})
        return httpx.Response(200, json={"output": {"task_status": "RUNNING"}})
    v = _wan(handler)
    assert v.poll("run").state == "running"
    assert v.poll("failed").state == "failed" and "DataInspectionFailed" in v.poll("failed").error
    assert v.poll("unknown").state == "failed"
    assert v.poll("gone").state == "unknown"            # transport trouble is never a verdict


# ── images ──────────────────────────────────────────────────────────────────

def test_seedream_references_and_b64(tmp_path, png):
    seen = {}

    def handler(r):
        seen["path"], seen["body"] = r.url.path, json.loads(r.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png.read_bytes()).decode()}],
                                         "usage": {"generated_images": 1}})
    v = SeedreamImageProvider("seedream-4-5-251128", {}, {"unit": "image", "usd": 0.04}, transport=_mock(handler),
                              api_key="ark", base_url="https://ark.test/api/v3")
    refs = [{"label": "Minh <char_minh>", "sha": "a", "path": str(png)}]
    req, _ = v.adapt({"prompt": "keyframe", "size": "1024x1536", "seed": 1, "reference_images": refs,
                      "negative_prompt": "text"})
    assert req["size"] == "1440x2560" or req["size"] == "1728x2304"
    out = v.submit(req, "k1234567890123", tmp_path).output
    b = seen["body"]
    assert seen["path"] == "/api/v3/images/generations" and b["response_format"] == "b64_json"
    assert b["image"][0].startswith("data:image/png;base64,") and b["prompt"].startswith("Reference images: [Image 1]")
    assert out.path.read_bytes() == png.read_bytes()


def test_gemini_image_inline_and_blocked(tmp_path, png):
    seen = {}

    def handler(r):
        seen["path"], seen["body"], seen["hdr"] = r.url.path, json.loads(r.content), dict(r.headers)
        if "blocked" in seen["body"]["contents"][0]["parts"][-1]["text"]:
            return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "here"}, {"inlineData": {"mimeType": "image/png",
                                              "data": base64.b64encode(png.read_bytes()).decode()}}]}}]})
    v = GeminiImageProvider("gemini-3.1-flash-image", {}, {"unit": "image", "usd": 0.04}, transport=_mock(handler),
                            api_key="g", base_url="https://g.test")
    out = v.submit({"prompt": "frame", "size": "768x1344",
                    "reference_images": [{"label": "Minh", "sha": "a", "path": str(png)}]}, "k" * 16, tmp_path).output
    assert seen["path"] == "/v1beta/models/gemini-3.1-flash-image:generateContent"
    assert seen["hdr"]["x-goog-api-key"] == "g" and "authorization" not in seen["hdr"]
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png" and parts[-1]["text"].endswith("frame")
    assert seen["body"]["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16"
    assert out.path.read_bytes() == png.read_bytes()
    with pytest.raises(ProviderError, match="SAFETY"):
        v.submit({"prompt": "blocked", "size": "768x1344"}, "k" * 16, tmp_path)


# ── MiniMax TTS ─────────────────────────────────────────────────────────────

def test_minimax_tts_hex_audio_and_language(tmp_path):
    wav = tmp_path / "s.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=0.5", str(wav)], check=True)
    seen = {}

    def handler(r):
        seen["body"] = json.loads(r.content)
        if seen["body"]["text"] == "limit":
            return httpx.Response(200, json={"base_resp": {"status_code": 1002, "status_msg": "rate limit"}})
        if seen["body"]["text"] == "bad":
            return httpx.Response(200, json={"base_resp": {"status_code": 2013, "status_msg": "invalid params"}})
        return httpx.Response(200, json={"data": {"audio": wav.read_bytes().hex(), "status": 2},
                                         "extra_info": {"usage_characters": 9}, "base_resp": {"status_code": 0}})
    t = MiniMaxTTSProvider("speech-2.8-hd", {"voice_map": {"verse": "Vietnamese_Young_Man", "sage": None},
                                             "voices": ["v"]},
                           {"unit": "1k_chars", "usd": 0.1}, transport=_mock(handler), api_key="mm",
                           base_url="https://mm.test")
    req, notes = t.adapt({"text": "Xin chào!", "voice": "verse", "instructions": "Giọng trẻ", "language": "vi",
                          "format": "wav"})
    assert req["voice"] == "Vietnamese_Young_Man" and any("instructions" in n for n in notes)
    out = t.submit(req, "k" * 16, tmp_path).output
    assert out.path.read_bytes() == wav.read_bytes() and out.usage["characters"] == 9
    assert seen["body"]["language_boost"] == "Vietnamese" and seen["body"]["voice_setting"]["voice_id"] == "Vietnamese_Young_Man"
    with pytest.raises(RateLimited):
        t.submit({"text": "limit", "voice": "v"}, "k" * 16, tmp_path)
    with pytest.raises(ProviderError, match="2013"):
        t.submit({"text": "bad", "voice": "v"}, "k" * 16, tmp_path)
    with pytest.raises(UnsupportedParameter, match="voice_map"):      # unmapped voice: refused before any call
        t.submit({"text": "x", "voice": "sage"}, "k" * 16, tmp_path)


# ── registry ────────────────────────────────────────────────────────────────

def test_huobao_profile_builds_the_line_up(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_PROVIDERS", "configs/studio/providers.huobao.yaml")
    cfg = load_provider_config()
    assert cfg["live"]["video"]["provider"] == "seedance"
    reg = Registry("live", tmp_path, cfg)
    assert isinstance(reg.get("video"), SeedanceVideoProvider)
    assert isinstance(reg.get("image"), SeedreamImageProvider)
    assert isinstance(reg.get("tts"), MiniMaxTTSProvider)
    # list prices from the profile; a voice without a MiniMax voice_id is refused before any call
    assert reg.get("video").estimate_cost({"duration": 5}) == pytest.approx(0.65)
    assert reg.get("image").estimate_cost({}) == 0.04
    with pytest.raises(UnsupportedParameter):
        reg.get("tts").validate(reg.get("tts").adapt({"text": "x", "voice": "verse"})[0])
    assert reg.get("tts").adapt({"text": "x", "voice": "alloy"})[0]["voice"] == "Vietnamese_kindhearted_girl"
    assert reg.get("judge").estimate_cost({}) is None              # judge price depends on the chosen LLM
    for name, cls in (("minimax-video", MiniMaxVideoProvider), ("wan-video", WanVideoProvider)):
        c = {"live": {"video": {"provider": name, "model": "m"}}}
        assert isinstance(Registry("live", tmp_path, c).get("video"), cls)
    with pytest.raises(NotConfigured):
        Registry("live", tmp_path, {"live": {"video": {"provider": "seedream-image"}}}).get("video")


def test_openai_image_falls_back_to_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("HUOBAO_API_KEY", "hb")
    monkeypatch.setenv("T2I_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    img = Registry("live", tmp_path, {"live": {"image": {"provider": "openai-image", "model": "gpt-image-2"}}}).get("image")
    assert img.api_key == "hb" and img.base_url == "https://api.firemux.com/v1"
