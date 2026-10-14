#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Comic carousel from the accepted storyboard: PNG slides (1080x1350, 4:5) + one PDF.

Derived content, no AI calls: panels are the accepted keyframes, layouts are explicit, speech
bubbles and narration captions are placed deterministically with the line text verbatim. An
unchanged storyboard re-exports from cache. Not a manga production tool.
"""

import json
import textwrap
from pathlib import Path
from typing import Any, Dict, List

from . import avtools
from .domain import assets_by_asset_id, load_blueprint, resolve_dep
from .export import ExportRefused
from .hashing import hash_obj
from .store import new_id, now

SLIDE = (1080, 1350)
LAYOUTS = {"two_stack": 2, "single": 1}
COMIC_VERSION = 1


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    path = avtools.FONT_BOLD if bold else avtools.FONT
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


def _bubble(draw, box, text: str, speaker: str, right: bool, narration: bool = False):
    x0, y0, x1, y1 = box
    max_w = int((x1 - x0) * 0.62)
    size = 34
    while size >= 20:
        f = _font(size)
        lines = textwrap.wrap(text, max(8, int(max_w / (size * 0.55))))
        h = len(lines) * int(size * 1.25) + (size if speaker else 0) + 28
        if h < (y1 - y0) * 0.45:
            break
        size -= 2
    tw = max(draw.textlength(l, font=f) for l in lines) if lines else 0
    bw, bh = int(max(tw, draw.textlength(speaker or "", font=_font(int(size * 0.8), True))) + 40), h
    bx = x1 - bw - 24 if right else x0 + 24
    by = y0 + 24
    if narration:
        draw.rectangle([bx, by, bx + bw, by + bh], fill=(255, 236, 160), outline=(30, 30, 30), width=3)
    else:
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=28, fill=(255, 255, 255), outline=(20, 20, 20),
                               width=4)
        tail_x = bx + (bw - 70 if right else 70)
        draw.polygon([(tail_x - 18, by + bh - 2), (tail_x + 18, by + bh - 2), (tail_x + (25 if right else -25),
                     by + bh + 46)], fill=(255, 255, 255), outline=(20, 20, 20))
        draw.line([(tail_x - 16, by + bh - 2), (tail_x + 16, by + bh - 2)], fill=(255, 255, 255), width=6)
    ty = by + 14
    if speaker:
        draw.text((bx + 20, ty), speaker, fill=(160, 30, 30), font=_font(int(size * 0.8), True))
        ty += size
    for l in lines:
        draw.text((bx + 20, ty), l, fill=(15, 15, 15), font=f)
        ty += int(size * 1.25)


def export_comic(studio, episode_id: str, layout: str = "two_stack", allow_draft: bool = False) -> Dict[str, Any]:
    from PIL import Image, ImageDraw
    store, media = studio.store, studio.media
    ep = store.get("episodes", episode_id)
    bp = load_blueprint(ep)
    assets = assets_by_asset_id(store, ep["project_id"])
    panels, problems = [], []
    for s in bp.shots:
        kf = resolve_dep(store, ep, f"kf:{s.shot_id}")
        a = store.get("attempts", kf["selected_attempt"]) if kf and kf["selected_attempt"] else None
        if not a:
            problems.append(f"{s.shot_id}: no keyframe")
            continue
        if kf["verdict"] != "accepted":
            problems.append(f"{s.shot_id}: keyframe {kf['verdict']}")
        d = s.narrative_layer.dialogue
        panels.append({"shot_id": s.shot_id, "sha": a["media_sha"], "verdict": kf["verdict"],
                       "speaker": assets[d.speaker_asset_id]["name"] if d.speaker_asset_id in assets else None,
                       "line": d.text if d.has_dialogue else None,
                       "narration": ep["studio"]["shots"][s.shot_id].get("narration")})
    ready = {"status": "blocked" if any("no keyframe" in p for p in problems) else "draft" if problems else "final",
             "reason": "; ".join(problems) or "every panel accepted", "items": []}
    if problems and not allow_draft:
        raise ExportRefused(f"storyboard not ready for a carousel: {ready['reason']}", ready)
    key = hash_obj({"v": COMIC_VERSION, "layout": layout, "panels": panels, "title": ep["title"]})
    prev = store.one("SELECT * FROM exports WHERE episode_id = ? AND kind = 'comic' AND input_hash = ?", (episode_id, key))
    if prev and all(Path(p).exists() for p in prev["files"].values()):
        prev["reused"] = True
        return prev
    xid = new_id("cmc")
    out = studio.home / "exports" / episode_id / xid
    out.mkdir(parents=True, exist_ok=True)
    W, H = SLIDE
    slides: List[Image.Image] = []
    # cover
    cover = Image.new("RGB", SLIDE, (250, 245, 235))
    d = ImageDraw.Draw(cover)
    if panels:
        im = Image.open(media.path(panels[0]["sha"])).convert("RGB")
        im = _fit(im, W - 120, H - 420)
        cover.paste(im, ((W - im.width) // 2, 300))
    project = store.get("projects", ep["project_id"])
    d.text((60, 70), project["name"], fill=(160, 30, 30), font=_font(44, True))
    d.multiline_text((60, 140), "\n".join(textwrap.wrap(f"Tập {ep['number']}: {ep['title']}", 26)), fill=(20, 20, 20),
                     font=_font(62, True))
    slides.append(cover)
    per = LAYOUTS.get(layout, 2)
    gutter = 24
    for i in range(0, len(panels), per):
        slide = Image.new("RGB", SLIDE, (250, 245, 235))
        d = ImageDraw.Draw(slide)
        group = panels[i:i + per]
        ph = (H - gutter * (per + 1) - 50) // per
        for k, pnl in enumerate(group):
            box = (gutter, gutter + k * (ph + gutter), W - gutter, gutter + k * (ph + gutter) + ph)
            im = _cover(Image.open(media.path(pnl["sha"])).convert("RGB"), box[2] - box[0], box[3] - box[1])
            slide.paste(im, (box[0], box[1]))
            d.rectangle(box, outline=(20, 20, 20), width=6)
            if pnl["narration"]:
                _bubble(d, box, pnl["narration"], "", right=False, narration=True)
            if pnl["line"]:
                _bubble(d, (box[0], box[1] + (int(ph * 0.4) if pnl["narration"] else 0), box[2], box[3]),
                        pnl["line"], pnl["speaker"] or "", right=(i // per + k) % 2 == 1)
            if pnl["verdict"] != "accepted":
                d.text((box[0] + 16, box[3] - 50), f"DRAFT ({pnl['verdict']})", fill=(220, 30, 30), font=_font(30, True))
        d.text((W - 120, H - 48), f"{len(slides) + 1}", fill=(90, 90, 90), font=_font(28))
        slides.append(slide)
    files = {}
    for n, sl in enumerate(slides, 1):
        p = out / f"slide_{n:02d}.png"
        sl.save(p)
        files[f"slide_{n:02d}"] = str(p)
    pdf = out / "carousel.pdf"
    slides[0].save(pdf, save_all=True, append_images=slides[1:], resolution=150)
    files["pdf"] = str(pdf)
    manifest = {"format": "cinecrew-studio-comic", "layout": layout, "slide_size": f"{W}x{H}", "panels": panels,
                "status": "final" if not problems else "draft_preview", "ai_calls": 0,
                "contains_fixture_media": any(media.get(p["sha"])["fixture"] for p in panels),
                "text_guarantee": "speech/caption text is rendered verbatim by Pillow (deterministic)"}
    (out / "comic_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    files["manifest"] = str(out / "comic_manifest.json")
    row = {"id": xid, "episode_id": episode_id, "kind": "comic", "status": manifest["status"], "input_hash": key,
           "files": files, "manifest": manifest, "created_at": now()}
    store.insert("exports", row)
    row["reused"] = False
    return row


def _fit(im, w, h):
    r = min(w / im.width, h / im.height)
    return im.resize((int(im.width * r), int(im.height * r)))


def _cover(im, w, h):
    r = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1))
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))
