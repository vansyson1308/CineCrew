#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Studio domain: projects, the persistent series asset library, episodes (FilmDSL + studio sidecar)
and the job graph derived from them.

Creative plan vs execution
  The episode's SceneBlueprint is the creative plan (FilmDSL). The studio sidecar adds per-shot
  start/end state, wardrobe state, props, narration, locks and directed overrides. `plan_jobs()`
  derives the execution graph (job keys, specs, deps) from both; the scheduler never edits the plan.

Identity vs shot state
  An asset's `identity` (face/body, or a location's fixed look) is immutable unless explicitly
  revised (and locked assets refuse that). Wardrobe is a named state chosen per shot. Pose, action,
  mood and lighting belong to the shot.

Prompts are composed deterministically from those fields (compose_*), so an edit to one field changes
exactly the jobs whose composed request includes it — the basis of selective regeneration.
"""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..schemas.assets import AssetLibrary, CharacterAsset, LocationAsset, NarrativeContext, PropAsset, VisualReferences
from ..schemas.blueprint import (
    BlueprintMetadata, CameraSpec, CharacterConsistencyControl, ConditioningSpec, DialogueDef, EntityInStaging,
    NarrativeLayer, RenderLayer, SceneBlueprint, ShotBlueprint, StagingLayer,
)
from .hashing import hash_obj
from .store import Store, new_id, now

TEMPLATES = Path(__file__).resolve().parent / "templates"
FIXTURE_SIZES = {"9:16": "360x640", "16:9": "640x360"}
KINDS_WITH_REVIEW = ("image", "video")


# ── projects & assets ──────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "aspect": "9:16",
    "language": "vi",
    "review": True,                       # judge every keyframe / clip
    "max_attempts": 2,                    # generation attempts per job (1 + retries)
    "require_storyboard_approval": True,  # clips wait for an approved storyboard
    "subtitles": {"burn_in": True, "max_chars_per_line": 32, "max_lines": 2, "font_size": 54},
    "audio": {"music_asset": None, "music_gain_db": -16, "duck_db": 10, "keep_clip_audio_db": None,
              "fade_in": 0.3, "fade_out": 0.8, "narration": True},
    "overlays": [],                       # exact deterministic overlays: title/logo/price/cta
    "concurrency": 2,
}


def create_project(store: Store, name: str, kind: str = "series", mode: str = "offline_fixture",
                   budget_limit_usd: float = 0.0, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if mode not in ("offline_fixture", "live"):
        raise ValueError("mode must be offline_fixture or live")
    s = copy.deepcopy(DEFAULT_SETTINGS)
    _deep_update(s, settings or {})
    row = {"id": new_id("prj"), "name": name, "kind": kind, "mode": mode, "settings": s,
           "budget_limit_usd": float(budget_limit_usd), "created_at": now()}
    store.insert("projects", row)
    store.event("info", f"project created ({mode})", project_id=row["id"])
    return row


def _deep_update(dst: Dict, src: Dict) -> Dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v
    return dst


def add_asset(store: Store, project_id: str, kind: str, name: str, data: Dict[str, Any],
              canonical_media: Optional[str] = None, locked: bool = False) -> Dict[str, Any]:
    if not data.get("asset_id"):
        raise ValueError("asset data needs a stable asset_id (e.g. char_minh)")
    row = {"id": new_id("ast"), "project_id": project_id, "kind": kind, "name": name, "data": data,
           "locked": int(locked), "version": 1, "content_hash": hash_obj(data), "canonical_media": canonical_media,
           "updated_at": now()}
    store.insert("assets", row)
    return row


def assets_by_asset_id(store: Store, project_id: str) -> Dict[str, Dict[str, Any]]:
    return {a["data"]["asset_id"]: a for a in store.q("SELECT * FROM assets WHERE project_id = ?", (project_id,))}


def asset_library(store: Store, project_id: str, title: str = "", global_style: str = "") -> AssetLibrary:
    """FilmDSL AssetLibrary view of the studio's persistent assets (stable IDs are the contract)."""
    chars, locs, props = [], [], []
    for a in assets_by_asset_id(store, project_id).values():
        d = a["data"]
        ref = VisualReferences(canonical_image_path=None)
        if a["kind"] == "character":
            chars.append(CharacterAsset(id=d["asset_id"], name=a["name"], description=d.get("identity", ""),
                                        clothing_style=(d.get("wardrobe") or {}).get("default", ""),
                                        voice_description=(d.get("voice") or {}).get("instructions", ""),
                                        visual_references=ref))
        elif a["kind"] == "location":
            locs.append(LocationAsset(id=d["asset_id"], name=a["name"], type="EXT", visual_style=d.get("identity", "")))
        elif a["kind"] in ("prop", "product", "logo"):
            props.append(PropAsset(id=d["asset_id"], name=a["name"], visual_description=d.get("identity", ""),
                                   importance="critical" if a["kind"] == "product" else "supporting"))
    return AssetLibrary(project_title=title, global_style=global_style,
                        narrative_context=NarrativeContext(time_period="present day", global_mood=""),
                        characters=chars, locations=locs, props=props)


# ── episodes ───────────────────────────────────────────────────────────────

def load_template(template_id: str) -> Dict[str, Any]:
    return json.loads((TEMPLATES / f"{template_id}.json").read_text(encoding="utf-8"))


def create_series_from_template(store: Store, template_id: str = "hanoi_delivery", mode: str = "offline_fixture",
                                budget_limit_usd: float = 0.0, name: Optional[str] = None,
                                episodes: Optional[List[int]] = None) -> Dict[str, Any]:
    t = load_template(template_id)
    project = create_project(store, name or t["series_title"], kind=t.get("kind", "series"), mode=mode,
                             budget_limit_usd=budget_limit_usd,
                             settings={"aspect": t.get("aspect", "9:16"), "language": t.get("language", "vi"),
                                       "global_style": t.get("global_style", ""), "template_id": template_id})
    for a in t["assets"]:
        data = {k: v for k, v in a.items() if k not in ("kind", "name")}
        add_asset(store, project["id"], a["kind"], a["name"], data)
    for ep in t["episodes"]:
        if episodes and ep["number"] not in episodes:
            continue
        create_episode_from_spec(store, project["id"], ep)
    return project


def create_episode_from_spec(store: Store, project_id: str, ep: Dict[str, Any]) -> Dict[str, Any]:
    """Structured episode spec (template format) -> FilmDSL blueprint + studio sidecar."""
    project = store.get("projects", project_id)
    assets = assets_by_asset_id(store, project_id)
    number = ep.get("number") or (store.one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM episodes WHERE project_id = ?",
                                            (project_id,))["n"])
    eid = new_id("ep")
    shots, studio_shots = [], {}
    for s in ep["shots"]:
        dl = s.get("dialogue")
        speaker = assets.get(dl["speaker"]) if dl else None
        if dl and speaker is None:
            raise ValueError(f"{s['shot_id']}: speaker {dl['speaker']} is not in the asset library")
        chars = s.get("characters", {})
        for cid in list(chars) + s.get("props", []) + [s.get("location")]:
            if cid and cid not in assets:
                raise ValueError(f"{s['shot_id']}: {cid} is not in the asset library (IDs are the contract)")
        shot = ShotBlueprint(
            shot_id=s["shot_id"],
            narrative_layer=NarrativeLayer(
                narrative_action=s["action"], emotional_beat=s.get("beat", ""),
                dialogue=DialogueDef(has_dialogue=bool(dl), speaker_asset_id=dl["speaker"] if dl else None,
                                     text=dl["text"] if dl else None)),
            staging_layer=StagingLayer(
                duration_seconds=float(s["duration"]),
                camera=CameraSpec(shot_scale=s.get("scale"), angle=s.get("angle"), movement=s.get("movement")),
                lighting=s.get("lighting"), environment_id=s.get("location"),
                entities=[EntityInStaging(asset_id=c) for c in chars] + [EntityInStaging(asset_id=p) for p in s.get("props", [])],
                consistency_constraints=[f"{assets[c]['name']} wears: {(assets[c]['data'].get('wardrobe') or {}).get(w, w)}"
                                         for c, w in chars.items()]),
            render_layer=RenderLayer(),
        )
        shots.append(shot)
        studio_shots[s["shot_id"]] = {
            "wardrobe": chars, "props": s.get("props", []), "start_state": s.get("start_state", ""),
            "end_state": s.get("end_state", ""), "narration": s.get("narration"), "locked": False,
            "overrides": {}, "base": None,
        }
    bp = SceneBlueprint(
        blueprint_id=f"bp_{eid}",
        metadata=BlueprintMetadata(project_name=project["name"], target_resolution=_render_size(project, "video"),
                                   target_aspect_ratio=project["settings"]["aspect"], global_fps=24),
        global_style=project["settings"].get("global_style", ""), shots=shots)
    studio = {"shots": studio_shots, "source": "template" if ep.get("shots") else "pipeline"}
    _refresh_render_layer(bp, studio, project, assets)
    row = {"id": eid, "project_id": project_id, "number": number, "title": ep["title"], "script": ep.get("script", ""),
           "blueprint": json.loads(bp.model_dump_json()), "studio": studio, "version": 1, "storyboard_approved": 0,
           "planning": ep.get("planning", {"mode": "template"}), "created_at": now(), "updated_at": now()}
    store.insert("episodes", row)
    store.insert("episode_versions", {"episode_id": eid, "version": 1, "blueprint": row["blueprint"], "studio": studio,
                                      "patch": None, "note": "created", "created_at": now()})
    sync_jobs(store, eid)
    return row


def create_episode_from_blueprint(store: Store, project_id: str, title: str, bp: SceneBlueprint,
                                  script: str = "", planning: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Import a crew-planned blueprint (LLM pipeline or a shipped example). Its resolved prompts become the
    shot's `base` prompts; studio overrides are appended to them as explicit directed changes."""
    project = store.get("projects", project_id)
    assets = assets_by_asset_id(store, project_id)
    studio_shots = {}
    for shot in bp.shots:
        r = shot.render_layer
        studio_shots[shot.shot_id] = {
            "wardrobe": {e.asset_id: "default" for e in (shot.staging_layer.entities if shot.staging_layer else [])
                         if e.asset_id.startswith("char_") and e.asset_id in assets},
            "props": [e.asset_id for e in (shot.staging_layer.entities if shot.staging_layer else [])
                      if not e.asset_id.startswith("char_") and e.asset_id in assets],
            "start_state": "", "end_state": "", "narration": None, "locked": False, "overrides": {},
            "base": {"t2i": (r.image.resolved_ti2i or r.image.resolved_t2i) if r else None,
                     "i2v": r.video.resolved_i2v if r else None},
        }
    eid = new_id("ep")
    number = store.one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM episodes WHERE project_id = ?", (project_id,))["n"]
    studio = {"shots": studio_shots, "source": "pipeline"}
    row = {"id": eid, "project_id": project_id, "number": number, "title": title, "script": script,
           "blueprint": json.loads(bp.model_dump_json()), "studio": studio, "version": 1, "storyboard_approved": 0,
           "planning": planning or {"mode": "imported"}, "created_at": now(), "updated_at": now()}
    store.insert("episodes", row)
    store.insert("episode_versions", {"episode_id": eid, "version": 1, "blueprint": row["blueprint"], "studio": studio,
                                      "patch": None, "note": "imported", "created_at": now()})
    sync_jobs(store, eid)
    return row


def load_blueprint(episode: Dict[str, Any]) -> SceneBlueprint:
    from .migrations import migrate_blueprint
    return SceneBlueprint.model_validate(migrate_blueprint(episode["blueprint"]))


# ── deterministic prompt composition ───────────────────────────────────────

def _render_size(project: Dict[str, Any], kind: str) -> str:
    aspect = project["settings"].get("aspect", "9:16")
    if project["mode"] == "live":
        from .providers import load_provider_config
        cfg = (load_provider_config().get("live") or {}).get(kind) or {}
        return cfg.get("size_portrait" if aspect == "9:16" else "size_landscape") or FIXTURE_SIZES[aspect]
    return FIXTURE_SIZES[aspect]


def _wardrobe(asset: Dict[str, Any], state: str) -> str:
    w = asset["data"].get("wardrobe") or {}
    return w.get(state) or w.get("default") or ""


def compose_image_prompt(shot: ShotBlueprint, sshot: Dict[str, Any], assets: Dict[str, Dict], style: str) -> str:
    st, nl = shot.staging_layer, shot.narrative_layer
    cast = [f"{assets[c]['name']} ({c}): {assets[c]['data'].get('identity', '')}; wearing {_wardrobe(assets[c], w)}"
            for c, w in sshot.get("wardrobe", {}).items() if c in assets]
    props = [f"{assets[p]['name']}: {assets[p]['data'].get('identity', '')}" for p in sshot.get("props", []) if p in assets]
    loc = assets.get(st.environment_id) if st and st.environment_id else None
    base = (sshot.get("base") or {}).get("t2i")
    if base:
        directed = _overrides_text(sshot, st)
        return base + (f"\n\nDirected changes (they override anything above): {directed}" if directed else "")
    cam = st.camera if st else CameraSpec()
    parts = [
        f"{style}." if style else "",
        f"{cam.shot_scale or 'MS'} shot, {(cam.angle or 'eye_level').replace('_', ' ')} angle.",
        f"Location: {loc['data'].get('identity')}." if loc else "",
        f"Lighting: {st.lighting}." if st and st.lighting else "",
        ("Characters: " + " | ".join(cast) + ".") if cast else "No people in frame.",
        ("Props: " + " | ".join(props) + ".") if props else "",
        f"First frame: {sshot.get('start_state')}." if sshot.get("start_state") else "",
        f"About to happen: {nl.narrative_action}" if nl else "",
        "Photographic still, no text, no captions, no watermarks.",
    ]
    return " ".join(p for p in parts if p)


def compose_video_prompt(shot: ShotBlueprint, sshot: Dict[str, Any], assets: Dict[str, Dict], style: str) -> str:
    st, nl = shot.staging_layer, shot.narrative_layer
    base = (sshot.get("base") or {}).get("i2v")
    if base:
        directed = _overrides_text(sshot, st)
        return base + (f"\n\nDirected changes (they override anything above): {directed}" if directed else "")
    names = ", ".join(assets[c]["name"] for c in sshot.get("wardrobe", {}) if c in assets)
    parts = [
        nl.narrative_action if nl else "",
        f"Camera: {st.camera.movement}." if st and st.camera.movement else "",
        f"Lighting stays: {st.lighting}." if st and st.lighting else "",
        f"Ends with: {sshot.get('end_state')}." if sshot.get("end_state") else "",
        f"{names} keep exactly the faces, hair and clothing of the first frame." if names else "",
        "Natural motion, no cuts, no on-screen text.",
        f"Style: {style}." if style else "",
    ]
    return " ".join(p for p in parts if p)


def _overrides_text(sshot: Dict[str, Any], st: Optional[StagingLayer]) -> str:
    o = sshot.get("overrides") or {}
    return "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(o.items()))


def _refresh_render_layer(bp: SceneBlueprint, studio: Dict[str, Any], project: Dict[str, Any], assets: Dict) -> None:
    """Keep FilmDSL Layer 3 truthful: the resolved prompts ARE the composed studio prompts."""
    style = bp.global_style
    for shot in bp.shots:
        ss = studio["shots"][shot.shot_id]
        rl = shot.ensure_render_layer()
        rl.image.resolved_t2i = compose_image_prompt(shot, ss, assets, style)
        rl.image.characters_in_shot = list(ss.get("wardrobe", {}))
        rl.image.character_appearances = {c: f"{assets[c]['data'].get('identity', '')}; wearing {_wardrobe(assets[c], w)}"
                                          for c, w in ss.get("wardrobe", {}).items() if c in assets}
        rl.image.conditioning = ConditioningSpec(character_consistency=[
            CharacterConsistencyControl(asset_id=c, method="reference_image") for c in ss.get("wardrobe", {})])
        rl.video.resolved_i2v = compose_video_prompt(shot, ss, assets, style)
        rl.video.lip_sync_constraint.enabled = False


# ── job graph ──────────────────────────────────────────────────────────────

def plan_jobs(store: Store, episode: Dict[str, Any], assets: Optional[Dict[str, Dict]] = None) -> List[Dict[str, Any]]:
    """
    Job specs for an episode (pure; no DB writes). Keys:
      ref:<asset_id>   project-scope reference sheet (only when no canonical image was supplied)
      kf:<shot>        storyboard keyframe (image)     deps: ref sheets of the shot's characters/props
      tts:<shot>       dialogue line (tts)
      narr:<shot>      narration line (tts)
      clip:<shot>      shot clip (video)              deps: kf, tts/narr (duration follows measured speech)
    """
    project = store.get("projects", episode["project_id"])
    assets = assets if assets is not None else assets_by_asset_id(store, project["id"])
    bp = load_blueprint(episode)
    studio = episode["studio"]
    style = bp.global_style
    img_size, vid_size = _render_size(project, "image"), _render_size(project, "video")
    live = project["mode"] == "live"
    specs: List[Dict[str, Any]] = []
    for i, shot in enumerate(bp.shots):
        ss = studio["shots"][shot.shot_id]
        refs = [a for a in list(ss.get("wardrobe", {})) + ss.get("props", []) if a in assets]
        ref_keys = [f"ref:{a}" for a in refs]
        specs.append({"key": f"kf:{shot.shot_id}", "kind": "image", "shot_id": shot.shot_id, "deps": ref_keys,
                      "mandatory": True, "locked": bool(ss.get("locked") or ss.get("keyframe_locked")),
                      "spec": {"purpose": "keyframe", "prompt": compose_image_prompt(shot, ss, assets, style),
                               "size": img_size, "seed": 1000 + i, "references": refs,
                               "label": f"{episode['title']} · {shot.shot_id} keyframe",
                               "negative_prompt": "text, watermark, extra people, deformed hands"}})
        dl = shot.narrative_layer.dialogue
        speech_keys = []
        if dl.has_dialogue and dl.text:
            spk = assets.get(dl.speaker_asset_id)
            v = (spk["data"].get("voice") or {}) if spk else {}
            specs.append({"key": f"tts:{shot.shot_id}", "kind": "tts", "shot_id": shot.shot_id, "deps": [],
                          "mandatory": True, "locked": bool(ss.get("voice_locked")),
                          "spec": {"purpose": "dialogue", "text": dl.text, "speaker": dl.speaker_asset_id,
                                   "voice": dl.voice_preset or v.get("live" if live else "fixture"),
                                   "instructions": "; ".join(x for x in (v.get("instructions"),
                                                   f"Emotion: {shot.narrative_layer.performance_emotion or shot.narrative_layer.emotional_beat}")
                                                   if x),
                                   "language": project["settings"].get("language", "vi")}})
            speech_keys.append(f"tts:{shot.shot_id}")
        if ss.get("narration") and project["settings"]["audio"].get("narration", True):
            nar = assets.get("voice_narrator")
            v = (nar["data"].get("voice") or {}) if nar else {}
            specs.append({"key": f"narr:{shot.shot_id}", "kind": "tts", "shot_id": shot.shot_id, "deps": [],
                          "mandatory": True, "locked": False,
                          "spec": {"purpose": "narration", "text": ss["narration"], "speaker": "voice_narrator",
                                   "voice": v.get("live" if live else "fixture") or ("alloy" if live else "fixture_narrator"),
                                   "instructions": v.get("instructions", ""),
                                   "language": project["settings"].get("language", "vi")}})
            speech_keys.append(f"narr:{shot.shot_id}")
        specs.append({"key": f"clip:{shot.shot_id}", "kind": "video", "shot_id": shot.shot_id,
                      "deps": [f"kf:{shot.shot_id}"] + speech_keys, "mandatory": True,
                      "locked": bool(ss.get("locked")),
                      "spec": {"purpose": "clip", "prompt": compose_video_prompt(shot, ss, assets, style),
                               "planned_duration": shot.staging_layer.duration_seconds, "size": vid_size,
                               "seed": 2000 + i, "label": f"{episode['title']} · {shot.shot_id} clip",
                               "negative_prompt": "text, watermark, morphing faces, extra limbs"}})
    return specs


def plan_reference_jobs(store: Store, project_id: str) -> List[Dict[str, Any]]:
    project = store.get("projects", project_id)
    style = project["settings"].get("global_style", "")
    out = []
    for aid, a in assets_by_asset_id(store, project_id).items():
        if a["kind"] not in ("character", "location", "prop", "product"):
            continue
        d = a["data"]
        if a["kind"] == "character":
            prompt = (f"Character reference sheet on a plain light-grey background: full-body front view and a face "
                      f"close-up of {a['name']}: {d.get('identity', '')}; wearing {(d.get('wardrobe') or {}).get('default', '')}. "
                      f"Neutral expression, even studio light. {style}")
        elif a["kind"] == "location":
            prompt = f"Establishing shot, no people: {d.get('identity', '')}. {style}"
        else:
            prompt = f"Product/prop reference on a plain background: {d.get('identity', '')}. Even light, sharp detail."
        out.append({"key": f"ref:{aid}", "kind": "image", "shot_id": None, "deps": [], "mandatory": True,
                    "locked": bool(a["locked"]), "supplied": a["canonical_media"],
                    "spec": {"purpose": "reference_sheet", "asset_id": aid, "prompt": prompt,
                             "size": _render_size(project, "image"), "seed": 7,
                             "label": f"{a['name']} reference", "references": []}})
    return out


def _upsert_jobs(store: Store, scope_id: str, specs: List[Dict[str, Any]], max_attempts: int) -> Dict[str, List[str]]:
    existing = {j["key"]: j for j in store.q("SELECT * FROM jobs WHERE episode_id = ?", (scope_id,))}
    changed, added, kept = [], [], []
    for s in specs:
        h = hash_obj({"kind": s["kind"], "spec": s["spec"], "deps": s["deps"]})
        j = existing.get(s["key"])
        if j is None:
            row = {"id": new_id("job"), "episode_id": scope_id, "key": s["key"], "kind": s["kind"],
                   "shot_id": s.get("shot_id"), "deps": s["deps"], "spec": s["spec"], "spec_hash": h,
                   "input_hash": None, "mandatory": int(s.get("mandatory", True)), "state": "pending",
                   "provider_state": "none", "verdict": "pending", "verdict_source": None, "selected_attempt": None,
                   "locked": int(s.get("locked", False)), "max_attempts": max_attempts, "transport_retries": 0,
                   "next_run_at": 0, "cancel_requested": 0, "lease_until": 0, "error": None, "cache_hit": 0,
                   "created_at": now(), "updated_at": now()}
            if s.get("supplied"):
                row.update(state="done", verdict="accepted", verdict_source="supplied")
            store.insert("jobs", row)
            added.append(s["key"])
        elif j["spec_hash"] != h:
            # The plan changed: the old attempts stay (history, cache, rollback); the job runs again.
            store.update("jobs", "id", j["id"], {
                "spec": s["spec"], "deps": s["deps"], "spec_hash": h, "state": "pending", "provider_state": "none",
                "verdict": "pending", "verdict_source": None, "selected_attempt": None, "input_hash": None,
                "error": None, "cache_hit": 0, "transport_retries": 0, "cancel_requested": 0, "force_new": 0,
                "epoch": now(), "locked": int(s.get("locked", False)), "updated_at": now()})
            changed.append(s["key"])
        else:
            store.update("jobs", "id", j["id"], {"locked": int(s.get("locked", False))})
            if s.get("supplied") and j["verdict"] != "accepted":
                store.update("jobs", "id", j["id"], {"state": "done", "verdict": "accepted", "verdict_source": "supplied"})
            kept.append(s["key"])
    gone = [k for k in existing if k not in {s["key"] for s in specs}]
    for k in gone:
        store.update("jobs", "id", existing[k]["id"], {"state": "obsolete", "mandatory": 0, "updated_at": now()})
    return {"changed": changed, "added": added, "kept": kept, "obsolete": gone}


def sync_jobs(store: Store, episode_id: str) -> Dict[str, List[str]]:
    """Reconcile the job table with the current plan. Returns which job keys changed."""
    ep = store.get("episodes", episode_id)
    project = store.get("projects", ep["project_id"])
    maxa = int(project["settings"].get("max_attempts", 2))
    ref_result = _upsert_jobs(store, project["id"], plan_reference_jobs(store, project["id"]), maxa)
    result = _upsert_jobs(store, episode_id, plan_jobs(store, ep), maxa)
    result["changed"] += ref_result["changed"]
    return result


def jobs_for_episode(store: Store, episode_id: str) -> List[Dict[str, Any]]:
    ep = store.get("episodes", episode_id)
    return store.q("SELECT * FROM jobs WHERE episode_id IN (?, ?) AND state != 'obsolete' ORDER BY created_at",
                   (episode_id, ep["project_id"]))


def resolve_dep(store: Store, episode: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    scope = episode["project_id"] if key.startswith("ref:") else episode["id"]
    return store.one("SELECT * FROM jobs WHERE episode_id = ? AND key = ?", (scope, key))


def save_episode(store: Store, episode_id: str, bp: SceneBlueprint, studio: Dict[str, Any], patch: Any, note: str) -> int:
    ep = store.get("episodes", episode_id)
    project = store.get("projects", ep["project_id"])
    _refresh_render_layer(bp, studio, project, assets_by_asset_id(store, project["id"]))
    version = ep["version"] + 1
    blueprint = json.loads(bp.model_dump_json())
    store.update("episodes", "id", episode_id, {"blueprint": blueprint, "studio": studio, "version": version,
                                               "updated_at": now()})
    store.insert("episode_versions", {"episode_id": episode_id, "version": version, "blueprint": blueprint,
                                      "studio": studio, "patch": patch, "note": note, "created_at": now()})
    return version


def shot_index(bp: SceneBlueprint, shot_id: str) -> int:
    for i, s in enumerate(bp.shots):
        if s.shot_id == shot_id:
            return i
    raise KeyError(shot_id)


def episode_shot_ids(bp: SceneBlueprint) -> List[str]:
    return [s.shot_id for s in bp.shots]

