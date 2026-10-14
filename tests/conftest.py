# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""Shared pytest fixtures. Tests never touch a real model backend."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# No test may reach a paid backend: blank keys, and point every base URL at a closed port.
for _k in ("LLM_API_KEY", "OPENAI_API_KEY", "T2I_API_KEY", "VIDEO_API_KEY", "TTS_API_KEY"):
    os.environ[_k] = ""
for _k in ("LLM_BASE_URL", "T2I_BASE_URL", "VIDEO_BASE_URL", "TTS_BASE_URL"):
    os.environ[_k] = "http://127.0.0.1:9/v1"
os.environ.setdefault("TRACE_LOGS", "0")

import pytest  # noqa: E402

from src.schemas.assets import AssetLibrary, CharacterAsset, LocationAsset, NarrativeContext  # noqa: E402
from src.schemas.blueprint import (  # noqa: E402
    BlueprintMetadata, DialogueDef, NarrativeLayer, RenderLayer, SceneBlueprint, ShotBlueprint, StagingLayer,
)


def make_shot(shot_id: str, duration: float, line: str = None, speaker: str = "char_an") -> ShotBlueprint:
    shot = ShotBlueprint(
        shot_id=shot_id,
        narrative_layer=NarrativeLayer(
            narrative_action=f"action of {shot_id}",
            emotional_beat="calm",
            dialogue=DialogueDef(has_dialogue=bool(line), speaker_asset_id=speaker if line else None, text=line),
        ),
        staging_layer=StagingLayer(duration_seconds=duration, environment_id="loc_alley"),
    )
    rl = RenderLayer()
    rl.image.resolved_t2i = f"keyframe prompt {shot_id}"
    rl.video.resolved_i2v = f"motion prompt {shot_id}"
    shot.render_layer = rl
    return shot


@pytest.fixture
def library() -> AssetLibrary:
    return AssetLibrary(
        project_title="test",
        global_style="flat test style",
        narrative_context=NarrativeContext(time_period="now", global_mood="calm"),
        characters=[CharacterAsset(id="char_an", name="An", description="young courier", voice_preset_id="alloy")],
        locations=[LocationAsset(id="loc_alley", name="Alley", type="EXT", visual_style="old Hanoi alley")],
    )


@pytest.fixture
def blueprint_factory():
    def _make(shots):
        return SceneBlueprint(
            blueprint_id="bp_test",
            metadata=BlueprintMetadata(project_name="test", target_resolution="640x360", global_fps=16),
            global_style="flat test style",
            shots=shots,
        )
    return _make
