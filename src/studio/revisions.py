#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Directed revisions: natural language -> typed patch -> impact + cost preview -> apply / rollback.

  resolve_edit()  deterministic rules for common Vietnamese / English phrasings ("đổi ánh sáng cảnh 3
                  thành …", "change the lighting of shot 3 to …"); an LLM resolver (edit_resolver agent)
                  is used only in live mode with budget; anything else comes back `ambiguous` with a question.
  preview()       which jobs (in which episodes) the patch invalidates, what stays cached, the cost
                  estimate, lock conflicts, and whether confirmation is required (identity changes).
  apply()         writes a new episode version and re-syncs the job graph; unaffected approved media
                  keeps its hash and is never regenerated.
  rollback()      restores an earlier version as a new version; its media come back from the cache.
"""

import copy
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from . import domain
from .hashing import hash_obj
from .store import now

SHOT_FIELDS = ("lighting", "camera", "action", "dialogue", "duration", "start_state", "end_state", "narration",
               "wardrobe")
ASSET_FIELDS = ("identity", "wardrobe_default", "voice_instructions")


class EditOp(BaseModel):
    target: Literal["shot", "asset"]
    field: str = Field(..., description=f"shot: {SHOT_FIELDS}; asset: {ASSET_FIELDS}")
    value: Any = Field(..., description="New value (text; duration in seconds; wardrobe: {asset_id: state})")
    shot_id: Optional[str] = None
    asset_id: Optional[str] = None


class EditPatch(BaseModel):
    ops: List[EditOp] = Field(default_factory=list)
    source_text: str = ""
    resolver: Literal["rules", "llm", "manual"] = "manual"
    ambiguous: bool = False
    question: Optional[str] = Field(None, description="What to ask the director when the request is ambiguous")


class RevisionError(Exception):
    pass


# ── natural language -> patch ──────────────────────────────────────────────

_FIELD_WORDS = [
    ("lighting", r"ánh sáng|anh sang|lighting|light"),
    ("camera", r"góc máy|goc may|chuyển động máy|camera|máy quay"),
    ("dialogue", r"lời thoại|câu thoại|thoại|thoai|dialogue|line"),
    ("action", r"hành động|hanh dong|action"),
    ("duration", r"thời lượng|thoi luong|duration|độ dài"),
    ("narration", r"lời dẫn|dẫn truyện|narration|voice-?over"),
    ("start_state", r"trạng thái đầu|start state|khung đầu"),
    ("end_state", r"trạng thái cuối|end state|khung cuối"),
]
_ASSET_FIELD_WORDS = [
    ("wardrobe_default", r"trang phục|quần áo|wardrobe|outfit|clothes"),
    ("identity", r"ngoại hình|khuôn mặt|gương mặt|appearance|identity|face"),
    ("voice_instructions", r"giọng|voice"),
]
_SHOT_REF = re.compile(r"(?:shot|cảnh|canh|cảnh số|shot số)\s*#?\s*0*(\d+)", re.I)
_VALUE = re.compile(r"(?:\bthành\b|\bsang\b|\bto\b|\bbằng\b|=|:)\s*[\"“']?(.+?)[\"”']?\s*$", re.I | re.S)


def resolve_edit(text: str, bp, assets: Dict[str, Dict[str, Any]]) -> EditPatch:
    t = " ".join(text.strip().split())
    shot_m = _SHOT_REF.search(t)
    val_m = _VALUE.search(t)
    value = val_m.group(1).strip() if val_m else None
    if shot_m:
        idx = int(shot_m.group(1)) - 1
        if not 0 <= idx < len(bp.shots):
            return EditPatch(source_text=text, resolver="rules", ambiguous=True,
                             question=f"Tập này có {len(bp.shots)} shot; shot {idx + 1} không tồn tại.")
        sid = bp.shots[idx].shot_id
        for fld, words in _FIELD_WORDS:
            if re.search(words, t, re.I):
                if value is None:
                    return EditPatch(source_text=text, resolver="rules", ambiguous=True,
                                     question=f"Đổi {fld} của {sid} thành gì?")
                if fld == "duration":
                    num = re.search(r"(\d+(?:[.,]\d+)?)", value)
                    if not num:
                        return EditPatch(source_text=text, resolver="rules", ambiguous=True,
                                         question="Thời lượng mới là bao nhiêu giây?")
                    value = float(num.group(1).replace(",", "."))
                return EditPatch(ops=[EditOp(target="shot", shot_id=sid, field=fld, value=value)], source_text=text,
                                 resolver="rules")
    for fld, words in _ASSET_FIELD_WORDS:
        if re.search(words, t, re.I):
            hit = [aid for aid, a in assets.items() if a["kind"] == "character" and
                   re.search(r"\b" + re.escape(a["name"]) + r"\b", t, re.I)]
            if len(hit) == 1 and value:
                return EditPatch(ops=[EditOp(target="asset", asset_id=hit[0], field=fld, value=value)],
                                 source_text=text, resolver="rules")
            return EditPatch(source_text=text, resolver="rules", ambiguous=True,
                             question="Thay đổi này áp dụng cho nhân vật nào, và giá trị mới là gì?")
    return EditPatch(source_text=text, resolver="rules", ambiguous=True,
                     question="Chưa hiểu yêu cầu. Hãy ghi rõ shot (vd. 'shot 3'), thuộc tính (ánh sáng, góc máy, "
                              "thoại, hành động, thời lượng) và giá trị mới sau 'thành'.")


def resolve_edit_llm(text: str, bp, assets) -> EditPatch:
    """Live mode only: the edit_resolver crew member (YAML prompt) returns a typed EditPatch."""
    from ..agents.edit_resolver import EditResolverAgent
    p = EditResolverAgent().run(text, bp, assets)
    p.resolver = "llm"
    return p


# ── apply ops in memory ────────────────────────────────────────────────────

def _apply_ops(patch: EditPatch, bp, studio: Dict[str, Any], assets: Dict[str, Dict[str, Any]]) -> List[str]:
    """Mutates bp / studio / assets copies. Returns lock conflicts (nothing is applied by the caller if any)."""
    conflicts = []
    for op in patch.ops:
        if op.target == "shot":
            if op.field not in SHOT_FIELDS:
                raise RevisionError(f"unknown shot field {op.field}")
            shot = next((s for s in bp.shots if s.shot_id == op.shot_id), None)
            if shot is None:
                raise RevisionError(f"no shot {op.shot_id}")
            ss = studio["shots"][op.shot_id]
            if ss.get("locked"):
                conflicts.append(f"{op.shot_id} is locked")
            has_base = bool(ss.get("base"))
            if op.field == "lighting":
                shot.staging_layer.lighting = str(op.value)
            elif op.field == "camera":
                shot.staging_layer.camera.movement = str(op.value)
            elif op.field == "action":
                shot.narrative_layer.narrative_action = str(op.value)
            elif op.field == "duration":
                shot.staging_layer.duration_seconds = float(op.value)
            elif op.field == "dialogue":
                d = shot.narrative_layer.dialogue
                if not d.speaker_asset_id:
                    raise RevisionError(f"{op.shot_id} has no speaker; name the character for the new line")
                if ss.get("voice_locked"):
                    conflicts.append(f"{op.shot_id} voice is locked")
                d.text, d.has_dialogue = str(op.value), True
            elif op.field in ("start_state", "end_state", "narration"):
                ss[op.field] = op.value
            elif op.field == "wardrobe":
                if not isinstance(op.value, dict):
                    raise RevisionError("wardrobe value must be {asset_id: state}")
                ss["wardrobe"].update(op.value)
            if has_base and op.field in ("lighting", "camera", "action", "start_state", "end_state"):
                ss.setdefault("overrides", {})[op.field] = op.value
        else:
            a = assets.get(op.asset_id)
            if a is None:
                raise RevisionError(f"no asset {op.asset_id}")
            if a["locked"]:
                conflicts.append(f"asset {op.asset_id} identity is locked")
            if op.field == "identity":
                a["data"]["identity"] = str(op.value)
            elif op.field == "wardrobe_default":
                a["data"].setdefault("wardrobe", {})["default"] = str(op.value)
            elif op.field == "voice_instructions":
                a["data"].setdefault("voice", {})["instructions"] = str(op.value)
            else:
                raise RevisionError(f"unknown asset field {op.field}")
    return conflicts


def _changed_keys(store, episode: Dict[str, Any], assets_override=None) -> Dict[str, str]:
    """{job_key: spec_hash} for the (possibly modified) episode, plus project-scope reference jobs."""
    specs = domain.plan_jobs(store, episode, assets_override)
    out = {s["key"]: hash_obj({"kind": s["kind"], "spec": s["spec"], "deps": s["deps"]}) for s in specs}
    deps = {s["key"]: s["deps"] for s in specs}
    return out, deps


def preview(studio, episode_id: str, patch: EditPatch) -> Dict[str, Any]:
    store, engine = studio.store, studio.engine
    ep = store.get("episodes", episode_id)
    project = store.get("projects", ep["project_id"])
    assets = copy.deepcopy(domain.assets_by_asset_id(store, project["id"]))
    if patch.ambiguous or not patch.ops:
        return {"patch": patch.model_dump(), "applicable": False, "question": patch.question}
    episodes = store.q("SELECT * FROM episodes WHERE project_id = ? ORDER BY number", (project["id"],))
    touched_assets = {op.asset_id for op in patch.ops if op.target == "asset"}
    impact, total, unknown, conflicts = [], 0.0, [], []
    for e in episodes:
        if e["id"] != episode_id and not touched_assets:
            continue
        bp = domain.load_blueprint(e)
        studio_sc = copy.deepcopy(e["studio"])
        ops = [op for op in patch.ops if op.target == "asset" or e["id"] == episode_id]
        conflicts += _apply_ops(EditPatch(ops=ops), bp, studio_sc, assets)
        new_ep = dict(e, blueprint=bp.model_dump(), studio=studio_sc)
        new_hashes, deps = _changed_keys(store, new_ep, assets)
        cur = {j["key"]: j for j in store.q("SELECT * FROM jobs WHERE episode_id = ? AND state != 'obsolete'", (e["id"],))}
        direct = {k for k, h in new_hashes.items() if k not in cur or cur[k]["spec_hash"] != h}
        if touched_assets:
            direct |= {k for k, d in deps.items() if any(x in (f"ref:{a}" for a in touched_assets) for x in d)}
        affected = set(direct)
        changed = True
        while changed:  # downstream closure: a re-rendered upstream changes its dependents' inputs
            changed = False
            for k, d in deps.items():
                if k not in affected and any(x in affected for x in d):
                    affected.add(k)
                    changed = True
        locked_hits = [k for k in affected if cur.get(k, {}).get("locked")]
        conflicts += [f"{e['title']}: {k} is locked" for k in locked_hits]
        items = []
        for k in sorted(affected):
            kind = (cur.get(k) or {}).get("kind") or ("video" if k.startswith("clip:") else "tts"
                                                      if k.startswith(("tts:", "narr:")) else "image")
            usd = _estimate(engine, project, kind, cur.get(k))
            if usd is None:
                unknown.append(k)
            else:
                total += usd
            items.append({"key": k, "kind": kind, "reason": "changed" if k in direct else "depends on a changed input",
                          "estimate_usd": usd})
        kept = [k for k in cur if k not in affected and not k.startswith("ref:")]
        impact.append({"episode_id": e["id"], "episode": f"Ep {e['number']}: {e['title']}", "affected": items,
                       "kept_cached": kept})
    if touched_assets:
        ref_items = []
        for a in touched_assets:
            j = store.one("SELECT * FROM jobs WHERE episode_id = ? AND key = ?", (project["id"], f"ref:{a}"))
            if j and assets[a]["canonical_media"] is None:
                usd = _estimate(engine, project, "image", j)
                ref_items.append({"key": f"ref:{a}", "kind": "image", "reason": "identity reference sheet",
                                  "estimate_usd": usd})
                total += usd or 0
                if usd is None:
                    unknown.append(f"ref:{a}")
        impact.insert(0, {"episode_id": None, "episode": "series assets", "affected": ref_items, "kept_cached": []})
    n_eps = sum(1 for i in impact if i["episode_id"] and i["affected"])
    requires_confirmation = bool(touched_assets) or n_eps > 1
    return {"patch": patch.model_dump(), "applicable": not conflicts, "lock_conflicts": sorted(set(conflicts)),
            "impact": impact, "episodes_affected": n_eps,
            "model_jobs_affected": sum(len(i["affected"]) for i in impact),
            "estimate_usd": round(total, 6), "estimate_unknown_for": unknown,
            "mode": project["mode"], "requires_confirmation": requires_confirmation,
            "warning": ("Identity/series-wide change: it invalidates shots in every episode that uses this asset."
                        if touched_assets else None)}


def _estimate(engine, project, kind: str, job: Optional[Dict[str, Any]]) -> Optional[float]:
    prov = engine.provider_for(project, kind)
    spec = (job or {}).get("spec") or {}
    req = {"prompt": spec.get("prompt", ""), "text": spec.get("text", ""), "size": spec.get("size"),
           "duration": spec.get("planned_duration", 0)}
    req, _ = prov.adapt({k: v for k, v in req.items() if k in prov.caps.accepted_fields})
    usd = prov.estimate_cost(req)
    if kind in ("image", "video") and project["settings"].get("review", True):
        j = engine.provider_for(project, "judge").estimate_cost({})
        usd = None if (usd is None or j is None) else usd + j
    return usd


def apply(studio, episode_id: str, patch: EditPatch, confirm: bool = False) -> Dict[str, Any]:
    pv = preview(studio, episode_id, patch)
    if not pv.get("applicable"):
        raise RevisionError(pv.get("question") or f"cannot apply: {pv.get('lock_conflicts')}")
    if pv["requires_confirmation"] and not confirm:
        raise RevisionError("this change affects identity or several episodes; review the impact and confirm")
    store = studio.store
    ep = store.get("episodes", episode_id)
    project = store.get("projects", ep["project_id"])
    assets = copy.deepcopy(domain.assets_by_asset_id(store, project["id"]))
    touched_assets = {op.asset_id for op in patch.ops if op.target == "asset"}
    results = {}
    asset_ops = [op for op in patch.ops if op.target == "asset"]
    shot_ops = [op for op in patch.ops if op.target == "shot"]
    targets = store.q("SELECT * FROM episodes WHERE project_id = ?", (project["id"],)) if touched_assets else [ep]
    with store.tx():
        if asset_ops:
            _apply_ops(EditPatch(ops=asset_ops), None, {}, assets)
            for aid in touched_assets:
                a = assets[aid]
                store.update("assets", "id", a["id"], {"data": a["data"], "version": a["version"] + 1,
                                                        "content_hash": hash_obj(a["data"]), "updated_at": now()})
        for e in targets:
            bp = domain.load_blueprint(e)
            sc = copy.deepcopy(e["studio"])
            if e["id"] == episode_id:
                _apply_ops(EditPatch(ops=shot_ops), bp, sc, assets)
            domain.save_episode(store, e["id"], bp, sc, patch.model_dump(),
                                f"revision: {patch.source_text or 'manual patch'}")
    for e in targets:
        results[e["id"]] = domain.sync_jobs(store, e["id"])
    store.event("info", f"revision applied: {patch.source_text or patch.ops}", episode_id=episode_id)
    return {"preview": pv, "sync": results, "version": store.get("episodes", episode_id)["version"]}


def rollback(studio, episode_id: str, version: int) -> Dict[str, Any]:
    store = studio.store
    row = store.one("SELECT * FROM episode_versions WHERE episode_id = ? AND version = ?", (episode_id, version))
    if row is None:
        raise RevisionError(f"no version {version}")
    from ..schemas.blueprint import SceneBlueprint
    bp = SceneBlueprint.model_validate(row["blueprint"])
    domain.save_episode(store, episode_id, bp, row["studio"], {"rollback_to": version}, f"rollback to v{version}")
    res = domain.sync_jobs(store, episode_id)
    return {"version": store.get("episodes", episode_id)["version"], "sync": res}


def versions(store, episode_id: str) -> List[Dict[str, Any]]:
    return store.q("SELECT episode_id, version, patch, note, created_at FROM episode_versions WHERE episode_id = ? "
                   "ORDER BY version", (episode_id,))
