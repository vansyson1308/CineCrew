# CineCrew Studio — Milestone 0 audit

Audit date: 2026-10-14. Read-only preflight of the connected fork, before any change.

## Pinned baseline

| Item | Value |
|------|-------|
| Fork | `vansyson1308/CineCrew` (origin), branch `main` |
| Commit | `3eac773704088f65ae2e24054c48fccbd4172f2d` (same as the upstream HEAD the brief reviewed) |
| Working tree | clean, no local user changes to preserve |
| Upstream remote | not configured in this checkout; nothing is pushed upstream |
| License | Apache-2.0 (`LICENSE`); file header block required on every `.py` (AGENTS.md) |
| Instruction files | `AGENTS.md` (authoritative), `CLAUDE.md` → `@AGENTS.md` |
| Tests | none upstream (`AGENTS.md`: "There is no test suite") |
| CI | `.github/workflows/pages.yml` only (project website), no test job |
| Python deps | `openai>=2`, `instructor>=1`, `pydantic>=2`, `PyYAML`, `Jinja2` (resolved here: openai 3.3.0, instructor 1.17.0, pydantic 2.14.1) |
| Runtime | Python 3.13 in this container (pyproject says >=3.10), ffmpeg 6.1.1 with libx264, libass, freetype, fontconfig; DejaVu fonts (Vietnamese glyphs OK); Node 22; Chromium for Playwright |
| Code size | ~6k lines Python in `src/`, 8 agent packages, 13 shipped example runs under `examples/` |

Baseline reproduction without network (the AGENTS.md sanity checks):

```
python -c "import src.pipeline, src.adapters"            # OK
ProductionOperatorAgent().build_from_blueprint(bp, lib)  # OK on all 13 examples
```

The full CLI (`run_dataset_story.py`) needs a real LLM key for every stage, so it was not run: the
external budget is zero and no key is configured. Planning stages are therefore covered by fixtures
and by the shipped example blueprints, not by a fresh LLM run.

## Findings from the brief, rechecked

Classification: **Defect** = confirmed wrong behaviour, reproduced by a test that fails on 3eac773.
**Incomplete** = feature the brief expects that the code does not implement (not a bug in what exists).
**Doc claim** = documented intent that is not backed by code.

| # | Finding | Verdict | Evidence on 3eac773 |
|---|---------|---------|----------------------|
| F1 | Default run plans rather than renders | **Confirmed (by design)** | `configs/pipeline_settings.yaml`: `execute: false`, `generate_references: false`, `design_voices: false`. Not a defect; the Studio adds explicit `offline / plan / render / export` modes. |
| F2 | One-segment dialogue is mislabeled and loses the line | **Defect, worse than reported** | `production_operator_agent.py` slices `["prelude","dialogue_core","afterglow"][:n]`, so a 1-segment split is labelled `prelude` and `dialogue`/`voice` are dropped. Across the 13 shipped examples, **11 of 12 dialogue shots lose their line** in the job batch (only `great_gatsby_fireworks_smile` survives). Tests: `test_short_dialogue_shot_keeps_line_and_voice`, `test_every_example_dialogue_line_reaches_a_job`. |
| F3 | Identity review lacks the actual reference images | **Defect** | `visual_judge.py` only adds a text note ("Picture N refers to them") when `used_references=True`; `images=[generated]` never includes the sheets, so the VLM is told to compare against pictures it never receives. Tests: `test_keyframe_judge_receives_reference_images`, `test_visual_judge_attaches_reference_images_to_request`. |
| F4 | Rejected or unreviewed media can advance | **Defect** | `_render_keyframe` keeps the last image after all rejections and keeps it on judge exception (`break`); `_render_clip` keeps `result.video_path` after a final rejection, after missing frames, and after judge failure. Nothing records a verdict state. Tests: `test_rejected_clip_is_not_assembled_as_final`, `test_judge_error_is_not_acceptance`. |
| F5 | Partial generation can still assemble | **Defect** | `execute_jobs` stitches when `done > 0`; `concat_clips` silently filters missing paths. A 2-shot film with one failed shot is assembled into `film.mp4` and recorded as `final_film_path`. Test: `test_failed_shot_blocks_final_assembly`. |
| F6 | Prompt provenance can be inaccurate | **Defect** | Keyframe: `prompt = review.revised_prompt` runs after the last rejection, then that unrendered prompt is written to `resolved_t2i/ti2i`. Clip: `job.prompt` is overwritten the same way and `video_jobs.json` is re-saved with it. Also `job.seed += 1` mutates the job. Tests: `test_kept_keyframe_prompt_is_the_prompt_that_rendered_it`, `test_kept_clip_prompt_is_the_prompt_that_rendered_it`. |
| F7 | No durable recovery for rendering | **Incomplete** | `VideoClient.generate` keeps the remote `video_id` only in memory; results are written after the whole sequential loop returns; a local `TimeoutError` abandons a job that may still be running remotely, and a rerun submits again (paid twice). No checkpoint, resume, cancel, rate-limit or budget logic exists. |
| F8 | Long input is silently sliced | **Defect** | `pipeline.py` sets `script_segment = script_content[:10000]` with no warning or record of what was dropped. Test: `test_script_truncation_is_reported`. |
| F9 | Sound metadata is not sound production | **Incomplete** | `AudioTrack` stores intent only; `src/adapters` has no TTS, subtitle or mixing code; `concat_clips` adds silence to clips without audio. Lip sync is only a prompt phrase. |

Additional observations (not in the brief):

| # | Observation | Verdict |
|---|-------------|---------|
| A1 | `VideoClient.submit` always sends `negative_prompt`, `fps`, `frame_num`, `duration`, `seed`, dialogue fields through `extra_body`. A strict provider may reject unknown fields; nothing declares which ones a backend accepts. | Incomplete (provider contract) |
| A2 | `sora*` durations are snapped to 4/8/12 s, so the rendered clip length differs from the planned segment length, but the plan keeps the planned duration. | Defect (timing truth); handled by recording native duration per attempt |
| A3 | `T2I_KEYFRAME_SIZE` defaults to `VIDEO_SIZE` (1280x720) which `gpt-image-1` does not accept (1024x1024, 1536x1024, 1024x1536). | Config hazard; documented in provider capabilities |
| A4 | Blueprint and asset schemas carry no version field, so there is no migration path. | Incomplete; Studio adds `schema_version` + migration tests |

Baseline test run on 3eac773 (all ten new regression tests fail for the reasons above):
`docs/studio/evidence/baseline_3eac773_regressions.txt`.

## What exists and is worth keeping

- FilmDSL (`SceneBlueprint`, four layers) and `AssetLibrary` with stable IDs: the right contract for continuity. Kept unchanged except additive, versioned fields.
- `ConfigurableAgent` (YAML prompt + Pydantic schema + trace) and the eight crew agents: kept; the Studio calls the existing pipeline for planning.
- OpenAI-shaped adapters: kept as the transport layer under the new provider interface.
- `run_dataset_story.py` CLI: kept working; its execution path gets the M1 fixes.
