#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Subtitles from MEASURED speech timings: SRT + ASS, line wrapping, safe zones and timing checks.

Cues come from the export timeline (where each TTS waveform actually sits), not from a
words-per-second estimate. Text is UTF-8 throughout; Vietnamese diacritics need a font that has
them (DejaVu Sans is used for burn-in).
"""

import textwrap
from pathlib import Path
from typing import Any, Dict, List, Tuple

MIN_CUE_S = 0.8
MAX_CPS = 20.0          # characters per second a viewer can read comfortably (incl. Vietnamese diacritics)


def wrap(text: str, max_chars: int, max_lines: int) -> List[List[str]]:
    """Split text into cues of at most `max_lines` lines of `max_chars` characters (word boundaries)."""
    lines = textwrap.wrap(" ".join(text.split()), width=max_chars, break_long_words=True) or [""]
    return [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]


def build_cues(lines: List[Dict[str, Any]], max_chars: int, max_lines: int) -> List[Dict[str, Any]]:
    """
    lines: [{start, end, text, speaker}] in seconds (from the timeline). A long line is split into
    consecutive cues, each getting time in proportion to its character count.
    """
    cues = []
    for ln in lines:
        chunks = wrap(ln["text"], max_chars, max_lines)
        total = sum(len(" ".join(c)) for c in chunks) or 1
        t = ln["start"]
        span = ln["end"] - ln["start"]
        for c in chunks:
            d = span * len(" ".join(c)) / total
            cues.append({"start": round(t, 3), "end": round(t + d, 3), "lines": c, "speaker": ln.get("speaker"),
                         "kind": ln.get("kind", "dialogue")})
            t += d
    return cues


def check_cues(cues: List[Dict[str, Any]], film_duration: float) -> List[Dict[str, Any]]:
    issues = []
    for i, c in enumerate(cues):
        d = c["end"] - c["start"]
        chars = len(" ".join(c["lines"]))
        if d < MIN_CUE_S:
            issues.append({"cue": i + 1, "issue": f"shorter than {MIN_CUE_S}s ({d:.2f}s)"})
        if d > 0 and chars / d > MAX_CPS:
            issues.append({"cue": i + 1, "issue": f"reading speed {chars / d:.1f} cps > {MAX_CPS}"})
        if c["end"] > film_duration + 0.05:
            issues.append({"cue": i + 1, "issue": f"ends after the film ({c['end']:.2f}s > {film_duration:.2f}s)"})
        if i and c["start"] < cues[i - 1]["end"] - 1e-3:
            issues.append({"cue": i + 1, "issue": "overlaps the previous cue"})
    return issues


def _ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_ass(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def write_srt(cues: List[Dict[str, Any]], path: Path) -> Path:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_ts_srt(c['start'])} --> {_ts_srt(c['end'])}\n" + "\n".join(c["lines"]) + "\n")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def safe_margin(width: int, height: int) -> Tuple[int, int]:
    """(vertical, horizontal) margins keeping captions clear of platform UI (bottom ~15% on 9:16)."""
    if height > width:
        return int(height * 0.16), int(width * 0.07)
    return int(height * 0.08), int(width * 0.08)


def write_ass(cues: List[Dict[str, Any]], path: Path, width: int, height: int, font: str = "DejaVu Sans",
              font_size: int = 54) -> Path:
    mv, mh = safe_margin(width, height)
    fs = int(font_size * height / 1920) if height > width else int(font_size * height / 1080 * 0.8)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Dialogue,{font},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{max(2, fs // 18)},1,2,{mh},{mh},{mv},1
Style: Narration,{font},{fs},&H0000E5FF,&H000000FF,&H00000000,&H80000000,0,1,0,0,100,100,0,0,1,{max(2, fs // 18)},1,2,{mh},{mh},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for c in cues:
        style = "Narration" if c.get("kind") == "narration" else "Dialogue"
        text = "\\N".join(l.replace("{", "(").replace("}", ")") for l in c["lines"])
        rows.append(f"Dialogue: 0,{_ts_ass(c['start'])},{_ts_ass(c['end'])},{style},{c.get('speaker') or ''},0,0,0,,{text}")
    path.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return path
