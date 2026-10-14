#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
FilmDSL schema migrations. Documents without `schema_version` are 1.0 (upstream 3eac773).

1.0 -> 1.1  additive: schema_version, draft_film_path, assembly_report. A 1.0 `final_film_path`
            was produced by a loop that could stitch rejected or missing clips, so it is moved to
            `draft_film_path` (its readiness is unknown) and recorded in assembly_report.
"""

import copy
from typing import Any, Callable, Dict, List, Tuple

CURRENT = "1.1"


def _v10_to_v11(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.setdefault("draft_film_path", None)
    if doc.get("final_film_path") and not doc.get("assembly_report"):
        doc["draft_film_path"] = doc["final_film_path"]
        doc["final_film_path"] = None
        doc["assembly_report"] = {"status": "unknown", "reason": "migrated from schema 1.0: the film was assembled "
                                  "without readiness checks; kept as a draft"}
    doc.setdefault("assembly_report", None)
    doc["schema_version"] = "1.1"
    return doc


_STEPS: List[Tuple[str, str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [("1.0", "1.1", _v10_to_v11)]


def migrate_blueprint(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(doc)
    version = out.get("schema_version", "1.0")
    for src, dst, fn in _STEPS:
        if version == src:
            out = fn(out)
            version = dst
    if version != CURRENT:
        raise ValueError(f"unknown FilmDSL schema_version {version!r} (this build reads up to {CURRENT})")
    return out
