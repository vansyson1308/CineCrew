#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
ffmpeg / ffprobe helpers for the studio (deterministic, no model calls).

Unlike src/adapters/media_tools.py (which degrades silently for the research CLI), these raise
AVError on failure: an export must never pretend a step worked.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

FONT = next((p for p in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
) if Path(p).exists()), None)
FONT_BOLD = next((p for p in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
) if Path(p).exists()), FONT)


class AVError(RuntimeError):
    pass


def require() -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise AVError("ffmpeg and ffprobe are required (install ffmpeg)")


def run(args: List[str], timeout: int = 900) -> str:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise AVError(f"ffmpeg failed ({p.returncode}): {p.stderr.strip()[-800:]}")
    return p.stderr


def run_capture(args: List[str], timeout: int = 900) -> str:
    """Run ffmpeg keeping info-level stderr (for analysis filters)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise AVError(f"ffmpeg failed ({p.returncode}): {p.stderr.strip()[-800:]}")
    return p.stderr


def probe(path: Path) -> Dict[str, Any]:
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AVError(f"ffprobe failed on {path}: {p.stderr.strip()[-300:]}")
    data = json.loads(p.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    fps = None
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = round(float(n) / float(d), 3) if float(d) else None
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "width": int(v["width"]) if v else None,
        "height": int(v["height"]) if v else None,
        "fps": fps,
        "video_codec": v.get("codec_name") if v else None,
        "audio_streams": len(a),
        "audio": [{"codec": s.get("codec_name"), "sample_rate": int(s.get("sample_rate", 0) or 0),
                   "channels": s.get("channels")} for s in a],
        "size_bytes": int(data.get("format", {}).get("size", 0) or 0),
    }


def duration(path: Path) -> float:
    return probe(path)["duration"]


def black_segments(path: Path, min_d: float = 0.5) -> List[Dict[str, float]]:
    err = run_capture(["-i", str(path), "-vf", f"blackdetect=d={min_d}:pix_th=0.10", "-an", "-f", "null", "-"])
    out = []
    for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", err):
        out.append({"start": float(m.group(1)), "end": float(m.group(2)), "duration": float(m.group(3))})
    return out


def audio_levels(path: Path) -> Dict[str, Optional[float]]:
    err = run_capture(["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    mean = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    peak = re.search(r"max_volume: (-?[\d.]+) dB", err)
    return {"mean_db": float(mean.group(1)) if mean else None, "peak_db": float(peak.group(1)) if peak else None}


def write_text_file(path: Path, text: str) -> Path:
    """drawtext reads text from a file: no escaping of quotes/colons/Vietnamese diacritics needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def esc_path(p: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
