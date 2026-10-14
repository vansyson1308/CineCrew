# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""
Offline engineering gate for CineCrew Studio (fixture providers; no network, no spend).

Fault injection: tags in a shot's start_state reach only the keyframe prompt, tags in end_state reach only the
clip prompt (see compose_image_prompt / compose_video_prompt).
"""

import json
import threading

import httpx
import pytest

from src.studio import Studio, domain, revisions
from src.studio.budget import Budget, BudgetBlocked, BudgetExceeded
from src.studio.export import ExportRefused
from src.studio.providers.base import (AmbiguousSubmission, NotConfigured, ProviderError, RateLimited,
                                       UnsupportedParameter)
from src.studio.providers.live import VeoVideoProvider
from tests.studio_helpers import fixture_config, generate_attempts, render_all, small_project


@pytest.fixture
def studio(tmp_path):
    return Studio(tmp_path / "studio", provider_config=fixture_config())


def _jobs(studio, ep):
    return {j["key"]: j for j in domain.jobs_for_episode(studio.store, ep["id"])}


def _tag(shot_index, field, tag):
    def m(spec):
        spec["shots"][shot_index][field] = (spec["shots"][shot_index].get(field) or "") + " " + tag
    return m


# ── provider contracts (Veo over a mock transport) ──────────────────────────

def _veo(handler):
    return VeoVideoProvider(api_key="test-key", price={"unit": "second", "usd": 0.1},
                            transport=httpx.MockTransport(handler))


def test_veo_contract_submit_poll_fetch(tmp_path, monkeypatch):
    seen = {}
    mp4 = (tmp_path / "remote.mp4")
    from tests.test_upstream_regressions import _tiny_mp4
    _tiny_mp4(mp4)

    def handler(req: httpx.Request):
        if req.url.path.endswith(":predictLongRunning"):
            seen["body"] = json.loads(req.content)
            seen["key"] = req.headers["x-goog-api-key"]
            return httpx.Response(200, json={"name": "models/veo/operations/op1"})
        if req.url.path.endswith("operations/op1"):
            seen["polls"] = seen.get("polls", 0) + 1
            if seen["polls"] == 1:
                return httpx.Response(200, json={"name": "op1", "done": False})
            return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {
                "generatedSamples": [{"video": {"uri": "https://files.example/v.mp4"}}]}}})
        if req.url.host == "files.example":
            return httpx.Response(200, content=mp4.read_bytes())
        return httpx.Response(404)

    v = _veo(handler)
    req, notes = v.adapt({"prompt": "p", "duration": 5, "size": "576x1024", "fps": 24, "seed": 7})
    assert req["duration"] == 6.0 and req["size"] == "720x1280" and "fps" not in req
    assert any("fps" in n for n in notes) and any("duration" in n for n in notes)
    rid = v.submit(req, "k", tmp_path).remote_id
    p = seen["body"]["parameters"]
    assert seen["key"] == "test-key" and p["aspectRatio"] == "9:16" and p["durationSeconds"] == "6" and p["seed"] == 7
    assert v.poll(rid).state == "running"
    assert v.poll(rid).state == "completed"
    out = v.fetch(rid, tmp_path / "dl")
    assert out.path.exists() and out.usage["seconds"] > 0


def test_veo_reference_images_force_8s():
    v = _veo(lambda r: httpx.Response(500))
    req, notes = v.adapt({"prompt": "p", "duration": 4, "size": "720x1280", "reference_images": [{"path": "a.png"}]})
    assert req["duration"] == 8.0 and any("8 s" in n for n in notes)


@pytest.mark.parametrize("status,exc,retryable", [(429, RateLimited, True), (503, ProviderError, True),
                                                  (400, ProviderError, False)])
def test_veo_error_classes(tmp_path, status, exc, retryable):
    v = _veo(lambda r: httpx.Response(status, headers={"retry-after": "3"}, text="nope"))
    with pytest.raises(exc) as e:
        v.submit({"prompt": "p", "duration": 4.0, "size": "720x1280"}, "k", tmp_path)
    assert e.value.retryable is retryable
    if status == 429:
        assert e.value.retry_after == 3.0


def test_veo_network_drop_is_ambiguous(tmp_path):
    def handler(r):
        raise httpx.ReadTimeout("timed out", request=r)
    with pytest.raises(AmbiguousSubmission):
        _veo(handler).submit({"prompt": "p", "duration": 4.0, "size": "720x1280"}, "k", tmp_path)


def test_veo_unsupported_parameter_and_missing_key(tmp_path):
    v = _veo(lambda r: httpx.Response(200, json={"name": "x"}))
    with pytest.raises(UnsupportedParameter):
        v.submit({"prompt": "p", "duration": 5.0, "size": "720x1280"}, "k", tmp_path)
    with pytest.raises(UnsupportedParameter):
        v.submit({"prompt": "p", "duration": 4.0, "size": "720x1280", "fps": 24}, "k", tmp_path)
    with pytest.raises(NotConfigured):
        VeoVideoProvider(api_key="").submit({"prompt": "p", "duration": 4.0, "size": "720x1280"}, "k", tmp_path)


def test_veo_filtered_result_and_failed_operation(tmp_path):
    def handler(r):
        if "op_err" in r.url.path:
            return httpx.Response(200, json={"done": True, "error": {"message": "safety"}})
        return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {}}})
    v = _veo(handler)
    assert v.poll("models/m/operations/op_err").state == "failed"
    with pytest.raises(ProviderError):
        v.fetch("models/m/operations/op_ok", tmp_path)


def test_unknown_live_price_blocks_paid_call():
    v = VeoVideoProvider(api_key="k", price={"unit": "second", "usd": None, "max_usd_per_call": None})
    assert v.estimate_cost({"duration": 8}) is None
    with pytest.raises(BudgetBlocked):
        Budget.__new__(Budget).reserve("p", None, "video")


# ── engine: complete render, failures, review ───────────────────────────────

def test_complete_render_is_final_and_reviewed_with_references(studio):
    p, ep = small_project(studio)
    r = render_all(studio, p, ep)
    assert r["status"] == "final", r
    assert r["contains_fixture_media"] is True
    calls = studio.engine.provider_for(p, "judge").calls
    kf_calls = {c["shot"]["shot_id"]: [r["label"] for r in c["references"]] for c in calls
                if c.get("media_kind") == "keyframe" and c["shot"]["shot_id"].startswith("shot_")}
    assert "canonical:loc_ngo_pho_co" in kf_calls["shot_01"] and "canonical:char_minh" in kf_calls["shot_01"]
    assert {"canonical:char_minh", "canonical:char_ba_tu"} <= set(kf_calls["shot_02"])
    # (previous_shot:<id> is added when the previous keyframe was already accepted; keyframes render in parallel)
    clip_calls = [c for c in calls if c.get("media_kind") == "clip"]
    assert clip_calls and all("approved_first_frame" in [r["label"] for r in c["references"]] for c in clip_calls)


def test_storyboard_gate_blocks_clips_until_approved(studio):
    p, ep = small_project(studio)
    studio.engine.run_until_idle(p["id"], timeout=120)
    jobs = _jobs(studio, ep)
    assert all(j["state"] == "blocked" and not j["selected_attempt"] for k, j in jobs.items() if k.startswith("clip:"))
    assert studio.engine.provider_for(p, "video").stats().get("submissions", 0) == 0


def test_failed_clip_blocks_readiness_and_export(studio):
    p, ep = small_project(studio, mutate=_tag(1, "end_state", "[[fail]]"))
    r = render_all(studio, p, ep)
    assert r["status"] == "blocked" and "clip:shot_02" in r["missing"]
    assert _jobs(studio, ep)["clip:shot_02"]["state"] == "failed"
    with pytest.raises(ExportRefused):
        studio.export(ep["id"])
    partial = studio.export(ep["id"], allow_draft=True)
    assert partial["status"] == "partial_preview"
    assert partial["manifest"]["inspection"]["probe"]["duration"] > 0


def test_rejected_clip_is_draft_not_final(studio):
    p, ep = small_project(studio, mutate=_tag(1, "end_state", "[[always_reject]]"))
    r = render_all(studio, p, ep)
    assert r["status"] == "draft" and "clip:shot_02" in r["not_accepted"]
    with pytest.raises(ExportRefused):
        studio.export(ep["id"])
    d = studio.export(ep["id"], allow_draft=True)
    assert d["status"] == "draft_preview"


def test_judge_error_is_not_acceptance(studio):
    p, ep = small_project(studio, mutate=_tag(0, "start_state", "[[judge_error]]"))
    studio.engine.run_until_idle(p["id"], timeout=120)
    kf = _jobs(studio, ep)["kf:shot_01"]
    assert kf["verdict"] in ("review_error", "pending") and kf["verdict"] != "accepted"
    assert studio.engine.readiness(ep["id"])["status"] != "final"


def test_rate_limit_is_retried_without_new_generation(studio):
    p, ep = small_project(studio, mutate=_tag(1, "end_state", "[[ratelimit]]"))
    r = render_all(studio, p, ep)
    assert r["status"] == "final"
    clip = _jobs(studio, ep)["clip:shot_02"]
    gens = studio.store.q("SELECT * FROM attempts WHERE job_id = ? AND purpose = 'generate'", (clip["id"],))
    accepted_by_provider = [a for a in gens if a["remote_id"]]
    refused = [a for a in gens if not a["remote_id"]]
    assert len(accepted_by_provider) == 1, "a 429 is a transport retry, not a second paid generation"
    assert refused and all(a["error"].startswith("transport:") for a in refused)
    led = studio.store.q("SELECT state FROM ledger WHERE attempt_id = ?", (refused[0]["id"],))
    assert [r["state"] for r in led] == ["released"], "a refused submit is never billed"


def test_attempts_are_immutable(studio):
    p, ep = small_project(studio, n_shots=1)
    render_all(studio, p, ep)
    a = studio.store.one("SELECT * FROM attempts WHERE purpose = 'generate' LIMIT 1")
    with pytest.raises(Exception):
        studio.store.update_attempt(a["id"], request={"prompt": "rewritten"})
    assert "/" not in json.dumps(a["request"]).replace("\\/", "") or "sha256:" in json.dumps(a["request"]) \
        or "image" not in a["request"]


# ── restart / recovery ──────────────────────────────────────────────────────

def test_restart_mid_render_does_not_resubmit(tmp_path):
    home = tmp_path / "studio"
    cfg = fixture_config()
    cfg["offline_fixture"]["video"]["latency_seconds"] = 1.5
    s1 = Studio(home, provider_config=cfg)
    p, ep = small_project(s1)
    s1.engine.run_until_idle(p["id"], timeout=120)
    s1.approve_storyboard(ep["id"])
    for _ in range(50):
        s1.engine.tick(p["id"])
        n = s1.store.one("SELECT COUNT(*) AS n FROM attempts WHERE provider_state IN ('submitted','running')")["n"]
        if n == 2:
            break
    assert n == 2, "both clips should be in flight before the simulated crash"
    del s1  # crash: nothing is closed or finished
    s2 = Studio(home, provider_config=cfg)
    stats = s2.engine.recover()
    assert stats["resumed_polling"] == 2
    s2.engine.run_until_idle(p["id"], timeout=120)
    assert s2.engine.readiness(ep["id"])["status"] == "final"
    assert s2.engine.provider_for(p, "video").stats()["submissions"] == 2


def test_interrupted_submit_idempotent_vs_unknown(tmp_path):
    home = tmp_path / "studio"
    s = Studio(home, provider_config=fixture_config())
    p, ep = small_project(s, n_shots=1)
    render_all(s, p, ep)
    a = s.store.one("SELECT a.* FROM attempts a JOIN jobs j ON j.id = a.job_id WHERE j.key = 'clip:shot_01' "
                    "AND a.purpose = 'generate'")
    # pretend the worker died between "submitting" and learning the remote id
    s.store.conn().execute("UPDATE attempts SET provider_state = 'submitting', remote_id = NULL WHERE id = ?", (a["id"],))
    before = s.engine.provider_for(p, "video").stats()["submissions"]
    stats = Studio(home, provider_config=fixture_config()).engine.recover()
    assert stats["resubmitted_idempotent"] == 1
    s2 = Studio(home, provider_config=fixture_config())
    s2.engine.run_until_idle(p["id"], timeout=60)
    v = s2.engine.provider_for(p, "video")
    assert v.stats()["submissions"] == before, "same idempotency key -> the provider replays, nothing new is billed"

    # a provider without idempotency must not be resubmitted blindly
    class NoIdem(type(v)):
        caps = type(v).caps.__class__(**{**type(v).caps.to_dict(), "idempotency": False}) \
            if hasattr(type(v).caps, "to_dict") else type(v).caps
    s.store.conn().execute("UPDATE attempts SET provider_state = 'submitting', remote_id = NULL WHERE id = ?", (a["id"],))
    s3 = Studio(home, provider_config=fixture_config())
    s3.engine.overrides[p["id"]] = {"video": NoIdem(home / "provider_state" / "fixture_video", 0.2)}
    st = s3.engine.recover()
    assert st["marked_unknown"] == 1
    job = s3.store.get("jobs", a["job_id"])
    assert job["state"] == "needs_review" and job["provider_state"] == "unknown"


def test_ambiguous_submission_is_reconciled_not_duplicated(studio):
    p, ep = small_project(studio, n_shots=1, mutate=_tag(0, "end_state", "[[ambiguous]]"))
    r = render_all(studio, p, ep)
    job = _jobs(studio, ep)["clip:shot_01"]
    subs = studio.engine.provider_for(p, "video").stats()["submissions"]
    # the fixture service created the job on the dropped call; an idempotent retry must find it, not create another
    assert subs == 1, (subs, job)
    assert r["status"] == "final"


def test_cancel_running_clip(tmp_path):
    cfg = fixture_config()
    cfg["offline_fixture"]["video"]["latency_seconds"] = 5
    s = Studio(tmp_path / "s", provider_config=cfg)
    p, ep = small_project(s, n_shots=1)
    s.engine.run_until_idle(p["id"], timeout=60)
    s.approve_storyboard(ep["id"])
    for _ in range(20):
        s.engine.tick(p["id"])
        if s.store.one("SELECT COUNT(*) AS n FROM attempts WHERE provider_state = 'submitted'")["n"]:
            break
    clip = _jobs(s, ep)["clip:shot_01"]
    s.engine.cancel(clip["id"])
    s.engine.run_until_idle(p["id"], timeout=30)
    clip = s.store.get("jobs", clip["id"])
    assert clip["state"] == "cancelled"
    assert s.engine.readiness(ep["id"])["status"] == "blocked"


# ── budget ──────────────────────────────────────────────────────────────────

def test_budget_reservations_are_atomic_under_threads(studio):
    p = domain.create_project(studio.store, "b", budget_limit_usd=1.0)
    b = Budget(studio.store)
    ok, refused = [], []

    def worker():
        try:
            ok.append(b.reserve(p["id"], 0.1, "video"))
        except BudgetExceeded:
            refused.append(1)
    ts = [threading.Thread(target=worker) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 10 and len(refused) == 20
    for rid in ok[:4]:
        b.commit(rid, 0.05, "test")
    for rid in ok[4:]:
        b.release(rid)
    sm = b.summary(p["id"])
    assert sm["committed_usd"] == pytest.approx(0.2) and sm["reserved_usd"] == 0 and sm["remaining_usd"] == pytest.approx(0.8)


def test_engine_stops_at_budget_and_resumes_after_raise(tmp_path):
    s = Studio(tmp_path / "s", provider_config=fixture_config(image=0.1, video=0.0, tts=0.0, judge=0.0))
    p, ep = small_project(s, n_shots=2, budget=0.15)
    s.engine.run_until_idle(p["id"], timeout=120)
    led = s.engine.budget.summary(p["id"])
    assert led["committed_usd"] + led["reserved_usd"] <= 0.15 + 1e-9
    images = s.store.q("SELECT * FROM jobs WHERE kind = 'image' AND (episode_id = ? OR episode_id = ?)",
                       (ep["id"], p["id"]))
    assert len(images) == 6  # 4 reference sheets + 2 keyframes
    assert sum(j["state"] == "done" for j in images) == 1
    assert any("budget" in (j["error"] or "").lower() for j in images)
    # blocked stays blocked on later ticks (no flip-flop), until the director raises the budget
    s.engine.run_until_idle(p["id"], timeout=10)
    assert sum(j["state"] == "done" for j in s.store.q("SELECT * FROM jobs WHERE kind = 'image'")) == 1
    s.set_budget(p["id"], 1.0)
    s.engine.run_until_idle(p["id"], timeout=120)
    assert all(j["state"] == "done" for j in s.store.q("SELECT * FROM jobs WHERE kind = 'image'"))
    assert s.engine.budget.summary(p["id"])["committed_usd"] == pytest.approx(0.6)


def test_live_project_without_price_is_blocked_before_any_call(tmp_path):
    s = Studio(tmp_path / "s")
    p, ep = small_project(s, n_shots=1, mode="live", budget=5.0)
    s.engine.run_until_idle(p["id"], timeout=20)
    assert s.store.one("SELECT COUNT(*) AS n FROM attempts WHERE remote_id IS NOT NULL")["n"] == 0
    jobs = _jobs(s, ep)
    assert all(j["state"] in ("blocked", "pending", "waiting") for j in jobs.values())
    assert s.engine.budget.summary(p["id"])["committed_usd"] == 0


# ── revisions: selective regeneration, rollback, locks ──────────────────────

def test_revision_regenerates_only_affected_and_rollback_is_free(studio):
    p, ep = small_project(studio, n_shots=3)
    assert render_all(studio, p, ep)["status"] == "final"
    before_sel = {k: j["selected_attempt"] for k, j in _jobs(studio, ep).items()}
    bp = domain.load_blueprint(studio.store.get("episodes", ep["id"]))
    patch = revisions.resolve_edit("Đổi ánh sáng cảnh 2 thành ánh đèn lồng đỏ", bp,
                                   domain.assets_by_asset_id(studio.store, p["id"]))
    assert not patch.ambiguous and patch.ops[0].shot_id == "shot_02"
    pv = revisions.preview(studio, ep["id"], patch)
    assert _affected(pv) == {"kf:shot_02", "clip:shot_02"}, pv["impact"]
    assert pv["requires_confirmation"] is False and pv["estimate_usd"] == 0
    n0 = generate_attempts(studio)
    revisions.apply(studio, ep["id"], patch)
    studio.engine.run_until_idle(p["id"], timeout=120)
    assert generate_attempts(studio) - n0 == 2
    after = _jobs(studio, ep)
    for k, sel in before_sel.items():
        if k not in ("kf:shot_02", "clip:shot_02"):
            assert after[k]["selected_attempt"] == sel, f"{k} must be untouched"
    assert studio.engine.readiness(ep["id"])["status"] == "final"
    n1 = generate_attempts(studio)
    revisions.rollback(studio, ep["id"], 1)
    studio.engine.run_until_idle(p["id"], timeout=60)
    assert generate_attempts(studio) == n1, "rollback restores from cache with zero generations"
    restored = _jobs(studio, ep)
    assert {k: j["selected_attempt"] for k, j in restored.items()} == before_sel


def _affected(pv):
    return {i["key"] for e in pv["impact"] for i in e["affected"]}


def test_asset_change_spans_episodes_and_needs_confirmation(studio):
    p = studio.create_series("hanoi_delivery")
    eps = studio.store.q("SELECT * FROM episodes WHERE project_id = ?", (p["id"],))
    patch = revisions.EditPatch(ops=[revisions.EditOp(target="asset", asset_id="char_minh", field="wardrobe_default",
                                                      value="red raincoat")])
    pv = revisions.preview(studio, eps[0]["id"], patch)
    assert pv["episodes_affected"] == 2 and pv["requires_confirmation"]
    assert "ref:char_minh" in _affected(pv)
    with pytest.raises(revisions.RevisionError):
        revisions.apply(studio, eps[0]["id"], patch)


def test_locked_shot_refuses_edit(studio):
    p, ep = small_project(studio, n_shots=2)
    e = studio.store.get("episodes", ep["id"])
    e["studio"]["shots"]["shot_01"]["locked"] = True
    studio.store.update("episodes", "id", ep["id"], {"studio": e["studio"]})
    patch = revisions.EditPatch(ops=[revisions.EditOp(target="shot", shot_id="shot_01", field="lighting",
                                                      value="neon")])
    assert revisions.preview(studio, ep["id"], patch)["lock_conflicts"]
    with pytest.raises(revisions.RevisionError):
        revisions.apply(studio, ep["id"], patch)


def test_ambiguous_edit_asks_question(studio):
    p, ep = small_project(studio, n_shots=1)
    bp = domain.load_blueprint(studio.store.get("episodes", ep["id"]))
    patch = revisions.resolve_edit("làm cho đẹp hơn", bp, domain.assets_by_asset_id(studio.store, p["id"]))
    assert patch.ambiguous and patch.question and not patch.ops


# ── export guarantees ───────────────────────────────────────────────────────

def test_export_checks_and_manifest(studio):
    p, ep = small_project(studio, n_shots=2)
    render_all(studio, p, ep)
    ex = studio.export(ep["id"], aspect="16:9")
    m = ex["manifest"]
    assert ex["status"] == "final" and m["inspection"]["all_pass"], m["inspection"]
    pr = m["inspection"]["probe"]
    assert (pr["width"], pr["height"]) == (1920, 1080) and pr["audio_streams"] >= 1
    assert m["contains_fixture_media"] is True
    tl_shots = [s["shot_id"] for s in m["timeline"]["shots"]] if "timeline" in m else None
    assert tl_shots in (None, ["shot_01", "shot_02"])
    assert studio.export(ep["id"], aspect="16:9")["reused"] is True


def test_subtitle_cues_never_dropped(studio):
    p, ep = small_project(studio, n_shots=3)
    render_all(studio, p, ep)
    ex = studio.export(ep["id"])
    srt = open(ex["files"]["srt"], encoding="utf-8").read()
    bp = domain.load_blueprint(studio.store.get("episodes", ep["id"]))
    for s in bp.shots:
        d = s.narrative_layer.dialogue
        if d.has_dialogue:
            for word in d.text.split()[:3]:
                assert word in srt


# ── migration / segmentation / planning / API ───────────────────────────────

def test_blueprint_migration_1_0_to_1_1():
    from src.studio.migrations import migrate_blueprint
    doc = {"metadata": {}, "shots": [], "final_film_path": "x/film.mp4"}
    out = migrate_blueprint(doc)
    assert out["schema_version"] == "1.1" and out.get("draft_film_path") == "x/film.mp4"
    assert not out.get("final_film_path")
    assert migrate_blueprint(out) == out


def test_script_segmentation_reports_coverage():
    from src.skills.script_segmentation import segment_script, select_within
    text = "\n\n".join(f"CẢNH {i}. NGÕ PHỐ - NGÀY\nMinh đi giao hàng lần thứ {i}. " + "x " * 200 for i in range(1, 9))
    segs = segment_script(text)
    assert len(segs) == 8
    rep = select_within(text, 1500)
    assert rep["coverage_report"]["omitted_segments"] if "coverage_report" in rep else rep["omitted"]


def test_live_planning_refuses_without_live_mode_and_price(studio, monkeypatch):
    from src.studio.planning import PlanningRefused, plan_episode_live
    p, _ = small_project(studio, n_shots=1)
    with pytest.raises(PlanningRefused):
        plan_episode_live(studio, p["id"], "x", "script", run_pipeline=lambda *a, **k: {})
    live = domain.create_project(studio.store, "L", mode="live", budget_limit_usd=10)
    with pytest.raises(BudgetBlocked):
        plan_episode_live(studio, live["id"], "x", "script", run_pipeline=lambda *a, **k: {})


def test_live_planning_imports_blueprint_and_settles_budget(studio, blueprint_factory):
    from src.studio.planning import plan_episode_live
    cfg = fixture_config()
    cfg["live"]["planning"]["max_usd_per_episode"] = 0.5
    live = domain.create_project(studio.store, "L", mode="live", budget_limit_usd=1.0)
    t = domain.load_template("hanoi_delivery")
    for a in t["assets"]:
        domain.add_asset(studio.store, live["id"], a["kind"], a["name"],
                         {k: v for k, v in a.items() if k not in ("kind", "name")})
    from tests.conftest import make_shot
    bp = blueprint_factory([make_shot("shot_01", 4, "Xin chào", speaker="char_minh"), make_shot("shot_02", 4)])
    seen = {}

    def fake_pipeline(script, run_root=None, initial_ctx=None, stages=None):
        seen["library_ids"] = [c.id for c in initial_ctx["asset_library"].characters]
        seen["stages"] = [s["id"] for s in stages]
        return {"scene_blueprint": bp}
    ep = plan_episode_live(studio, live["id"], "Planned", "script", provider_config=cfg, run_pipeline=fake_pipeline)
    assert "char_minh" in seen["library_ids"] and "art_department" not in seen["stages"]
    assert ep["planning"]["mode"] == "crew" and len(domain.load_blueprint(ep).shots) == 2
    sm = studio.engine.budget.summary(live["id"])
    assert sm["committed_usd"] == pytest.approx(0.5) and sm["reserved_usd"] == 0  # unknown token price -> the cap


def test_api_end_to_end(tmp_path):
    from fastapi.testclient import TestClient
    from src.studio.api import create_app
    s = Studio(tmp_path / "s", provider_config=fixture_config())
    c = TestClient(create_app(s, start_worker=False, serve_ui=False))
    assert c.get("/api/health").json()["ok"]
    p, ep = small_project(s, n_shots=1)
    r = c.get(f"/api/episodes/{ep['id']}").json()
    assert r["readiness"]["status"] == "blocked"
    assert c.post(f"/api/episodes/{ep['id']}/export", json={}).status_code == 409
    render_all(s, p, ep)
    assert c.get(f"/api/episodes/{ep['id']}").json()["readiness"]["status"] == "final"
    pv = c.post(f"/api/episodes/{ep['id']}/edits/preview", json={"text": "Đổi ánh sáng cảnh 1 thành ánh trăng"})
    assert pv.status_code == 200 and "kf:shot_01" in _affected(pv.json())
    ex = c.post(f"/api/episodes/{ep['id']}/export", json={"aspect": "9:16"})
    assert ex.status_code == 200 and ex.json()["status"] == "final"
