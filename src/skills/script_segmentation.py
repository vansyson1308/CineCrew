#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Source-traceable script segmentation (replaces the silent `script[:limit]` slice).

segment_script()   split a script into scenes (headings: INT./EXT./SCENE/CẢNH/Chapter ...)
                   or, failing that, paragraphs; every segment keeps its [start, end) span in the
                   original text.
select_within()    pick whole segments from the start that fit a character budget and report
                   coverage: covered/omitted chars and every omitted span with an extractive summary.

Deterministic, no model calls. The summary of omitted material is EXTRACTIVE (first sentence),
labelled as such; a model-written hierarchical summary can be layered on top by the caller.
"""

import re
from typing import Any, Dict, List

_HEADING = re.compile(
    r"^\s*(?:INT\.|EXT\.|INT/EXT\.|I/E\.|SCENE\b|CẢNH\b|Cảnh\b|CHAPTER\b|Chapter\b|CHƯƠNG\b|Chương\b|#{1,3}\s)",
    re.MULTILINE,
)
_PARA = re.compile(r"\n\s*\n")


def segment_script(text: str) -> List[Dict[str, Any]]:
    """[{index, start, end, heading, text}] covering `text` in order (separators belong to no segment)."""
    if not text:
        return []
    starts = [m.start() for m in _HEADING.finditer(text)]
    if len(starts) >= 2:
        if starts[0] > 0 and text[: starts[0]].strip():
            starts = [0] + starts
        bounds = list(zip(starts, starts[1:] + [len(text)]))
    else:
        bounds, pos = [], 0
        for m in _PARA.finditer(text):
            bounds.append((pos, m.start()))
            pos = m.end()
        bounds.append((pos, len(text)))
    segs = []
    for start, end in bounds:
        # trim surrounding whitespace but keep exact offsets into the source
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            continue
        body = text[start:end]
        segs.append({"index": len(segs), "start": start, "end": end,
                     "heading": body.splitlines()[0][:80], "text": body})
    return segs


def _first_sentence(text: str, limit: int = 160) -> str:
    one = " ".join(text.split())
    m = re.search(r"(.+?[.!?。…])(\s|$)", one)
    s = m.group(1) if m else one
    return s if len(s) <= limit else s[: limit - 1] + "…"


def select_within(text: str, max_len: int) -> Dict[str, Any]:
    """
    Whole segments from the start of `text` while the covered prefix stays <= max_len. If even the
    first segment is longer, it is cut at the last whitespace before max_len (reported as partial).
    Returns {script_segment, coverage_report}.
    """
    segs = segment_script(text)
    if len(text) <= max_len:
        return {"script_segment": text, "coverage_report": {
            "total_chars": len(text), "covered_chars": len(text), "omitted_chars": 0,
            "coverage_ratio": 1.0, "segments_total": len(segs), "segments_covered": len(segs),
            "partial_segment": None, "omitted_segments": [], "summary_kind": None}}
    covered_end, n_cov, partial = 0, 0, None
    for s in segs:
        if s["end"] <= max_len:
            covered_end, n_cov = s["end"], n_cov + 1
        else:
            break
    if n_cov == 0:
        cut = text.rfind(" ", 0, max_len)
        covered_end = cut if cut > max_len // 2 else max_len
        partial = {"index": 0, "start": 0, "end": segs[0]["end"] if segs else len(text), "covered_to": covered_end}
    omitted = []
    if partial:
        rest = text[covered_end:partial["end"]]
        omitted.append({"index": 0, "start": covered_end, "end": partial["end"], "chars": len(rest),
                        "heading": "(rest of segment 0)", "summary": _first_sentence(rest)})
    for s in segs[n_cov + (1 if partial else 0):]:
        omitted.append({"index": s["index"], "start": s["start"], "end": s["end"], "chars": s["end"] - s["start"],
                        "heading": s["heading"], "summary": _first_sentence(s["text"])})
    return {"script_segment": text[:covered_end], "coverage_report": {
        "total_chars": len(text), "covered_chars": covered_end, "omitted_chars": len(text) - covered_end,
        "coverage_ratio": round(covered_end / len(text), 4), "segments_total": len(segs),
        "segments_covered": n_cov, "partial_segment": partial, "omitted_segments": omitted,
        "summary_kind": "extractive_first_sentence"}}


__all__ = ["segment_script", "select_within"]
