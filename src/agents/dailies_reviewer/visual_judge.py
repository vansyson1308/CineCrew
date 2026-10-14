#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
VisualJudgeAgent — VLM gate on generated media (part of the Dailies Reviewer).

DailiesReviewerAgent reviews the *prompts* before rendering. This agent reviews
the *result*: given a keyframe (one image) or a clip (a few sampled frames), it
scores fidelity to the shot spec and, on rejection, returns a revised prompt for
the next attempt. ProductionOperatorAgent.execute_jobs() runs the loop.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...engine import ConfigurableAgent
from ...schemas.blueprint import ShotBlueprint
from ...schemas.critic import VisualReview

_HERE = Path(__file__).resolve().parent


class VisualJudgeAgent:
    """Judge a generated keyframe / clip against its ShotBlueprint (visual_judge.yaml)."""

    def __init__(self):
        self._agent = ConfigurableAgent(config_path=str(_HERE / "visual_judge.yaml"))

    def review_keyframe(
        self,
        image_path: str,
        shot: ShotBlueprint,
        prompt: str,
        global_style: str = "",
        used_references: bool = False,
        reference_images: Optional[Dict[str, List[str]]] = None,
    ) -> VisualReview:
        """`reference_images`: {asset_id: [canonical sheet paths]} sent to the VLM ahead of the media."""
        return self._review([image_path], "a generated keyframe (single image)", shot, prompt, global_style,
                            reference_images, used_references)

    def review_clip(
        self,
        frame_paths: List[str],
        shot: ShotBlueprint,
        prompt: str,
        global_style: str = "",
        reference_images: Optional[Dict[str, List[str]]] = None,
    ) -> VisualReview:
        kind = (
            "frames sampled from a generated video clip, in time order"
            if len(frame_paths) > 1
            else "a contact sheet of frames sampled from a generated video clip (read left-to-right, top-to-bottom)"
        )
        return self._review(frame_paths, kind, shot, prompt, global_style, reference_images, False)

    # ------------------------------------------------------------------

    def _review(
        self,
        media: List[str],
        media_kind: str,
        shot: ShotBlueprint,
        prompt: str,
        global_style: str,
        reference_images: Optional[Dict[str, List[str]]],
        used_references: bool,
    ) -> VisualReview:
        refs, note = _reference_images_and_note(reference_images, len(media), used_references)
        return self._agent.run(
            images=refs + list(media),
            media_kind=media_kind,
            global_style=global_style or "(unspecified)",
            shot_context=_shot_context(shot),
            character_appearances=_appearances(shot),
            constraints=_constraints(shot),
            prompt=prompt,
            reference_note=note,
        )


def _reference_images_and_note(
    reference_images: Optional[Dict[str, List[str]]], n_media: int, used_references: bool
) -> Tuple[List[str], str]:
    """
    Canonical sheets go FIRST in the image list, labelled by asset id, so the VLM compares identity
    against the actual pictures rather than a sentence claiming they exist.
    """
    refs: List[str] = []
    labels: List[str] = []
    for asset_id, paths in (reference_images or {}).items():
        for p in paths:
            refs.append(p)
            labels.append(f"Image {len(refs)} = canonical reference sheet of {asset_id}")
    if not refs:
        note = (
            "No canonical reference images are attached; judge identity only against the appearance sheets (text)."
            if used_references else ""
        )
        return [], note
    first, last = len(refs) + 1, len(refs) + n_media
    media = f"Image {first}" if n_media == 1 else f"Images {first}-{last}"
    note = (
        "REFERENCE IMAGES (ground truth for identity, not media to judge):\n- "
        + "\n- ".join(labels)
        + f"\nThe media to judge is {media}. Check every character's face, hair, build and wardrobe against "
        "their reference sheet; identity drift is a material defect."
    )
    return refs, note


def _shot_context(shot: ShotBlueprint) -> str:
    n, s = shot.narrative_layer, shot.staging_layer
    lines = [f"shot_id: {shot.shot_id}"]
    if n:
        lines.append(f"action: {n.narrative_action}")
        lines.append(f"emotional beat: {n.emotional_beat}")
        if n.performance_emotion:
            lines.append(f"performance: {n.performance_emotion} ({n.performance_intensity or '-'}/10)")
        if n.dialogue.has_dialogue and n.dialogue.text:
            lines.append(f"dialogue ({n.dialogue.speaker_asset_id}): \"{n.dialogue.text}\"")
    if s:
        cam = s.camera
        lines.append(f"camera: scale={cam.shot_scale} angle={cam.angle} movement={cam.movement}")
        lines.append(f"lighting: {s.lighting}")
        lines.append(f"location: {s.environment_id}")
        if s.entities:
            lines.append("entities: " + "; ".join(
                f"{e.asset_id} ({e.position or '?'}, {e.action_state or 'idle'})" for e in s.entities
            ))
    return "\n".join(lines)


def _appearances(shot: ShotBlueprint) -> str:
    r = shot.render_layer
    if not r or not r.image.character_appearances:
        return "(none)"
    return "\n".join(f"- {k}: {v}" for k, v in r.image.character_appearances.items())


def _constraints(shot: ShotBlueprint) -> str:
    s = shot.staging_layer
    if not s or not s.consistency_constraints:
        return "(none)"
    return "\n".join(f"- {c}" for c in s.consistency_constraints)


__all__ = ["VisualJudgeAgent", "VisualReview"]
