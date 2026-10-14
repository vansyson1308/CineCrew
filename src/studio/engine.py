#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Execution engine (the worker). One `tick()` is one scheduling pass:

  1. reconcile  in-flight attempts are polled; results are fetched, stored, reviewed
  2. evaluate   jobs whose dependencies are satisfied become ready; done jobs whose inputs changed
                (an upstream re-render, a revision) become pending again
  3. dispatch   ready jobs: cache lookup -> adapt/validate -> budget reservation -> immutable attempt
                row -> submit (remote id persisted the moment it is returned)

Every state transition is written to SQLite before the next external call, so a crash at any point
leaves enough to resume: `recover()` (run at worker start) polls submitted remote jobs instead of
paying for them again, resubmits an interrupted submission only when the provider supports
idempotency keys, and otherwise marks it `unknown` for a human to resolve.

State axes (never collapsed): job.state (scheduling), job.provider_state / attempt.provider_state
(transport), job.verdict / attempt.verdict (quality). Final export readiness is derived from them in
readiness().
"""

import concurrent.futures as cf
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import avtools
from .budget import Budget, BudgetBlocked, BudgetExceeded
from .domain import load_blueprint, resolve_dep
from .hashing import hash_obj
from .media_store import MediaStore
from .providers import (
    AmbiguousSubmission, NotConfigured, Provider, ProviderError, RateLimited, Registry, UnsupportedParameter,
)
from .store import Store, new_id, now

MAX_TRANSPORT_RETRIES = 4
LOCAL_TIMEOUT_S = 900          # after this the UI says "still running remotely"; polling continues
MAX_REMOTE_WAIT_S = 3 * 3600   # after this the attempt is timed_out (remote id kept for later reconcile)
LEASE_S = 120
SPEECH_LEAD_S, SPEECH_TAIL_S, SPEECH_GAP_S = 0.3, 0.6, 0.3
SATISFIED = ("accepted", "unreviewed")


class Engine:
    def __init__(self, store: Store, media: MediaStore, home: Path, provider_config: Optional[Dict] = None):
        self.store, self.media, self.home = store, media, Path(home)
        self.budget = Budget(store)
        self.provider_config = provider_config
        self._registries: Dict[str, Registry] = {}
        self._lock = threading.RLock()
        self.overrides: Dict[str, Dict[str, Provider]] = {}   # tests: {project_id: {kind: provider}}

    # ── providers ──────────────────────────────────────────────────────────
    def registry(self, project: Dict[str, Any]) -> Registry:
        key = f"{project['id']}:{project['mode']}"
        with self._lock:
            if key not in self._registries:
                self._registries[key] = Registry(project["mode"], self.home / "provider_state", self.provider_config,
                                                 self.overrides.get(project["id"]))
            return self._registries[key]

    def provider_for(self, project: Dict[str, Any], kind: str) -> Provider:
        return self.registry(project).get(kind)

    # ── public loop ────────────────────────────────────────────────────────
    def tick(self, project_id: Optional[str] = None) -> int:
        actions = self._reconcile(project_id)
        actions += self._evaluate(project_id)
        actions += self._dispatch(project_id)
        return actions

    def run_until_idle(self, project_id: Optional[str] = None, timeout: float = 120, poll: float = 0.1) -> int:
        deadline, total, idle = time.time() + timeout, 0, 0
        while time.time() < deadline:
            n = self.tick(project_id)
            total += n
            busy = self.store.one("SELECT COUNT(*) AS n FROM jobs j JOIN projects p ON 1=1 WHERE j.state IN "
                                  "('running','ready') AND (? IS NULL OR j.episode_id IN (SELECT id FROM episodes "
                                  "WHERE project_id = ?) OR j.episode_id = ?)", (project_id, project_id, project_id))["n"]
            if n == 0 and busy == 0:
                idle += 1
                if idle >= 2:
                    return total
            else:
                idle = 0
            time.sleep(poll)
        return total

    def serve_forever(self, stop: threading.Event, interval: float = 0.5) -> None:
        self.recover()
        while not stop.is_set():
            try:
                n = self.tick()
            except Exception:  # noqa: BLE001 — a worker must survive one bad job
                self.store.event("error", "worker tick failed: " + traceback.format_exc()[-600:])
                n = 0
            stop.wait(0.05 if n else interval)

    # ── recovery ───────────────────────────────────────────────────────────
    def recover(self) -> Dict[str, int]:
        """Run at worker start. Never blindly resubmits."""
        stats = {"resumed_polling": 0, "resubmitted_idempotent": 0, "marked_unknown": 0, "released_leases": 0}
        for a in self.store.q("SELECT a.*, j.episode_id FROM attempts a JOIN jobs j ON j.id = a.job_id "
                              "WHERE a.provider_state = 'submitting'"):
            job = self.store.get("jobs", a["job_id"])
            project = self._project_of(job)
            prov = self.provider_for(project, "judge" if a["purpose"] == "review" else job["kind"])
            if prov.caps.idempotency and a["purpose"] == "generate":
                self.store.update_attempt(a["id"], provider_state="reserved", error="interrupted during submit; "
                                          "resubmitting with the same idempotency key")
                self.store.update("jobs", "id", job["id"], {"state": "running", "lease_until": 0})
                stats["resubmitted_idempotent"] += 1
            else:
                self.store.update_attempt(a["id"], provider_state="unknown",
                                          error="interrupted during submit; provider has no idempotency key — "
                                                "not resubmitted to avoid paying twice")
                self.store.update("jobs", "id", job["id"], {"state": "needs_review", "provider_state": "unknown",
                                                            "error": "submission outcome unknown — check the provider "
                                                                     "console, then 'resolve' (reconcile or resubmit)",
                                                            "lease_until": 0})
                stats["marked_unknown"] += 1
        for a in self.store.q("SELECT * FROM attempts WHERE provider_state IN ('submitted','running') "
                              "AND remote_id IS NOT NULL"):
            self.store.update("jobs", "id", a["job_id"], {"state": "running", "lease_until": 0})
            stats["resumed_polling"] += 1
        n = self.store.conn().execute("UPDATE jobs SET lease_until = 0, state = 'ready' WHERE state = 'running' AND "
                                      "id NOT IN (SELECT job_id FROM attempts WHERE provider_state IN "
                                      "('submitted','running','reserved','submitting'))").rowcount
        stats["released_leases"] = n
        self.store.event("info", f"worker recovered: {stats}")
        return stats

    # ── 1. reconcile in-flight ─────────────────────────────────────────────
    def _reconcile(self, project_id: Optional[str]) -> int:
        actions = 0
        rows = self.store.q("SELECT * FROM attempts WHERE provider_state IN ('submitted','running','reserved') "
                            "AND purpose = 'generate'")
        for a in rows:
            job = self.store.get("jobs", a["job_id"])
            project = self._project_of(job)
            if project_id and project["id"] != project_id:
                continue
            if a["provider_state"] == "reserved":
                # an idempotent resubmission scheduled by recover()
                actions += self._submit(job, project, a)
                continue
            if job["cancel_requested"]:
                self._cancel_attempt(job, project, a)
                actions += 1
                continue
            if a["last_polled_at"] and now() - a["last_polled_at"] < 0.2:
                continue
            prov = self.provider_for(project, job["kind"])
            try:
                pr = prov.poll(a["remote_id"])
            except Exception as e:  # noqa: BLE001
                self.store.update_attempt(a["id"], last_polled_at=now(), error=f"poll error: {e}")
                continue
            self.store.update_attempt(a["id"], last_polled_at=now(), provider_state=
                                      "running" if pr.state in ("running", "queued") else a["provider_state"])
            if pr.state == "completed":
                try:
                    out = prov.fetch(a["remote_id"], self.media.scratch("downloads"))
                except Exception as e:  # noqa: BLE001
                    self.store.update_attempt(a["id"], error=f"download failed (will retry): {e}")
                    continue
                self._complete(job, project, self.store.get("attempts", a["id"]), out)
                actions += 1
            elif pr.state == "failed":
                self._fail_after_submit(job, project, a, pr.error or "provider reported failure")
                actions += 1
            elif pr.state in ("running", "queued"):
                waited = now() - (a["submitted_at"] or now())
                if waited > MAX_REMOTE_WAIT_S:
                    self.store.update_attempt(a["id"], provider_state="timed_out",
                                              error=f"no result after {waited:.0f}s; remote id kept for reconcile")
                    if a["reservation_id"]:
                        self.budget.commit(a["reservation_id"], None, "timed out (conservative: may be billed)")
                    self._set_job(job, state="needs_review", provider_state="timed_out",
                                  error=f"remote job {a['remote_id']} timed out; use 'reconcile' if it finishes later")
                    actions += 1
                elif waited > LOCAL_TIMEOUT_S and not (job["error"] or "").startswith("still running"):
                    self._set_job(job, error=f"still running remotely after {waited:.0f}s — polling continues; "
                                             "it will not be resubmitted")
            else:  # unknown
                self.store.update_attempt(a["id"], error=f"poll: {pr.error}")
        return actions

    # ── 2. evaluate dependencies / staleness ───────────────────────────────
    def _evaluate(self, project_id: Optional[str]) -> int:
        actions = 0
        jobs = self.store.q("SELECT * FROM jobs WHERE state IN ('pending','ready','blocked','waiting','done') "
                            "AND cancel_requested = 0")
        for job in jobs:
            project = self._project_of(job)
            if project_id and project["id"] != project_id:
                continue
            if job["key"].startswith("ref:"):
                ep = {"id": job["episode_id"], "project_id": project["id"]}
            else:
                ep = self.store.get("episodes", job["episode_id"])
            ok, why = self._deps_ok(job, ep)
            if job["state"] == "done":
                if job["locked"] or job["verdict_source"] == "supplied":
                    continue
                if not ok:
                    self._set_job(job, state="pending", error=f"upstream changed: {why}")
                    actions += 1
                    continue
                try:
                    _, material = self._build_request(job, project, ep, attempt_no=0)
                except Exception:  # noqa: BLE001
                    continue
                if job["input_hash"] and self._input_hash(job, project, material) != job["input_hash"]:
                    self._set_job(job, state="pending", selected_attempt=None, verdict="pending", verdict_source=None,
                                  cache_hit=0, error="inputs changed upstream; re-running")
                    actions += 1
                continue
            if job["state"] == "blocked" and not (job["error"] or "").startswith("storyboard"):
                continue  # budget / provider-contract blocks are sticky until settings change (unblock())
            if not ok:
                new_state = "waiting" if "needs review" in why else "pending"
                if job["state"] != new_state or job["error"] != why:
                    self._set_job(job, state=new_state, error=why)
                continue
            if job["kind"] == "video" and project["settings"].get("require_storyboard_approval", True) \
                    and not ep.get("storyboard_approved"):
                if job["state"] != "blocked" or job["error"] != "storyboard not approved yet":
                    self._set_job(job, state="blocked", error="storyboard not approved yet")
                continue
            if job["state"] != "ready":
                keep = job["error"] if (job["error"] or "").startswith(("rejected", "retrying", "generation failed",
                                                                         "retry", "inputs changed")) else None
                self._set_job(job, state="ready", error=keep)
                actions += 1
        return actions

    def _deps_ok(self, job: Dict[str, Any], ep: Dict[str, Any]) -> Tuple[bool, str]:
        for key in job["deps"]:
            dep = resolve_dep(self.store, ep, key)
            if dep is None:
                return False, f"missing dependency {key}"
            if dep["state"] == "done" and dep["verdict"] in SATISFIED and dep["selected_attempt"] or \
                    dep["verdict_source"] == "supplied":
                continue
            if dep["state"] in ("needs_review", "failed", "cancelled"):
                return False, f"waiting: {key} needs review ({dep['state']})"
            return False, f"waiting for {key}"
        return True, ""

    # ── 3. dispatch ────────────────────────────────────────────────────────
    def _dispatch(self, project_id: Optional[str]) -> int:
        ready = self.store.q("SELECT * FROM jobs WHERE state = 'ready' AND next_run_at <= ? AND lease_until < ? "
                             "ORDER BY CASE kind WHEN 'tts' THEN 0 WHEN 'image' THEN 1 ELSE 2 END, created_at",
                             (now(), now()))
        claimed = []
        for job in ready:
            project = self._project_of(job)
            if project_id and project["id"] != project_id:
                continue
            n = self.store.update("jobs", "id", job["id"], {"state": "running", "lease_until": now() + LEASE_S},
                                  where_extra=" AND state = 'ready' AND lease_until < ?", extra_args=(now(),))
            if n == 1:
                claimed.append((self.store.get("jobs", job["id"]), project))
        if not claimed:
            return 0
        workers = max(1, min(int(claimed[0][1]["settings"].get("concurrency", 2)), len(claimed)))
        if workers == 1:
            for job, project in claimed:
                self._run_job(job, project)
        else:
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(lambda jp: self._run_job(*jp), claimed))
        return len(claimed)

    def _run_job(self, job: Dict[str, Any], project: Dict[str, Any]) -> None:
        try:
            self._start_attempt(job, project)
        except Exception as e:  # noqa: BLE001 — record, never crash the worker
            self._set_job(self.store.get("jobs", job["id"]), state="failed", error=f"internal error: {e}",
                          lease_until=0)
            self.store.event("error", f"{job['key']}: {traceback.format_exc()[-800:]}", job_id=job["id"])

    def _start_attempt(self, job: Dict[str, Any], project: Dict[str, Any]) -> None:
        ep = {"id": job["episode_id"], "project_id": project["id"]} if job["key"].startswith("ref:") \
            else self.store.get("episodes", job["episode_id"])
        gen = self._generate_attempts(job)
        request, material = self._build_request(job, project, ep, attempt_no=len(gen))
        prov = self.provider_for(project, job["kind"])
        input_hash = self._input_hash(job, project, self._build_request(job, project, ep, attempt_no=0)[1])

        # cache: an accepted attempt with identical planned inputs => zero model calls
        if not job["force_new"] and not gen:
            hit = self._cached_attempt(input_hash, project)
            if hit:
                self._set_job(job, state="done", selected_attempt=hit["id"], verdict=hit["verdict"],
                              verdict_source="cache", input_hash=input_hash, cache_hit=1, provider_state="completed",
                              error=None, lease_until=0)
                self.store.event("info", f"{job['key']}: cache hit ({hit['id']}), no model call", job_id=job["id"])
                return
        if len(gen) >= job["max_attempts"]:
            self._exhausted(job, "attempt budget exhausted")
            return

        try:
            adapted, notes = prov.adapt(request)
            prov.validate(adapted)
        except UnsupportedParameter as e:
            self._set_job(job, state="blocked", error=f"provider contract: {e}", lease_until=0)
            return
        estimate = prov.estimate_cost(adapted)
        attempt_id = new_id("att")
        try:
            res_id = self.budget.reserve(project["id"], estimate, job["kind"], job["id"], attempt_id,
                                         note=f"{job['key']} attempt {len(gen) + 1}")
        except (BudgetBlocked, BudgetExceeded) as e:
            self._set_job(job, state="blocked", error=f"budget: {e}", lease_until=0)
            self.store.event("warn", f"{job['key']}: {e}", job_id=job["id"])
            return
        refs = {r["label"]: r["sha"] for r in adapted.get("reference_images") or []}
        if adapted.get("image"):
            refs["first_frame"] = material.get("image_sha")
        attempt = {
            "id": attempt_id, "job_id": job["id"], "n": self._attempt_number(job), "purpose": "generate", "provider": prov.name,
            "model": prov.model, "request": {"params": _public(adapted), "adaptations": notes},
            "request_hash": hash_obj(_public(adapted)), "input_hash": input_hash, "reference_hashes": refs,
            "seed": adapted.get("seed"), "idempotency_key": attempt_id, "remote_id": None,
            "provider_state": "reserved", "submitted_at": None, "completed_at": None, "last_polled_at": None,
            "verdict": "pending", "review": None, "error": None, "usage": None, "cost_estimate_usd": estimate,
            "cost_actual_usd": None, "reservation_id": res_id, "media_sha": None, "media_meta": None,
            "created_at": now(),
        }
        self.store.insert("attempts", attempt)
        self._set_job(job, input_hash=input_hash, provider_state="reserved", force_new=0)
        self._submit(self.store.get("jobs", job["id"]), project, attempt)

    def _submit(self, job: Dict[str, Any], project: Dict[str, Any], attempt: Dict[str, Any]) -> int:
        prov = self.provider_for(project, job["kind"])
        params = _materialize(attempt["request"]["params"], self.media)
        self.store.update_attempt(attempt["id"], provider_state="submitting")
        self._set_job(job, provider_state="submitting")
        try:
            sr = prov.submit(params, attempt["idempotency_key"], self.media.scratch("downloads"))
        except RateLimited as e:
            return self._transport_retry(job, attempt, f"rate limited: {e}", e.retry_after)
        except AmbiguousSubmission as e:
            if prov.caps.idempotency:
                self.store.update_attempt(attempt["id"], provider_state="reserved",
                                          error=f"ambiguous submit ({e}); resubmitting with the same idempotency key")
                self._set_job(job, provider_state="reserved")
                return 1
            self.store.update_attempt(attempt["id"], provider_state="unknown", error=str(e))
            self._set_job(job, state="needs_review", provider_state="unknown", lease_until=0,
                          error="submission outcome unknown and the provider has no idempotency key — not resubmitted")
            return 1
        except (UnsupportedParameter, NotConfigured) as e:
            self.store.update_attempt(attempt["id"], provider_state="failed", error=f"{e.code}: {e}")
            self.budget.release(attempt["reservation_id"], "rejected before generation")
            self._set_job(job, state="blocked", provider_state="failed", error=f"{e.code}: {e}", lease_until=0)
            return 1
        except ProviderError as e:
            if e.retryable:
                return self._transport_retry(job, attempt, str(e), None)
            self.store.update_attempt(attempt["id"], provider_state="failed", error=str(e))
            self.budget.release(attempt["reservation_id"], "provider refused the request")
            self._after_failure(job, f"generation failed: {e}")
            return 1
        self.store.update_attempt(attempt["id"], remote_id=sr.remote_id, provider_state="submitted",
                                  submitted_at=now())
        self._set_job(job, provider_state="submitted")
        if sr.output is not None:
            self._complete(self.store.get("jobs", job["id"]), project, self.store.get("attempts", attempt["id"]),
                           sr.output)
        return 1

    def _transport_retry(self, job, attempt, why: str, retry_after: Optional[float]) -> int:
        self.store.update_attempt(attempt["id"], provider_state="failed", error=f"transport: {why}")
        self.budget.release(attempt["reservation_id"], "not accepted by provider")
        n = job["transport_retries"] + 1
        if n > MAX_TRANSPORT_RETRIES:
            self._set_job(job, state="failed", provider_state="failed", lease_until=0,
                          error=f"gave up after {MAX_TRANSPORT_RETRIES} transport retries: {why}")
        else:
            delay = retry_after if retry_after is not None else min(60.0, 2.0 ** n)
            self._set_job(job, state="ready", provider_state="none", transport_retries=n, lease_until=0,
                          next_run_at=now() + delay, error=f"retrying in {delay:.1f}s: {why}")
        return 1

    def _fail_after_submit(self, job, project, attempt, why: str) -> None:
        self.store.update_attempt(attempt["id"], provider_state="failed", completed_at=now(), error=why)
        if attempt["reservation_id"]:
            self.budget.commit(attempt["reservation_id"], None, "failed after submission (conservative: may be billed)")
        self._after_failure(self.store.get("jobs", job["id"]), f"generation failed: {why}")

    def _after_failure(self, job, why: str) -> None:
        gen = self._generate_attempts(job)
        if len(gen) < job["max_attempts"]:
            self._set_job(job, state="ready", provider_state="failed", error=why, lease_until=0)
        else:
            self._exhausted(job, why)

    def _exhausted(self, job, why: str) -> None:
        best = self._best_candidate(job)
        if best:
            self._set_job(job, state="needs_review", selected_attempt=best["id"], verdict=best["verdict"],
                          verdict_source="judge", lease_until=0,
                          error=f"{why}; best candidate kept as a DRAFT ({best['verdict']})")
        else:
            self._set_job(job, state="failed", provider_state="failed", lease_until=0, error=why)

    # ── completion + quality ───────────────────────────────────────────────
    def _complete(self, job, project, attempt, out) -> None:
        prov = self.provider_for(project, job["kind"])
        meta = dict(out.meta or {})
        if job["kind"] in ("video", "tts"):
            meta.update(avtools.probe(out.path))
        row = self.media.put(out.path, job["kind"], meta, fixture=prov.is_fixture, move=True)
        usd, basis = prov.actual_cost(attempt["request"]["params"], out.usage or {})
        self.store.update_attempt(attempt["id"], provider_state="completed", completed_at=now(), usage=out.usage or {},
                                  media_sha=row["sha"], media_meta=meta, cost_actual_usd=usd)
        if attempt["reservation_id"]:
            self.budget.commit(attempt["reservation_id"], usd, basis)
        self._set_job(job, provider_state="completed")
        attempt = self.store.get("attempts", attempt["id"])
        verdict, review = self._quality(job, project, attempt)
        self.store.update_attempt(attempt["id"], verdict=verdict, review=review)
        job = self.store.get("jobs", job["id"])
        if verdict in SATISFIED:
            self._set_job(job, state="done", selected_attempt=attempt["id"], verdict=verdict,
                          verdict_source=(review or {}).get("source", "judge"), error=None, lease_until=0)
        elif verdict == "review_error":
            self._set_job(job, state="needs_review", selected_attempt=attempt["id"], verdict="review_error",
                          verdict_source="judge", lease_until=0,
                          error=f"review failed ({(review or {}).get('error')}); kept as a DRAFT — re-review or accept")
        else:  # rejected
            gen = self._generate_attempts(job)
            if len(gen) < job["max_attempts"]:
                self._set_job(job, state="ready", verdict="rejected", lease_until=0,
                              error="rejected by review; retrying with the judge's revised prompt")
            else:
                self._exhausted(job, "rejected by review on every attempt")

    def _quality(self, job, project, attempt) -> Tuple[str, Dict[str, Any]]:
        path = self.media.path(attempt["media_sha"])
        if job["kind"] == "tts":
            info = attempt["media_meta"] or {}
            lv = avtools.audio_levels(path)
            ok = info.get("duration", 0) > 0.2 and (lv["mean_db"] is None or lv["mean_db"] > -60)
            return ("accepted" if ok else "rejected"), {"source": "auto_check", "duration": info.get("duration"),
                                                        "levels": lv, "checks": "duration > 0.2 s, not silent"}
        if not project["settings"].get("review", True):
            return "unreviewed", {"source": "none", "note": "review disabled in project settings"}
        return self._review(job, project, attempt)

    def _review(self, job, project, attempt) -> Tuple[str, Dict[str, Any]]:
        judge = self.provider_for(project, "judge")
        media_path = self.media.path(attempt["media_sha"])
        work = self.media.scratch(f"review/{attempt['id']}")
        if job["kind"] == "video":
            from ..adapters.media_tools import sample_frames
            frames = [str(p) for p in sample_frames(media_path, work / "f", 4)]
            if not frames:
                return "review_error", {"source": "judge", "error": "could not sample frames"}
            media, media_kind = frames, "clip"
        else:
            media, media_kind = [str(media_path)], "keyframe"
        ep = None if job["key"].startswith("ref:") else self.store.get("episodes", job["episode_id"])
        references = self._review_references(job, ep)
        shot = None
        if ep and job["shot_id"]:
            bp = load_blueprint(ep)
            shot = next(s for s in bp.shots if s.shot_id == job["shot_id"]).model_dump()
        else:
            from ..schemas.blueprint import NarrativeLayer, ShotBlueprint
            shot = ShotBlueprint(shot_id=job["key"], narrative_layer=NarrativeLayer(
                narrative_action=attempt["request"]["params"].get("prompt", ""), emotional_beat="reference")).model_dump()
        prompt = attempt["request"]["params"].get("prompt", "")
        req = {"media": media, "references": references, "prompt": prompt, "shot": shot, "media_kind": media_kind,
               "global_style": project["settings"].get("global_style", "")}
        est = judge.estimate_cost(req)
        rid = new_id("att")
        try:
            res = self.budget.reserve(project["id"], est, "judge", job["id"], rid, note=f"review {job['key']}")
        except (BudgetBlocked, BudgetExceeded) as e:
            return "review_error", {"source": "judge", "error": f"budget: {e}"}
        material = {**req, "media": [self._sha_of(m) for m in media],
                    "references": [{"label": r["label"], "sha": r["sha"]} for r in references]}
        self.store.insert("attempts", {
            "id": rid, "job_id": job["id"], "n": attempt["n"], "purpose": "review", "provider": judge.name,
            "model": judge.model, "request": {"params": _public(material), "reviews_attempt": attempt["id"]},
            "request_hash": hash_obj(_public(material)), "input_hash": None,
            "reference_hashes": {r["label"]: r["sha"] for r in references}, "seed": None, "idempotency_key": rid,
            "remote_id": None, "provider_state": "submitting", "submitted_at": now(), "completed_at": None,
            "last_polled_at": None, "verdict": "pending", "review": None, "error": None, "usage": None,
            "cost_estimate_usd": est, "cost_actual_usd": None, "reservation_id": res, "media_sha": None,
            "media_meta": None, "created_at": now()})
        last_err = None
        for _ in range(2):  # one bounded retry for a transient judge failure
            try:
                sr = judge.submit(req, rid, work)
                review = dict(sr.output.meta["review"])
                usd, basis = judge.actual_cost(req, sr.output.usage or {})
                self.budget.commit(res, usd, basis)
                verdict = "accepted" if review.get("accepted") else "rejected"
                review["source"] = "judge"
                review["references_sent"] = [r["label"] for r in references]
                self.store.update_attempt(rid, provider_state="completed", completed_at=now(), verdict=verdict,
                                          review=review, cost_actual_usd=usd, remote_id=sr.remote_id)
                return verdict, review
            except ProviderError as e:
                last_err = str(e)
                if not e.retryable:
                    break
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                break
        self.budget.commit(res, None, "review call failed (conservative)")
        self.store.update_attempt(rid, provider_state="failed", completed_at=now(), verdict="review_error",
                                  error=last_err)
        return "review_error", {"source": "judge", "error": last_err}

    def _review_references(self, job, ep) -> List[Dict[str, Any]]:
        """Canonical sheets of the shot's assets + continuity frames, as labelled images."""
        refs: List[Dict[str, Any]] = []
        if ep is None:
            return refs
        spec = job["spec"]
        kf_job = None
        if job["kind"] == "video":
            kf_job = resolve_dep(self.store, ep, f"kf:{job['shot_id']}")
            spec = kf_job["spec"] if kf_job else spec
        for aid in spec.get("references", []):
            sha = self._asset_media(ep, aid)
            if sha:
                refs.append({"label": f"canonical:{aid}", "sha": sha, "path": str(self.media.path(sha))})
        if kf_job and kf_job["selected_attempt"]:
            a = self.store.get("attempts", kf_job["selected_attempt"])
            refs.append({"label": "approved_first_frame", "sha": a["media_sha"], "path": str(self.media.path(a["media_sha"]))})
        bp = load_blueprint(ep)
        ids = [s.shot_id for s in bp.shots]
        i = ids.index(job["shot_id"]) if job["shot_id"] in ids else -1
        if i > 0:
            prev = resolve_dep(self.store, ep, f"kf:{ids[i - 1]}")
            if prev and prev["selected_attempt"] and prev["verdict"] == "accepted":
                a = self.store.get("attempts", prev["selected_attempt"])
                refs.append({"label": f"previous_shot:{ids[i - 1]}", "sha": a["media_sha"],
                             "path": str(self.media.path(a["media_sha"]))})
        return refs

    # ── requests ───────────────────────────────────────────────────────────
    def _build_request(self, job, project, ep, attempt_no: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """(request with file paths, material with content hashes instead of paths)."""
        spec = job["spec"]
        material: Dict[str, Any] = {}
        if job["kind"] == "image":
            prompt, seed = spec["prompt"], spec.get("seed")
            if attempt_no:
                prompt = self._revised_prompt(job) or prompt
                seed = None if seed is None else seed + attempt_no
            refs = []
            for aid in spec.get("references", []):
                sha = self._asset_media(ep, aid)
                if sha:
                    refs.append({"label": aid, "sha": sha, "path": str(self.media.path(sha))})
            req = {"prompt": prompt, "negative_prompt": spec.get("negative_prompt"), "size": spec["size"],
                   "seed": seed, "reference_images": refs, "label": spec.get("label")}
            material = {**req, "reference_images": [{"label": r["label"], "sha": r["sha"]} for r in refs]}
            return req, material
        if job["kind"] == "tts":
            req = {"text": spec["text"], "voice": spec["voice"], "instructions": spec.get("instructions"),
                   "format": "wav", "language": spec.get("language")}
            return req, dict(req)
        if job["kind"] == "video":
            kf = resolve_dep(self.store, ep, f"kf:{job['shot_id']}")
            kf_att = self.store.get("attempts", kf["selected_attempt"]) if kf and kf["selected_attempt"] else None
            speech = self.speech_durations(ep, job["shot_id"])
            slot = shot_slot(spec["planned_duration"], speech)
            prompt, seed = spec["prompt"], spec.get("seed")
            if attempt_no:
                prompt = self._revised_prompt(job) or prompt
                seed = None if seed is None else seed + attempt_no
            req = {"prompt": prompt, "negative_prompt": spec.get("negative_prompt"), "duration": slot,
                   "size": spec["size"], "seed": seed, "label": spec.get("label"),
                   "image": str(self.media.path(kf_att["media_sha"])) if kf_att else None}
            material = {**req, "image": None, "image_sha": kf_att["media_sha"] if kf_att else None,
                        "speech": speech}
            return req, material
        raise ValueError(job["kind"])

    def speech_durations(self, ep, shot_id: str) -> Dict[str, float]:
        out = {}
        for key in (f"narr:{shot_id}", f"tts:{shot_id}"):
            j = resolve_dep(self.store, ep, key)
            if j and j["selected_attempt"] and j["state"] != "obsolete":
                a = self.store.get("attempts", j["selected_attempt"])
                out[key.split(":")[0]] = round(float((a["media_meta"] or {}).get("duration", 0)), 3)
        return out

    def _input_hash(self, job, project, material) -> str:
        prov = self.provider_for(project, job["kind"])
        return hash_obj({"kind": job["kind"], "provider": prov.name, "model": prov.model, "material": material})

    def _revised_prompt(self, job) -> Optional[str]:
        for a in reversed(self._generate_attempts(job)):
            if a["verdict"] == "rejected" and (a["review"] or {}).get("revised_prompt"):
                return a["review"]["revised_prompt"]
        return None

    def _asset_media(self, ep, asset_id: str) -> Optional[str]:
        asset = self.store.one("SELECT * FROM assets WHERE project_id = ? AND json_extract(data, '$.asset_id') = ?",
                               (ep["project_id"], asset_id))
        if asset and asset["canonical_media"]:
            return asset["canonical_media"]
        ref = self.store.one("SELECT * FROM jobs WHERE episode_id = ? AND key = ?", (ep["project_id"], f"ref:{asset_id}"))
        if ref and ref["selected_attempt"] and ref["verdict"] in SATISFIED:
            return self.store.get("attempts", ref["selected_attempt"])["media_sha"]
        return None

    def _cached_attempt(self, input_hash: str, project) -> Optional[Dict[str, Any]]:
        ok = ("accepted",) if project["settings"].get("review", True) else SATISFIED
        for a in self.store.q("SELECT * FROM attempts WHERE input_hash = ? AND purpose = 'generate' AND "
                              "provider_state = 'completed' ORDER BY created_at DESC", (input_hash,)):
            if a["verdict"] in ok and self.media.path(a["media_sha"]) and self.media.path(a["media_sha"]).exists():
                return a
        return None

    def _generate_attempts(self, job) -> List[Dict[str, Any]]:
        """
        Attempts that count against the job's attempt budget for its CURRENT spec: attempts made before
        a revision (job.epoch) and transport/contract rejections the provider never accepted do not count.
        """
        rows = self.store.q("SELECT * FROM attempts WHERE job_id = ? AND purpose = 'generate' AND created_at >= ? "
                            "ORDER BY created_at", (job["id"], job.get("epoch") or 0))
        skip = ("transport:", "unsupported_parameter:", "not_configured:")
        return [a for a in rows if not (a["error"] or "").startswith(skip)]

    def _attempt_number(self, job) -> int:
        return self.store.one("SELECT COUNT(*) AS n FROM attempts WHERE job_id = ? AND purpose = 'generate'",
                              (job["id"],))["n"] + 1

    def _best_candidate(self, job) -> Optional[Dict[str, Any]]:
        rank = {"accepted": 4, "unreviewed": 3, "rejected": 2, "review_error": 1}
        c = [a for a in self._generate_attempts(job) if a["media_sha"]]
        if not c:
            return None
        return max(c, key=lambda a: (rank.get(a["verdict"], 0), (a["review"] or {}).get("score") or 0, a["n"]))

    def _sha_of(self, path: str) -> str:
        from .hashing import hash_file
        return hash_file(Path(path))

    # ── cancellation ───────────────────────────────────────────────────────
    def _cancel_attempt(self, job, project, attempt) -> None:
        prov = self.provider_for(project, job["kind"])
        remote_cancelled = bool(attempt["remote_id"]) and prov.caps.cancellation and prov.cancel(attempt["remote_id"])
        note = "cancelled remotely" if remote_cancelled else \
            "cancelled locally; the provider has no cancel endpoint — the remote job may still finish and bill"
        self.store.update_attempt(attempt["id"], provider_state="cancelled", completed_at=now(), error=note)
        if attempt["reservation_id"]:
            self.budget.commit(attempt["reservation_id"], None, "cancelled after submission (conservative)")
        self._set_job(job, state="cancelled", provider_state="cancelled", error=note, lease_until=0)

    # ── human / API actions ────────────────────────────────────────────────
    def unblock(self, project_id: str) -> int:
        """After a budget / provider-settings change: blocked jobs get re-evaluated."""
        eps = [e["id"] for e in self.store.q("SELECT id FROM episodes WHERE project_id = ?", (project_id,))] + [project_id]
        n = 0
        for eid in eps:
            n += self.store.conn().execute("UPDATE jobs SET state = 'pending', error = NULL WHERE episode_id = ? AND "
                                           "state = 'blocked'", (eid,)).rowcount
        return n

    def cancel(self, job_id: str) -> Dict[str, Any]:
        job = self.store.get("jobs", job_id)
        inflight = self.store.one("SELECT * FROM attempts WHERE job_id = ? AND provider_state IN "
                                  "('submitted','running','reserved') AND purpose = 'generate'", (job_id,))
        if inflight:
            self._cancel_attempt(job, self._project_of(job), inflight)
        else:
            self._set_job(job, state="cancelled", error="cancelled before submission", lease_until=0)
        return self.store.get("jobs", job_id)

    def retry(self, job_id: str) -> Dict[str, Any]:
        """A fresh attempt even if a cached/accepted one exists (paid in live mode)."""
        job = self.store.get("jobs", job_id)
        used = len(self._generate_attempts(job))
        self._set_job(job, state="pending", force_new=1, cancel_requested=0, transport_retries=0,
                      max_attempts=max(job["max_attempts"], used + 1), error="retry requested", next_run_at=0)
        return self.store.get("jobs", job_id)

    def set_verdict(self, job_id: str, verdict: str, note: str = "") -> Dict[str, Any]:
        """Human accept / reject of the job's selected candidate. The user is the creative authority."""
        if verdict not in ("accepted", "rejected"):
            raise ValueError("verdict must be accepted or rejected")
        job = self.store.get("jobs", job_id)
        if not job["selected_attempt"]:
            raise ValueError("no candidate to review yet")
        a = self.store.get("attempts", job["selected_attempt"])
        review = dict(a["review"] or {})
        review["human"] = {"verdict": verdict, "note": note, "at": now(), "previous": a["verdict"]}
        self.store.update_attempt(a["id"], verdict=verdict, review=review)
        if verdict == "accepted":
            self._set_job(job, state="done", verdict="accepted", verdict_source="human", error=None)
        else:
            used = len(self._generate_attempts(job))
            self._set_job(job, state="pending", verdict="rejected", verdict_source="human", force_new=1,
                          max_attempts=max(job["max_attempts"], used + 1), error=f"rejected by director: {note}")
        return self.store.get("jobs", job_id)

    def select_attempt(self, job_id: str, attempt_id: str) -> Dict[str, Any]:
        """Compare versions / roll back one job: choose any earlier candidate that has media."""
        a = self.store.get("attempts", attempt_id)
        if not a or a["job_id"] != job_id or not a["media_sha"]:
            raise ValueError("attempt has no media for this job")
        job = self.store.get("jobs", job_id)
        self._set_job(job, state="done" if a["verdict"] in SATISFIED else "needs_review", selected_attempt=a["id"],
                      verdict=a["verdict"], verdict_source="human_selection",
                      input_hash=a["input_hash"] or job["input_hash"], error=None)
        return self.store.get("jobs", job_id)

    def rereview(self, job_id: str) -> Dict[str, Any]:
        job = self.store.get("jobs", job_id)
        a = self.store.get("attempts", job["selected_attempt"])
        project = self._project_of(job)
        verdict, review = self._review(job, project, a)
        self.store.update_attempt(a["id"], verdict=verdict, review=review)
        self._set_job(job, state="done" if verdict in SATISFIED else "needs_review", verdict=verdict,
                      verdict_source="judge", error=None if verdict in SATISFIED else f"re-review: {verdict}")
        return self.store.get("jobs", job_id)

    def resolve_unknown(self, job_id: str, action: str, remote_id: Optional[str] = None) -> Dict[str, Any]:
        """For an ambiguous submission: attach a remote id found in the provider console, or resubmit (pays again)."""
        job = self.store.get("jobs", job_id)
        a = self.store.one("SELECT * FROM attempts WHERE job_id = ? AND provider_state IN ('unknown','timed_out') "
                           "ORDER BY created_at DESC", (job_id,))
        if not a:
            raise ValueError("no unknown/timed-out attempt on this job")
        if action == "reconcile":
            rid = remote_id or a["remote_id"]
            if not rid:
                raise ValueError("reconcile needs the remote id from the provider console")
            self.store.update_attempt(a["id"], remote_id=rid, provider_state="submitted",
                                      submitted_at=a["submitted_at"] or now())
            self._set_job(job, state="running", provider_state="submitted", error="reconciling with provider")
        elif action == "resubmit":
            self.store.update_attempt(a["id"], provider_state="failed", error="abandoned by director (resubmitted)")
            used = len(self._generate_attempts(job))
            self._set_job(job, state="pending", force_new=1, max_attempts=max(job["max_attempts"], used + 1),
                          error="resubmission approved by director")
        else:
            raise ValueError("action must be reconcile or resubmit")
        return self.store.get("jobs", job_id)

    # ── readiness ──────────────────────────────────────────────────────────
    def readiness(self, episode_id: str) -> Dict[str, Any]:
        from .domain import jobs_for_episode
        ep = self.store.get("episodes", episode_id)
        jobs = [j for j in jobs_for_episode(self.store, episode_id) if j["mandatory"]]
        rows, missing, not_accepted = [], [], []
        for j in jobs:
            has_media = bool(j["selected_attempt"])
            accepted = j["verdict"] == "accepted" and j["state"] == "done"
            if j["key"].startswith("ref:"):
                continue  # references gate keyframes; a film's readiness is its shots and lines
            rows.append({"key": j["key"], "state": j["state"], "verdict": j["verdict"], "source": j["verdict_source"],
                         "has_media": has_media, "error": j["error"]})
            if not has_media:
                missing.append(j["key"])
            elif not accepted:
                not_accepted.append(j["key"])
        fixture = bool(self.store.one(
            "SELECT 1 AS x FROM jobs j JOIN attempts a ON a.id = j.selected_attempt JOIN media m ON m.sha = a.media_sha "
            "WHERE j.episode_id = ? AND m.fixture = 1 LIMIT 1", (episode_id,)))
        if missing:
            status = "blocked"
            reason = f"{len(missing)} mandatory item(s) have no media: {', '.join(missing)}"
        elif not_accepted:
            status = "draft"
            reason = f"{len(not_accepted)} item(s) not accepted: {', '.join(not_accepted)}"
        else:
            status, reason = "final", "every shot and line rendered and accepted"
        return {"episode_id": episode_id, "status": status, "reason": reason, "missing": missing,
                "not_accepted": not_accepted, "items": rows, "contains_fixture_media": fixture,
                "storyboard_approved": bool(ep["storyboard_approved"])}

    # ── helpers ────────────────────────────────────────────────────────────
    def _project_of(self, job) -> Dict[str, Any]:
        p = self.store.get("projects", job["episode_id"])
        if p:
            return p
        ep = self.store.get("episodes", job["episode_id"])
        return self.store.get("projects", ep["project_id"])

    def _set_job(self, job, **fields) -> None:
        fields["updated_at"] = now()
        if "state" in fields and fields["state"] != job.get("state"):
            lvl = "error" if fields["state"] in ("failed",) else "warn" if fields["state"] in (
                "needs_review", "blocked") else "info"
            msg = f"{job['key']}: {job.get('state')} -> {fields['state']}" + (
                f" ({fields.get('error')})" if fields.get("error") else "")
            self.store.event(lvl, msg, job_id=job["id"], episode_id=job["episode_id"])
        self.store.update("jobs", "id", job["id"], fields)
        job.update(fields)


def shot_slot(planned: float, speech: Dict[str, float]) -> float:
    """Shot length follows measured speech: narration, gap, line, plus lead/tail air; never shorter than planned."""
    spoken = sum(speech.values()) + (SPEECH_GAP_S if len(speech) > 1 else 0.0)
    need = SPEECH_LEAD_S + spoken + SPEECH_TAIL_S if spoken else 0.0
    return round(max(float(planned), need), 3)


def _public(params: Dict[str, Any]) -> Dict[str, Any]:
    """Request as recorded: local paths of inputs are replaced by their content hashes."""
    out = {}
    for k, v in params.items():
        if k == "reference_images":
            out[k] = [{"label": r["label"], "sha": r["sha"]} for r in v]
        elif k == "image" and isinstance(v, str):
            continue
        else:
            out[k] = v
    if "image" in params and isinstance(params["image"], str):
        from .hashing import hash_file
        out["image_sha"] = hash_file(Path(params["image"]))
    return out


def _materialize(public: Dict[str, Any], media: MediaStore) -> Dict[str, Any]:
    """Inverse of _public for submission: hashes back to local paths."""
    out = dict(public)
    if out.get("reference_images"):
        out["reference_images"] = [{**r, "path": str(media.path(r["sha"]))} for r in out["reference_images"]]
    if out.get("image_sha"):
        out["image"] = str(media.path(out.pop("image_sha")))
    return out
