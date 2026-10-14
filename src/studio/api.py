#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
HTTP API for the director workspace (FastAPI). All state is durable (SQLite); the UI only reflects it.

The worker runs in a background thread when `start_worker=True` (one-command startup) or as a separate
process (`python -m src.studio worker`). Both are safe together: jobs are claimed with a DB lease.
"""

import base64
import json
import mimetypes
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import Studio, domain, revisions
from .budget import Budget
from .export import ExportRefused
from .store import now

UI_DIST = Path(__file__).resolve().parents[2] / "studio-ui" / "dist"


class NewProject(BaseModel):
    template_id: Optional[str] = "hanoi_delivery"
    name: Optional[str] = None
    mode: str = "offline_fixture"
    budget_limit_usd: float = 0.0
    kind: str = "series"


class SettingsPatch(BaseModel):
    budget_limit_usd: Optional[float] = None
    mode: Optional[str] = None
    confirm_live: bool = False
    settings: Optional[Dict[str, Any]] = None


class NewEpisode(BaseModel):
    title: str
    shots: Optional[List[Dict[str, Any]]] = None     # structured shots (template format)
    script: Optional[str] = None                      # live planning with the crew (LLM)
    from_template_episode: Optional[int] = None


class EditText(BaseModel):
    text: Optional[str] = None
    patch: Optional[Dict[str, Any]] = None
    confirm: bool = False


class Verdict(BaseModel):
    verdict: str
    note: str = ""


class Lock(BaseModel):
    locked: bool
    what: str = "shot"        # shot | keyframe | voice


class ExportReq(BaseModel):
    aspect: Optional[str] = None
    burn_subtitles: Optional[bool] = None
    allow_draft: bool = False
    fit: str = "pad"


class Upload(BaseModel):
    filename: str
    data_b64: str


class NewAsset(BaseModel):
    kind: str
    name: str
    asset_id: str
    identity: str = ""
    wardrobe: Optional[Dict[str, str]] = None
    voice: Optional[Dict[str, str]] = None


class Resolve(BaseModel):
    action: str
    remote_id: Optional[str] = None


def create_app(studio: Optional[Studio] = None, start_worker: bool = True, serve_ui: bool = True) -> FastAPI:
    studio = studio or Studio()
    app = FastAPI(title="CineCrew Studio", version="0.1")
    app.state.studio = studio
    stop = threading.Event()
    store, engine = studio.store, studio.engine

    if start_worker:
        @app.on_event("startup")
        def _start():
            t = threading.Thread(target=engine.serve_forever, args=(stop,), daemon=True, name="studio-worker")
            t.start()

        @app.on_event("shutdown")
        def _stop():
            stop.set()

    def _project(pid: str) -> Dict[str, Any]:
        p = store.get("projects", pid)
        if not p:
            raise HTTPException(404, "project not found")
        return p

    def _episode(eid: str) -> Dict[str, Any]:
        e = store.get("episodes", eid)
        if not e:
            raise HTTPException(404, "episode not found")
        return e

    def _job(jid: str) -> Dict[str, Any]:
        j = store.get("jobs", jid)
        if not j:
            raise HTTPException(404, "job not found")
        return j

    def _err(e: Exception, code: int = 400):
        raise HTTPException(code, str(e))

    # ── health / providers ────────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        import shutil
        return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg")), "home": str(studio.home), "time": now()}

    @app.get("/api/projects/{pid}/providers")
    def providers(pid: str):
        p = _project(pid)
        out = {}
        for kind in ("image", "video", "tts", "judge"):
            try:
                out[kind] = engine.provider_for(p, kind).describe()
            except Exception as e:  # noqa: BLE001
                out[kind] = {"error": str(e)}
        return out

    # ── projects ──────────────────────────────────────────────────────────
    @app.get("/api/projects")
    def list_projects():
        rows = store.q("SELECT * FROM projects ORDER BY created_at DESC")
        b = Budget(store)
        return [dict(r, budget=b.summary(r["id"]),
                     episodes=store.one("SELECT COUNT(*) AS n FROM episodes WHERE project_id = ?", (r["id"],))["n"])
                for r in rows]

    @app.get("/api/templates")
    def templates():
        out = []
        for p in sorted(domain.TEMPLATES.glob("*.json")):
            t = json.loads(p.read_text(encoding="utf-8"))
            out.append({"template_id": t["template_id"], "title": t["series_title"], "kind": t.get("kind"),
                        "aspect": t.get("aspect"), "episodes": [{"number": e["number"], "title": e["title"],
                                                                 "shots": len(e["shots"])} for e in t["episodes"]]})
        return out

    @app.post("/api/projects")
    def create_project(req: NewProject):
        if req.mode == "live" and req.budget_limit_usd <= 0:
            raise HTTPException(400, "live mode needs a budget limit above 0 USD")
        try:
            if req.template_id:
                p = studio.create_series(req.template_id, req.mode, req.budget_limit_usd, req.name)
            else:
                p = domain.create_project(store, req.name or "Untitled", req.kind, req.mode, req.budget_limit_usd)
        except Exception as e:  # noqa: BLE001
            _err(e)
        return store.get("projects", p["id"])

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        p = _project(pid)
        assets = store.q("SELECT * FROM assets WHERE project_id = ? ORDER BY kind, name", (pid,))
        ref_jobs = {j["key"]: j for j in store.q("SELECT * FROM jobs WHERE episode_id = ?", (pid,))}
        for a in assets:
            j = ref_jobs.get(f"ref:{a['data']['asset_id']}")
            a["reference_job"] = _job_view(j) if j else None
            sha = a["canonical_media"] or (j and j["selected_attempt"] and
                                           store.get("attempts", j["selected_attempt"])["media_sha"])
            a["preview_sha"] = sha or None
        eps = store.q("SELECT id, number, title, version, storyboard_approved, created_at FROM episodes "
                      "WHERE project_id = ? ORDER BY number", (pid,))
        for e in eps:
            e["readiness"] = engine.readiness(e["id"])["status"]
        return {**p, "assets": assets, "episodes": eps, "budget": Budget(store).summary(pid)}

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, req: SettingsPatch):
        p = _project(pid)
        fields: Dict[str, Any] = {}
        if req.mode and req.mode != p["mode"]:
            if req.mode == "live" and not req.confirm_live:
                raise HTTPException(400, "switching to live mode enables paid calls; confirm_live is required")
            fields["mode"] = req.mode
        if req.budget_limit_usd is not None:
            if req.budget_limit_usd < Budget(store).summary(pid)["committed_usd"]:
                raise HTTPException(400, "limit below what is already committed")
            fields["budget_limit_usd"] = req.budget_limit_usd
        if req.settings:
            s = dict(p["settings"])
            domain._deep_update(s, req.settings)
            fields["settings"] = s
        store.update("projects", "id", pid, fields)
        engine.unblock(pid)
        for e in store.q("SELECT id FROM episodes WHERE project_id = ?", (pid,)):
            domain.sync_jobs(store, e["id"])
        return get_project(pid)

    @app.get("/api/projects/{pid}/ledger")
    def ledger(pid: str):
        _project(pid)
        return {"summary": Budget(store).summary(pid),
                "entries": store.q("SELECT * FROM ledger WHERE project_id = ? ORDER BY created_at DESC LIMIT 500", (pid,))}

    @app.get("/api/projects/{pid}/events")
    def events(pid: str, after: int = 0):
        eps = [e["id"] for e in store.q("SELECT id FROM episodes WHERE project_id = ?", (pid,))]
        q = ",".join("?" * (len(eps) + 1))
        return store.q(f"SELECT * FROM events WHERE id > ? AND (project_id = ? OR episode_id IN ({q}) OR job_id IN "
                       f"(SELECT id FROM jobs WHERE episode_id IN ({q}))) ORDER BY id DESC LIMIT 200",
                       (after, pid, *eps, pid, *eps, pid))

    # ── assets ────────────────────────────────────────────────────────────
    @app.post("/api/projects/{pid}/assets")
    def add_asset(pid: str, req: NewAsset):
        _project(pid)
        data = {"asset_id": req.asset_id, "identity": req.identity}
        if req.wardrobe:
            data["wardrobe"] = req.wardrobe
        if req.voice:
            data["voice"] = req.voice
        try:
            a = domain.add_asset(store, pid, req.kind, req.name, data)
        except Exception as e:  # noqa: BLE001
            _err(e)
        for e in store.q("SELECT id FROM episodes WHERE project_id = ?", (pid,)):
            domain.sync_jobs(store, e["id"])
        return a

    @app.post("/api/assets/{aid}/media")
    def upload_asset_media(aid: str, req: Upload):
        a = store.get("assets", aid)
        if not a:
            raise HTTPException(404, "asset not found")
        if a["locked"]:
            raise HTTPException(409, "asset is locked")
        suffix = Path(req.filename).suffix.lower() or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(base64.b64decode(req.data_b64))
        row = studio.media.put(Path(f.name), a["kind"], {"filename": req.filename, "supplied": True}, move=True)
        store.update("assets", "id", aid, {"canonical_media": row["sha"], "version": a["version"] + 1,
                                           "updated_at": now()})
        for e in store.q("SELECT id FROM episodes WHERE project_id = ?", (a["project_id"],)):
            domain.sync_jobs(store, e["id"])
        return store.get("assets", aid)

    @app.post("/api/assets/{aid}/lock")
    def lock_asset(aid: str, req: Lock):
        a = store.get("assets", aid)
        if not a:
            raise HTTPException(404, "asset not found")
        store.update("assets", "id", aid, {"locked": int(req.locked)})
        for e in store.q("SELECT id FROM episodes WHERE project_id = ?", (a["project_id"],)):
            domain.sync_jobs(store, e["id"])
        return store.get("assets", aid)

    # ── episodes ──────────────────────────────────────────────────────────
    @app.post("/api/projects/{pid}/episodes")
    def new_episode(pid: str, req: NewEpisode):
        p = _project(pid)
        try:
            if req.from_template_episode is not None:
                t = domain.load_template(p["settings"].get("template_id", "hanoi_delivery"))
                spec = next(e for e in t["episodes"] if e["number"] == req.from_template_episode)
                spec = dict(spec, title=req.title or spec["title"], number=None)
                return domain.create_episode_from_spec(store, pid, spec)
            if req.shots:
                return domain.create_episode_from_spec(store, pid, {"title": req.title, "shots": req.shots})
            if req.script:
                from .planning import plan_episode_live
                return plan_episode_live(studio, pid, req.title, req.script)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            _err(e)
        raise HTTPException(400, "give shots, a script (live planning) or from_template_episode")

    @app.get("/api/episodes/{eid}")
    def get_episode(eid: str):
        ep = _episode(eid)
        bp = domain.load_blueprint(ep)
        jobs = {j["key"]: _job_view(j) for j in domain.jobs_for_episode(store, eid)}
        shots = []
        for i, s in enumerate(bp.shots):
            ss = ep["studio"]["shots"][s.shot_id]
            shots.append({
                "index": i + 1, "shot_id": s.shot_id, "action": s.narrative_layer.narrative_action,
                "beat": s.narrative_layer.emotional_beat,
                "dialogue": s.narrative_layer.dialogue.text, "speaker": s.narrative_layer.dialogue.speaker_asset_id,
                "duration": s.staging_layer.duration_seconds, "lighting": s.staging_layer.lighting,
                "camera": s.staging_layer.camera.model_dump(), "location": s.staging_layer.environment_id,
                "wardrobe": ss.get("wardrobe"), "props": ss.get("props"), "start_state": ss.get("start_state"),
                "end_state": ss.get("end_state"), "narration": ss.get("narration"), "locked": ss.get("locked", False),
                "keyframe_locked": ss.get("keyframe_locked", False), "voice_locked": ss.get("voice_locked", False),
                "jobs": {k.split(":")[0]: jobs[k] for k in (f"kf:{s.shot_id}", f"clip:{s.shot_id}",
                                                            f"tts:{s.shot_id}", f"narr:{s.shot_id}") if k in jobs},
            })
        exports = store.q("SELECT id, kind, status, files, created_at FROM exports WHERE episode_id = ? "
                          "ORDER BY created_at DESC", (eid,))
        return {"id": ep["id"], "project_id": ep["project_id"], "number": ep["number"], "title": ep["title"],
                "version": ep["version"], "storyboard_approved": bool(ep["storyboard_approved"]),
                "planning": ep["planning"], "shots": shots, "readiness": engine.readiness(eid),
                "exports": exports, "references": [jobs[k] for k in jobs if k.startswith("ref:")],
                "counts": _counts(jobs.values())}

    @app.post("/api/episodes/{eid}/approve-storyboard")
    def approve(eid: str):
        _episode(eid)
        try:
            studio.approve_storyboard(eid)
        except ValueError as e:
            _err(e, 409)
        return get_episode(eid)

    @app.post("/api/episodes/{eid}/shots/{sid}/lock")
    def lock_shot(eid: str, sid: str, req: Lock):
        ep = _episode(eid)
        sc = ep["studio"]
        if sid not in sc["shots"]:
            raise HTTPException(404, "shot not found")
        sc["shots"][sid][{"shot": "locked", "keyframe": "keyframe_locked", "voice": "voice_locked"}[req.what]] = req.locked
        store.update("episodes", "id", eid, {"studio": sc})
        domain.sync_jobs(store, eid)
        return get_episode(eid)

    @app.post("/api/episodes/{eid}/edits/preview")
    def edit_preview(eid: str, req: EditText):
        return _resolve_and(eid, req, revisions.preview)

    @app.post("/api/episodes/{eid}/edits/apply")
    def edit_apply(eid: str, req: EditText):
        try:
            return _resolve_and(eid, req, lambda s, e, p: revisions.apply(s, e, p, confirm=req.confirm))
        except revisions.RevisionError as e:
            _err(e, 409)

    def _resolve_and(eid: str, req: EditText, fn):
        ep = _episode(eid)
        if req.patch:
            patch = revisions.EditPatch.model_validate(req.patch)
        elif req.text:
            bp = domain.load_blueprint(ep)
            assets = domain.assets_by_asset_id(store, ep["project_id"])
            patch = revisions.resolve_edit(req.text, bp, assets)
        else:
            raise HTTPException(400, "text or patch required")
        try:
            return fn(studio, eid, patch)
        except revisions.RevisionError:
            raise
        except Exception as e:  # noqa: BLE001
            _err(e)

    @app.get("/api/episodes/{eid}/versions")
    def get_versions(eid: str):
        _episode(eid)
        return revisions.versions(store, eid)

    @app.post("/api/episodes/{eid}/rollback/{version}")
    def do_rollback(eid: str, version: int):
        _episode(eid)
        try:
            revisions.rollback(studio, eid, version)
        except revisions.RevisionError as e:
            _err(e, 404)
        return get_episode(eid)

    @app.post("/api/episodes/{eid}/export")
    def do_export(eid: str, req: ExportReq):
        _episode(eid)
        try:
            row = studio.export(eid, aspect=req.aspect, burn_subtitles=req.burn_subtitles, allow_draft=req.allow_draft,
                                fit=req.fit)
        except ExportRefused as e:
            return JSONResponse(status_code=409, content={"detail": str(e), "readiness": e.readiness})
        except Exception as e:  # noqa: BLE001
            _err(e, 500)
        return _export_view(row)

    @app.post("/api/episodes/{eid}/comic")
    def do_comic(eid: str):
        _episode(eid)
        from .comic import export_comic
        try:
            row = export_comic(studio, eid)
        except ExportRefused as e:
            return JSONResponse(status_code=409, content={"detail": str(e), "readiness": e.readiness})
        return _export_view(row)

    @app.get("/api/exports/{xid}")
    def get_export(xid: str):
        row = store.get("exports", xid)
        if not row:
            raise HTTPException(404, "export not found")
        return _export_view(row)

    @app.get("/api/exports/{xid}/file/{name}")
    def export_file(xid: str, name: str):
        row = store.get("exports", xid)
        if not row or name not in row["files"]:
            raise HTTPException(404, "file not found")
        p = Path(row["files"][name])
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                            filename=p.name)

    # ── jobs ──────────────────────────────────────────────────────────────
    @app.get("/api/jobs/{jid}")
    def get_job(jid: str):
        j = _job(jid)
        atts = store.q("SELECT * FROM attempts WHERE job_id = ? ORDER BY created_at", (jid,))
        return {**_job_view(j), "spec": j["spec"], "attempts": atts}

    @app.post("/api/jobs/{jid}/retry")
    def retry(jid: str):
        _job(jid)
        return _job_view(engine.retry(jid))

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid: str):
        _job(jid)
        store.update("jobs", "id", jid, {"cancel_requested": 1})
        return _job_view(engine.cancel(jid))

    @app.post("/api/jobs/{jid}/verdict")
    def verdict(jid: str, req: Verdict):
        _job(jid)
        try:
            return _job_view(engine.set_verdict(jid, req.verdict, req.note))
        except ValueError as e:
            _err(e, 409)

    @app.post("/api/jobs/{jid}/select/{att}")
    def select(jid: str, att: str):
        _job(jid)
        try:
            return _job_view(engine.select_attempt(jid, att))
        except ValueError as e:
            _err(e, 409)

    @app.post("/api/jobs/{jid}/rereview")
    def rereview(jid: str):
        _job(jid)
        return _job_view(engine.rereview(jid))

    @app.post("/api/jobs/{jid}/resolve")
    def resolve(jid: str, req: Resolve):
        _job(jid)
        try:
            return _job_view(engine.resolve_unknown(jid, req.action, req.remote_id))
        except ValueError as e:
            _err(e, 409)

    # ── media ─────────────────────────────────────────────────────────────
    @app.get("/media/{sha}")
    def media(sha: str):
        row = studio.media.get(sha)
        if not row or not Path(row["path"]).exists():
            raise HTTPException(404, "media not found")
        return FileResponse(row["path"], media_type=row["mime"] or "application/octet-stream")

    def _job_view(j: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not j:
            return None
        sel = store.get("attempts", j["selected_attempt"]) if j["selected_attempt"] else None
        n = store.one("SELECT COUNT(*) AS n FROM attempts WHERE job_id = ? AND purpose = 'generate'", (j["id"],))["n"]
        cost = store.one("SELECT COALESCE(SUM(amount_usd), 0) AS c FROM ledger WHERE job_id = ? AND state = 'committed'",
                         (j["id"],))["c"]
        return {"id": j["id"], "key": j["key"], "kind": j["kind"], "state": j["state"],
                "provider_state": j["provider_state"], "verdict": j["verdict"], "verdict_source": j["verdict_source"],
                "error": j["error"], "locked": bool(j["locked"]), "cache_hit": bool(j["cache_hit"]),
                "attempts": n, "max_attempts": j["max_attempts"], "cost_usd": round(cost, 6),
                "media_sha": sel["media_sha"] if sel else None,
                "media_meta": sel["media_meta"] if sel else None,
                "review": sel["review"] if sel else None,
                "fixture": bool(sel and studio.media.get(sel["media_sha"]) and
                                studio.media.get(sel["media_sha"])["fixture"])}

    def _counts(jobs) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for j in jobs:
            c[j["state"]] = c.get(j["state"], 0) + 1
        return c

    def _export_view(row: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": row["id"], "episode_id": row["episode_id"], "kind": row["kind"], "status": row["status"],
                "files": {k: f"/api/exports/{row['id']}/file/{k}" for k in row["files"]},
                "manifest": row["manifest"], "reused": row.get("reused", False), "created_at": row["created_at"]}

    if serve_ui and UI_DIST.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIST), html=True), name="ui")
    return app
