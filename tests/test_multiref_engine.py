# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""
The studio engine driving a multi-reference video backend end to end: a live-mode project whose clips go
to MiniMaxVideoProvider / SeedanceVideoProvider over an httpx.MockTransport (real request shapes, real MP4
bytes from ffmpeg), keyframes/voices/judge from the offline fixtures. Proves that the asset sheets reach
the video request, the ledger settles, restarts reconcile and the export is final. No network, no spend.
"""

import json
import threading

import httpx

from src.studio import Studio
from src.studio.providers.multiref import MiniMaxVideoProvider, SeedanceVideoProvider
from tests.studio_helpers import fixture_config, render_all, small_project
from tests.test_upstream_regressions import _tiny_mp4


class FakeRemote:
    """In-memory video service: POST -> task id, GET task -> succeeded after `polls_needed` polls."""

    def __init__(self, tmp_path, polls_needed=1):
        self.mp4 = tmp_path / "remote.mp4"
        _tiny_mp4(self.mp4)
        self.bodies, self.polls, self.lock, self.polls_needed = [], {}, threading.Lock(), polls_needed

    def handler(self, r: httpx.Request):
        with self.lock:
            if r.method == "POST":
                self.bodies.append(json.loads(r.content))
                tid = f"task{len(self.bodies)}"
                return httpx.Response(200, json={"task_id": tid, "id": tid})
            if r.url.host == "cdn.test":
                return httpx.Response(200, content=self.mp4.read_bytes())
            tid = r.url.path.rsplit("/", 1)[-1]
            self.polls[tid] = self.polls.get(tid, 0) + 1
            done = self.polls[tid] > self.polls_needed
            return httpx.Response(200, json={
                "id": tid, "status": "succeeded" if done else "running",
                "content": {"video_url": f"https://cdn.test/{tid}.mp4"},
                "usage": {"completion_tokens": 50000, "total_tokens": 50000},
                "task": {"status": "succeeded" if done else "processing",
                         "content": {"url": f"https://cdn.test/{tid}.mp4"}}})


def _live_studio(tmp_path, provider):
    s = Studio(tmp_path / "s", provider_config=fixture_config())
    p, ep = small_project(s, n_shots=2, mode="live", budget=5.0)
    fx = Studio(tmp_path / "fx", provider_config=fixture_config())   # borrow fixture image/tts/judge providers
    pf, _ = small_project(fx, n_shots=1)
    s.engine.overrides[p["id"]] = {k: fx.engine.provider_for(pf, k) for k in ("image", "tts", "judge")}
    s.engine.overrides[p["id"]]["video"] = provider
    return s, p, ep


def test_reference_mode_clips_carry_asset_sheets_and_export_final(tmp_path):
    remote = FakeRemote(tmp_path)
    prov = MiniMaxVideoProvider("MiniMax-H3", {"video_mode": "references"}, {"unit": "second", "usd": 0.05},
                                transport=httpx.MockTransport(remote.handler), api_key="mm", base_url="https://mm.test")
    s, p, ep = _live_studio(tmp_path, prov)
    r = render_all(s, p, ep, timeout=240)
    assert r["status"] == "final", r
    assert len(remote.bodies) == 2                                    # one submission per clip, no duplicates
    for b in remote.bodies:
        roles = [c.get("role") for c in b["content"]]
        assert "first_frame" not in roles and roles.count("reference_image") >= 3   # sheets + approved keyframe
        assert b["content"][0]["text"].startswith("Reference images: [Image 1] ")
        assert "<char_minh>" in b["content"][0]["text"]
    att = s.store.q("SELECT * FROM attempts WHERE provider = 'minimax-video'")
    assert len(att) == 2 and all(len(a["reference_hashes"]) >= 3 for a in att)   # keyframe + >= 2 asset sheets
    assert all(any(k.startswith("approved keyframe") for k in a["reference_hashes"]) for a in att)
    led = s.engine.budget.summary(p["id"])
    assert led["reserved_usd"] == 0 and 0 < led["committed_usd"] <= 5.0
    ex = s.export(ep["id"])
    assert ex["status"] == "final" and ex["manifest"]["inspection"]["all_pass"], ex["manifest"]["inspection"]


def test_seedance_first_frame_mode_sends_no_sheets_and_survives_restart(tmp_path):
    remote = FakeRemote(tmp_path, polls_needed=3)

    def make():
        return SeedanceVideoProvider("seedance-2-0-fast-260128", {}, {"unit": "second", "usd": 0.05,
                                                                       "usd_per_1m_tokens": 1.0},
                                     transport=httpx.MockTransport(remote.handler), api_key="ark",
                                     base_url="https://ark.test/api/v3")
    s, p, ep = _live_studio(tmp_path, make())
    s.engine.run_until_idle(p["id"], timeout=120)
    s.approve_storyboard(ep["id"])
    for _ in range(40):                                               # submit the clips, stop before they finish
        s.engine.tick(p["id"])
        if len(remote.bodies) == 2:
            break
    assert len(remote.bodies) == 2
    # a new process: fresh Studio over the same home, a fresh provider instance (no in-memory results)
    s2 = Studio(tmp_path / "s", provider_config=fixture_config())
    s2.engine.overrides[p["id"]] = dict(s.engine.overrides[p["id"]], video=make())
    s2.engine.run_until_idle(p["id"], timeout=240)
    assert s2.engine.readiness(ep["id"])["status"] == "final"
    assert len(remote.bodies) == 2                                    # reconciled by polling, never resubmitted
    for b in remote.bodies:
        assert [c.get("role") for c in b["content"]] == [None, "first_frame"]
        assert b["generate_audio"] is False
    costs = [a["cost_actual_usd"] for a in s2.store.q("SELECT * FROM attempts WHERE provider = 'seedance'")]
    assert costs == [0.05, 0.05]                                      # 50 000 tokens x $1 / 1M tokens
