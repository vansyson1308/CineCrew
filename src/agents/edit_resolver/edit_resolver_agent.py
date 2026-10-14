#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
EditResolverAgent — turns a director's natural-language revision request into a typed EditPatch.

Used by CineCrew Studio in live mode when the deterministic rules in src/studio/revisions.py cannot read the
request. The patch is never applied directly: the studio previews impact, cost and lock conflicts first.
"""

from pathlib import Path
from typing import Any, Dict

from ...engine import ConfigurableAgent

_HERE = Path(__file__).resolve().parent


class EditResolverAgent:
    def __init__(self):
        self._agent = ConfigurableAgent(config_path=str(_HERE / "edit_resolver.yaml"))

    def run(self, text: str, blueprint, assets: Dict[str, Dict[str, Any]]):
        from ...studio.revisions import SHOT_FIELDS, ASSET_FIELDS
        shots = []
        for s in blueprint.shots:
            n, st = s.narrative_layer, s.staging_layer
            d = n.dialogue if n else None
            shots.append({"shot_id": s.shot_id, "action": n.narrative_action if n else "",
                          "line": d.text if d is not None and d.has_dialogue else "",
                          "lighting": getattr(st, "lighting", "") if st else ""})
        asset_rows = [{"asset_id": k, "kind": v["kind"], "name": v["name"]} for k, v in assets.items()]
        patch = self._agent.run(request=text, shots=shots, assets=asset_rows,
                                shot_fields=", ".join(SHOT_FIELDS), asset_fields=", ".join(ASSET_FIELDS))
        patch.source_text = text
        return patch
