#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Episode export: timeline -> native cut -> voice/music mix -> 9:16 or 16:9 delivery with exact
overlays and subtitles -> media inspection -> manifest + cost/quality report + contact sheet.

Gates
  final            every mandatory shot and line rendered and accepted (engine.readiness)
  draft_preview    everything rendered but something is not accepted: watermarked DRAFT
  draft_variant    a hook variant of an accepted cut (opening text card); labelled, never the release cut
  partial_preview  something is missing: a labelled slate stands in for each missing shot
                   (allow_draft=True only) — a missing shot is never silently dropped
An export never calls a model. Re-exporting an unchanged cut returns the earlier export (0 calls).

Truth in labelling
  native render size is recorded separately from the delivery size; scaling up is called an upscale.
  Lip sync is disclosed as unavailable unless the video backend declares audio-driven lip sync.
  Overlays and subtitles are deterministic (exact); generated characters/products are conditioned only.
"""

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import avtools, subtitles
from .domain import assets_by_asset_id, jobs_for_episode, load_blueprint, resolve_dep
from .engine import SPEECH_GAP_S, SPEECH_LEAD_S, shot_slot
from .hashing import hash_obj
from .store import new_id, now

DELIVERY = {"9:16": (1080, 1920), "16:9": (1920, 1080)}
FPS = 24
EXPORT_FORMAT_VERSION = 1


class ExportRefused(Exception):
    def __init__(self, message: str, readiness: Dict[str, Any]):
        super().__init__(message)
        self.readiness = readiness


def build_timeline(studio, episode_id: str) -> Dict[str, Any]:
    store, engine = studio.store, studio.engine
    ep = store.get("episodes", episode_id)
    bp = load_blueprint(ep)
    t, shots, lines = 0.0, [], []
    for shot in bp.shots:
        clip_job = resolve_dep(store, ep, f"clip:{shot.shot_id}")
        kf_job = resolve_dep(store, ep, f"kf:{shot.shot_id}")
        speech = engine.speech_durations(ep, shot.shot_id)
        slot = shot_slot(shot.staging_layer.duration_seconds, speech)
        clip = store.get("attempts", clip_job["selected_attempt"]) if clip_job and clip_job["selected_attempt"] else None
        kf = store.get("attempts", kf_job["selected_attempt"]) if kf_job and kf_job["selected_attempt"] else None
        cursor = t + SPEECH_LEAD_S
        for key, kind in (("narr", "narration"), ("tts", "dialogue")):
            j = resolve_dep(store, ep, f"{key}:{shot.shot_id}")
            if not j or j["state"] == "obsolete" or not j["selected_attempt"]:
                continue
            a = store.get("attempts", j["selected_attempt"])
            d = float((a["media_meta"] or {}).get("duration", 0))
            lines.append({"shot_id": shot.shot_id, "kind": kind, "text": j["spec"]["text"],
                          "speaker": j["spec"].get("speaker"), "start": round(cursor, 3), "end": round(cursor + d, 3),
                          "media_sha": a["media_sha"], "verdict": j["verdict"], "attempt": a["id"]})
            cursor += d + SPEECH_GAP_S
        shots.append({"shot_id": shot.shot_id, "start": round(t, 3), "duration": slot,
                      "planned_duration": shot.staging_layer.duration_seconds,
                      "clip_sha": clip["media_sha"] if clip else None, "clip_attempt": clip["id"] if clip else None,
                      "clip_verdict": clip_job["verdict"] if clip_job else None,
                      "clip_native": (clip["media_meta"] or {}) if clip else None,
                      "clip_request": clip["request"] if clip else None,
                      "keyframe_sha": kf["media_sha"] if kf else None, "keyframe_attempt": kf["id"] if kf else None,
                      "keyframe_verdict": kf_job["verdict"] if kf_job else None,
                      "dialogue": shot.narrative_layer.dialogue.text,
                      "speaker": shot.narrative_layer.dialogue.speaker_asset_id})
        t += slot
    return {"duration": round(t, 3), "shots": shots, "lines": lines}


def export_episode(studio, episode_id: str, aspect: Optional[str] = None, burn_subtitles: Optional[bool] = None,
                   allow_draft: bool = False, fit: str = "pad", hook: Optional[str] = None) -> Dict[str, Any]:
    avtools.require()
    store, engine, media = studio.store, studio.engine, studio.media
    ep = store.get("episodes", episode_id)
    project = store.get("projects", ep["project_id"])
    settings = project["settings"]
    aspect = aspect or settings.get("aspect", "9:16")
    if aspect not in DELIVERY:
        raise ValueError("aspect must be 9:16 or 16:9")
    sub_cfg = settings.get("subtitles", {})
    burn = sub_cfg.get("burn_in", True) if burn_subtitles is None else burn_subtitles
    ready = engine.readiness(episode_id)
    if ready["status"] == "final":
        status = "final"
    elif not allow_draft:
        raise ExportRefused(f"not exportable as final: {ready['reason']}. Pass allow_draft to get a labelled "
                            "preview.", ready)
    else:
        status = "draft_preview" if ready["status"] == "draft" else "partial_preview"

    overlays = list(settings.get("overlays") or [])
    if hook is not None:
        # hook variant: an exact opening text card from settings.hook_variants; always exported as a draft variant
        hv = {h["id"]: h for h in settings.get("hook_variants") or []}
        if hook not in hv:
            raise ValueError(f"unknown hook variant {hook!r} (have: {sorted(hv)})")
        overlays.insert(0, {"type": "text", "role": f"hook:{hook}", "text": hv[hook]["text"], "position": "center",
                            "start": 0, "end": float(hv[hook].get("seconds", 2.5)), "size": 84})
        if status == "final":
            status = "draft_variant"

    tl = build_timeline(studio, episode_id)
    out_w, out_h = DELIVERY[aspect]
    key = hash_obj({"v": EXPORT_FORMAT_VERSION, "timeline": [{k: s[k] for k in ("shot_id", "duration", "clip_sha")}
                                                             for s in tl["shots"]],
                    "lines": [{k: l[k] for k in ("start", "end", "media_sha", "text")} for l in tl["lines"]],
                    "status": status, "aspect": aspect, "burn": burn, "fit": fit, "audio": settings.get("audio"),
                    "subs": sub_cfg, "overlays": overlays,
                    "verdicts": [(i["key"], i["verdict"]) for i in ready["items"]]})
    prev = store.one("SELECT * FROM exports WHERE episode_id = ? AND input_hash = ? AND kind = 'video' "
                     "ORDER BY created_at DESC", (episode_id, key))
    if prev and all(Path(p).exists() for p in prev["files"].values()):
        prev["reused"] = True
        return prev

    export_id = new_id("exp")
    out = studio.home / "exports" / episode_id / export_id
    work = out / ".work"
    work.mkdir(parents=True, exist_ok=True)

    # ── native cut (video only) ────────────────────────────────────────────
    natives = [s["clip_native"] for s in tl["shots"] if s["clip_native"] and s["clip_native"].get("width")]
    nw, nh = (natives[0]["width"], natives[0]["height"]) if natives else (out_w // 3, out_h // 3)
    parts, holds = [], []
    for i, s in enumerate(tl["shots"]):
        part = work / f"part_{i:03d}.mp4"
        if s["clip_sha"]:
            src = media.path(s["clip_sha"])
            native_d = float(s["clip_native"].get("duration") or 0)
            pad = max(0.0, s["duration"] - native_d)
            if pad > 0.04:
                holds.append({"shot_id": s["shot_id"], "held_last_frame_s": round(pad, 3)})
            vf = (f"scale={nw}:{nh}:force_original_aspect_ratio=decrease,pad={nw}:{nh}:(ow-iw)/2:(oh-ih)/2,"
                  f"fps={FPS},tpad=stop_mode=clone:stop_duration={pad + 0.1:.3f}")
            avtools.run(["-i", str(src), "-an", "-vf", vf, "-t", f"{s['duration']:.3f}", "-c:v", "libx264",
                         "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(part)])
        else:
            label = avtools.write_text_file(work / f"missing_{i}.txt",
                                            f"MISSING SHOT\n{s['shot_id']}\nnot rendered — partial preview only")
            avtools.run(["-f", "lavfi", "-i", f"color=c=0x202020:s={nw}x{nh}:r={FPS}:d={s['duration']:.3f}",
                         "-vf", f"drawtext=fontfile={avtools.FONT}:textfile={avtools.esc_path(label)}:fontcolor=red:"
                                f"fontsize={max(14, nh // 18)}:x=(w-text_w)/2:y=(h-text_h)/2",
                         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(part)])
        parts.append(part)
    listing = work / "parts.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    native_cut = work / "native_cut.mp4"
    avtools.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(native_cut)])

    # ── audio: voice events + optional music with ducking ──────────────────
    audio_cfg = settings.get("audio", {})
    mix, audio_report = _mix_audio(studio, project, tl, work, audio_cfg, native_cut)

    # ── delivery render: aspect, overlays, subtitles, draft label ──────────
    cues = subtitles.build_cues(tl["lines"], int(sub_cfg.get("max_chars_per_line", 32 if aspect == "9:16" else 42)),
                                int(sub_cfg.get("max_lines", 2)))
    srt = subtitles.write_srt(cues, out / "subtitles.srt")
    ass = subtitles.write_ass(cues, out / "subtitles.ass", out_w, out_h, font_size=int(sub_cfg.get("font_size", 54)))
    sub_issues = subtitles.check_cues(cues, tl["duration"])

    if fit == "crop":
        geo = f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"
    else:
        geo = f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2"
    scale_factor = min(out_w / nw, out_h / nh) if fit != "crop" else max(out_w / nw, out_h / nh)
    chain = [geo, "setsar=1", f"fps={FPS}"]
    inputs = ["-i", str(native_cut), "-i", str(mix)]
    overlay_report, logo_inputs = _overlays(studio, project, overlays, work, out_w, out_h, chain)
    for p in logo_inputs:
        inputs += ["-i", str(p["path"])]
    if burn and cues:
        chain.append(f"subtitles={avtools.esc_path(ass)}:fontsdir={avtools.esc_path(Path(avtools.FONT).parent)}")
    if status != "final":
        lbl = avtools.write_text_file(work / "draft.txt", {"draft_preview": "DRAFT — NOT FOR RELEASE",
                                                             "draft_variant": f"HOOK VARIANT {hook} — DRAFT"}
                                      .get(status, "PARTIAL PREVIEW — SHOTS MISSING"))
        chain.append(f"drawtext=fontfile={avtools.FONT_BOLD}:textfile={avtools.esc_path(lbl)}:fontcolor=red@0.85:"
                     f"fontsize={out_h // 40}:x=(w-text_w)/2:y=h*0.03:box=1:boxcolor=black@0.5")
    filter_complex = f"[0:v]{','.join(chain)}[v0]"
    last = "v0"
    for k, p in enumerate(logo_inputs):
        idx = 2 + k
        filter_complex += (f";[{idx}:v]scale={p['w']}:-1[lg{k}];[{last}][lg{k}]overlay=x={p['x']}:y={p['y']}:"
                           f"enable='between(t,{p['start']},{p['end']})'[v{k + 1}]")
        last = f"v{k + 1}"
    film = out / (f"episode_{ep['number']:02d}_{aspect.replace(':', 'x')}" + (f"_hook{hook}" if hook else "") + ".mp4")
    avtools.run(inputs + ["-filter_complex", filter_complex, "-map", f"[{last}]", "-map", "1:a:0",
                          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{tl['duration']:.3f}",
                          "-movflags", "+faststart", str(film)])

    # ── inspection ─────────────────────────────────────────────────────────
    info = avtools.probe(film)
    blacks = avtools.black_segments(film)
    levels = avtools.audio_levels(film)
    checks = [
        {"check": "dimensions", "expected": f"{out_w}x{out_h}", "actual": f"{info['width']}x{info['height']}",
         "pass": (info["width"], info["height"]) == (out_w, out_h)},
        {"check": "frame_rate", "expected": FPS, "actual": info["fps"], "pass": abs((info["fps"] or 0) - FPS) < 0.01},
        {"check": "duration", "expected": tl["duration"], "actual": round(info["duration"], 3),
         "pass": abs(info["duration"] - tl["duration"]) <= 0.15},
        {"check": "audio_stream", "expected": ">= 1", "actual": info["audio_streams"], "pass": info["audio_streams"] >= 1},
        {"check": "no_black_segments", "expected": "none >= 0.5s", "actual": blacks, "pass": not blacks},
        {"check": "no_clipping", "expected": "peak <= -0.5 dBFS", "actual": levels["peak_db"],
         "pass": levels["peak_db"] is not None and levels["peak_db"] <= -0.5},
        {"check": "subtitle_timing", "expected": "no issues", "actual": sub_issues, "pass": not sub_issues},
        {"check": "every_shot_present", "expected": len(tl["shots"]),
         "actual": sum(1 for s in tl["shots"] if s["clip_sha"]), "pass": all(s["clip_sha"] for s in tl["shots"])},
    ]

    sheet = contact_sheet(studio, tl, out / "contact_sheet.png", ep["title"])
    manifest = _manifest(studio, ep, project, tl, ready, status, aspect, (nw, nh), (out_w, out_h), scale_factor, fit,
                         holds, audio_report, overlay_report, cues, sub_issues, burn, info, checks)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(_report_md(manifest), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    files = {"film": str(film), "srt": str(srt), "ass": str(ass), "contact_sheet": str(sheet),
             "manifest": str(out / "manifest.json"), "report": str(out / "report.md")}
    row = {"id": export_id, "episode_id": episode_id, "kind": "video", "status": status, "input_hash": key,
           "files": files, "manifest": manifest, "created_at": now()}
    store.insert("exports", row)
    store.event("info", f"export {status}: {film.name}", episode_id=episode_id)
    row["reused"] = False
    return row


def _mix_audio(studio, project, tl, work: Path, cfg: Dict[str, Any], native_cut: Path):
    media = studio.media
    dur = tl["duration"]
    inputs, filters, labels = [], [], []
    for k, ln in enumerate(tl["lines"]):
        inputs += ["-i", str(media.path(ln["media_sha"]))]
        ms = int(ln["start"] * 1000)
        filters.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[s{k}]")
        labels.append(f"[s{k}]")
    report: Dict[str, Any] = {"voice_events": len(tl["lines"]), "sources": [
        {"shot_id": l["shot_id"], "kind": l["kind"], "media_sha": l["media_sha"], "start": l["start"], "end": l["end"]}
        for l in tl["lines"]]}
    n = len(labels)
    if n:
        filters.append(f"{''.join(labels)}amix=inputs={n}:normalize=0:dropout_transition=0,apad,atrim=0:{dur:.3f}[voice]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        filters.append(f"[{len(inputs) // 2 - 1}:a]atrim=0:{dur:.3f}[voice]")
        n = 1
    music_sha = None
    if cfg.get("music_asset"):
        a = assets_by_asset_id(studio.store, project["id"]).get(cfg["music_asset"])
        music_sha = a["canonical_media"] if a else None
    nxt = len(inputs) // 2
    fade_in, fade_out = float(cfg.get("fade_in", 0.3)), float(cfg.get("fade_out", 0.8))
    if music_sha:
        inputs += ["-stream_loop", "-1", "-i", str(media.path(music_sha))]
        g = float(cfg.get("music_gain_db", -16))
        duck = float(cfg.get("duck_db", 10))
        ratio = max(2.0, min(20.0, 10 ** (duck / 20) * 2))
        filters.append(f"[voice]asplit=2[vo][sc]")
        filters.append(f"[{nxt}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{dur:.3f},volume={g}dB,"
                       f"afade=t=in:d={fade_in},afade=t=out:st={max(0, dur - fade_out):.3f}:d={fade_out}[mus]")
        filters.append(f"[mus][sc]sidechaincompress=threshold=0.03:ratio={ratio:.1f}:attack=20:release=300[duck]")
        filters.append("[vo][duck]amix=inputs=2:normalize=0[pre]")
        report["music"] = {"asset": cfg["music_asset"], "media_sha": music_sha, "gain_db": g, "ducking_db": duck,
                           "fade_in": fade_in, "fade_out": fade_out, "source": "supplied/authorized asset"}
        nxt += 1
    else:
        filters.append("[voice]anull[pre]")
        report["music"] = None
    if cfg.get("keep_clip_audio_db") is not None and avtools.probe(native_cut)["audio_streams"]:
        report["clip_audio"] = f"kept at {cfg['keep_clip_audio_db']} dB"
    else:
        report["clip_audio"] = "muted (generated clip audio is not used; voice comes from TTS)"
    filters.append(f"[pre]afade=t=in:d={min(fade_in, 0.1)},alimiter=limit=0.89:level=disabled,"
                   f"atrim=0:{dur:.3f}[out]")
    mix = work / "mix.wav"
    avtools.run(inputs + ["-filter_complex", ";".join(filters), "-map", "[out]", "-ac", "2", "-ar", "48000", str(mix)])
    report["levels"] = avtools.audio_levels(mix)
    report["limiter"] = "alimiter at -1 dBFS"
    return mix, report


def _overlays(studio, project, overlays: List[Dict[str, Any]], work: Path, w: int, h: int, chain: List[str]):
    """Exact text overlays (drawtext) and logo images (overlay filter). Returns (report, logo inputs)."""
    report, logos = [], []
    pos_y = {"top": "h*0.08", "center": "(h-text_h)/2", "bottom": "h*0.70", "lower_third": "h*0.62"}
    for k, o in enumerate(overlays):
        start, end = float(o.get("start", 0)), float(o.get("end", 9999))
        if o.get("type") == "text":
            tf = avtools.write_text_file(work / f"ov_{k}.txt", o["text"])
            size = int(o.get("size", 64) * h / 1920)
            chain.append(f"drawtext=fontfile={avtools.FONT_BOLD}:textfile={avtools.esc_path(tf)}:"
                         f"fontcolor={o.get('color', 'white')}:fontsize={size}:x=(w-text_w)/2:"
                         f"y={pos_y.get(o.get('position', 'top'), 'h*0.08')}:"
                         + (f"box=1:boxcolor={o.get('box_color', 'black@0.55')}:boxborderw={size // 3}:" if o.get("box", True) else "")
                         + f"enable='between(t,{start},{end})'")
            report.append({"type": "text", "role": o.get("role", "text"), "text": o["text"], "start": start, "end": end,
                           "guarantee": "deterministic (rendered exactly by ffmpeg drawtext)"})
        elif o.get("type") == "logo":
            a = assets_by_asset_id(studio.store, project["id"]).get(o["asset_id"])
            if not a or not a["canonical_media"]:
                raise ValueError(f"logo overlay: asset {o['asset_id']} has no supplied image")
            lw = int(o.get("width", 220) * w / 1080)
            margin = int(w * 0.05)
            x = {"top_left": f"{margin}", "top_right": f"W-w-{margin}", "bottom_right": f"W-w-{margin}",
                 "bottom_left": f"{margin}"}[o.get("position", "top_right")]
            y = f"{margin}" if o.get("position", "top_right").startswith("top") else f"H-h-{margin}"
            logos.append({"path": studio.media.path(a["canonical_media"]), "w": lw, "x": x, "y": y, "start": start,
                          "end": end})
            report.append({"type": "logo", "asset_id": o["asset_id"], "media_sha": a["canonical_media"],
                           "start": start, "end": end, "guarantee": "deterministic (supplied pixels composited, not generated)"})
    return report, logos


def contact_sheet(studio, tl: Dict[str, Any], path: Path, title: str, cols: int = 3) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    thumbs = []
    for s in tl["shots"]:
        p = studio.media.path(s["keyframe_sha"]) if s["keyframe_sha"] else None
        thumbs.append((s, Image.open(p).convert("RGB") if p else None))
    tw = 300
    th = int(tw * 16 / 9) if (thumbs and thumbs[0][1] and thumbs[0][1].height > thumbs[0][1].width) else int(tw * 9 / 16)
    cap = 90
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 16) + 16, 60 + rows * (th + cap + 16)), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(avtools.FONT, 15) if avtools.FONT else ImageFont.load_default()
    bold = ImageFont.truetype(avtools.FONT_BOLD, 22) if avtools.FONT_BOLD else font
    d.text((16, 16), title, fill=(240, 240, 240), font=bold)
    for i, (s, im) in enumerate(thumbs):
        x, y = 16 + (i % cols) * (tw + 16), 60 + (i // cols) * (th + cap + 16)
        if im:
            sheet.paste(im.resize((tw, th)), (x, y))
        else:
            d.rectangle([x, y, x + tw, y + th], outline=(200, 60, 60), width=3)
            d.text((x + 10, y + th // 2), "MISSING", fill=(220, 80, 80), font=bold)
        v = s["clip_verdict"] or s["keyframe_verdict"] or "-"
        d.text((x, y + th + 4), f"{s['shot_id']} · {s['duration']:.1f}s · {v}", fill=(200, 200, 200), font=font)
        if s["dialogue"]:
            import textwrap as tw_
            d.multiline_text((x, y + th + 24), "\n".join(tw_.wrap(f"“{s['dialogue']}”", 34)[:3]),
                             fill=(255, 220, 120), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path


def _manifest(studio, ep, project, tl, ready, status, aspect, native, out, scale, fit, holds, audio, overlays, cues,
              sub_issues, burn, info, checks) -> Dict[str, Any]:
    store = studio.store
    reg = studio.engine.registry(project)
    vprov = reg.get("video")
    job_ids = [j["id"] for j in jobs_for_episode(store, ep["id"])]
    q = ",".join("?" * len(job_ids))
    ledger = store.q(f"SELECT kind, state, SUM(amount_usd) AS usd, COUNT(*) AS n, GROUP_CONCAT(DISTINCT cost_basis) "
                     f"AS basis FROM ledger WHERE job_id IN ({q}) GROUP BY kind, state", tuple(job_ids)) if job_ids else []
    attempts = store.q(f"SELECT purpose, provider_state, verdict, COUNT(*) AS n FROM attempts WHERE job_id IN ({q}) "
                       f"GROUP BY purpose, provider_state, verdict", tuple(job_ids)) if job_ids else []
    committed = sum(r["usd"] or 0 for r in ledger if r["state"] == "committed")
    accepted_shots = sum(1 for s in tl["shots"] if s["clip_verdict"] == "accepted")
    gen_clips = sum(r["n"] for r in attempts if r["purpose"] == "generate")
    upscaled = scale > 1.0 + 1e-6
    return {
        "format": "cinecrew-studio-export", "format_version": EXPORT_FORMAT_VERSION, "status": status,
        "contains_fixture_media": ready["contains_fixture_media"],
        "fixture_notice": ("OFFLINE FIXTURE media: proves the pipeline plumbing, not any AI model integration"
                           if ready["contains_fixture_media"] else None),
        "project": {"id": project["id"], "name": project["name"], "mode": project["mode"]},
        "episode": {"id": ep["id"], "number": ep["number"], "title": ep["title"], "version": ep["version"]},
        "readiness": {k: ready[k] for k in ("status", "reason", "missing", "not_accepted")},
        "video": {"native_resolution": f"{native[0]}x{native[1]}", "delivery_resolution": f"{out[0]}x{out[1]}",
                  "aspect": aspect, "fit": fit, "scale_factor": round(scale, 3), "upscaled": upscaled,
                  "note": ("delivery is an UPSCALE of the native render, not native " + f"{out[0]}x{out[1]}")
                  if upscaled else "delivery at or below native resolution", "fps": FPS,
                  "held_last_frames": holds, "provider": vprov.name, "model": vprov.model},
        "lip_sync": ("available (backend declares audio-driven lip sync)" if vprov.caps.lip_sync else
                     "NOT AVAILABLE with the configured video backend: dialogue is TTS voice-over aligned to shot "
                     "timing; mouths are not synchronised to it"),
        "guarantees": {
            "deterministic": ["subtitles text and timing", "title/price/CTA text overlays", "logo pixels",
                              "voice timing (from measured waveforms)", "shot order and completeness"],
            "conditioned_only": ["character identity in generated frames", "product appearance in generated frames",
                                 "wardrobe / props / lighting adherence"],
        },
        "shots": [{k: s[k] for k in ("shot_id", "start", "duration", "planned_duration", "clip_attempt", "clip_sha",
                                     "clip_verdict", "keyframe_attempt", "keyframe_sha", "keyframe_verdict",
                                     "dialogue", "speaker")}
                  | {"adaptations": (s["clip_request"] or {}).get("adaptations", [])} for s in tl["shots"]],
        "lines": tl["lines"],
        "subtitles": {"cues": len(cues), "burned_in": burn, "issues": sub_issues, "files": ["subtitles.srt", "subtitles.ass"]},
        "audio": audio, "overlays": overlays,
        "costs": {"committed_usd": round(committed, 6), "ledger": ledger, "attempts": attempts,
                  "accepted_shots": accepted_shots, "generated_clip_attempts": gen_clips,
                  "cost_per_accepted_minute_usd": round(committed / (tl["duration"] / 60), 6) if tl["duration"] else None},
        "inspection": {"probe": info, "checks": checks, "all_pass": all(c["pass"] for c in checks)},
        "created_at": now(),
    }


def _report_md(m: Dict[str, Any]) -> str:
    lines = [f"# {m['project']['name']} — Ep {m['episode']['number']}: {m['episode']['title']}", "",
             f"**Status:** {m['status']}  ", f"**Readiness:** {m['readiness']['reason']}  "]
    if m["contains_fixture_media"]:
        lines.append(f"**{m['fixture_notice']}**  ")
    v = m["video"]
    lines += ["", "## Video", f"- native render: {v['native_resolution']} ({v['provider']} / {v['model']})",
              f"- delivery: {v['delivery_resolution']} {v['aspect']} ({v['fit']}, x{v['scale_factor']}) — {v['note']}",
              f"- lip sync: {m['lip_sync']}", "", "## Inspection (ffprobe / ffmpeg)"]
    for c in m["inspection"]["checks"]:
        lines.append(f"- {'PASS' if c['pass'] else 'FAIL'} {c['check']}: expected {c['expected']}, got {c['actual']}")
    lines += ["", "## Shots", "| shot | start | dur | clip verdict | keyframe verdict | line |", "|---|---|---|---|---|---|"]
    for s in m["shots"]:
        lines.append(f"| {s['shot_id']} | {s['start']:.2f} | {s['duration']:.2f} | {s['clip_verdict']} | "
                     f"{s['keyframe_verdict']} | {s['dialogue'] or ''} |")
    c = m["costs"]
    lines += ["", "## Cost", f"- committed: ${c['committed_usd']:.4f}",
              f"- generation attempts (clips+frames+voices): {c['generated_clip_attempts']}",
              f"- cost per accepted final minute: {c['cost_per_accepted_minute_usd']}", "",
              "## Guarantees", "- deterministic: " + "; ".join(m["guarantees"]["deterministic"]),
              "- conditioned only: " + "; ".join(m["guarantees"]["conditioned_only"]), ""]
    return "\n".join(lines)
