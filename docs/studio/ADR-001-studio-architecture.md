# ADR-001 — CineCrew Studio architecture

Status: accepted (2026-10-14). Scope: first single-user, local-first deployment.

## Context

The upstream repo is a planning pipeline (FilmDSL + crew agents) with an opt-in, in-memory render
loop. The brief asks for a studio: durable projects, per-shot revision, trustworthy completion states,
predictable spend and complete exports, with a usable director UI. The audit (AUDIT.md) found that the
render loop can publish rejected/partial media and loses most dialogue lines.

## Decisions

1. **Keep FilmDSL as the creative plan; add a separate execution model.** `SceneBlueprint` stays the
   model-facing plan. A new package `src/studio/` owns execution state (jobs, attempts, budget, media,
   revisions, exports) in SQLite. The scheduler never edits creative layers except to record what was
   actually rendered.

2. **Storage: SQLite (WAL) + a project asset directory behind `MediaStore`.** One DB file per studio
   home (`STUDIO_HOME`, default `data/studio/`). Media are content-addressed (`sha256`) files under
   `media/`. `MediaStore` is the seam for object storage later; `Store` keeps SQL in one module so
   PostgreSQL is a driver swap plus `BEGIN IMMEDIATE` → `SELECT … FOR UPDATE` in the budget ledger.

3. **Three separate state axes per job**, never collapsed into one "status":
   - provider lifecycle: `pending → reserved → submitting → submitted → running → completed | failed | timed_out | cancelled | unknown`
   - quality verdict: `pending | accepted | rejected | review_error | unreviewed`
   - readiness: derived — a job is *final-ready* only when completed **and** accepted (or the job kind
     has no review, e.g. deterministic overlays). Exports are `final` only when every mandatory job is
     final-ready; otherwise they can only be produced as a labelled `draft_preview`.

4. **Immutable attempt records.** Each provider call is an `attempts` row written *before* submission
   (exact request JSON + hash, provider/model, reference hashes, seed, idempotency key), then updated
   only with outcome fields (remote id stored the moment it is returned, timings, usage, cost, media
   hash, verdict). The prompt of an attempt never changes; a revised prompt creates the next attempt.
   The job points at `selected_attempt_id`.

5. **Recovery without blind resubmission.** On worker start, attempts in `submitted/running` with a
   remote id are reconciled by polling the provider. Attempts left in `submitting` without a remote id
   are `unknown`: resubmitted only when the provider supports idempotency keys, otherwise surfaced as
   `needs_review` for a human decision.

6. **Budget ledger with reservations.** Every external call reserves its (conservative) cost before
   dispatch inside a `BEGIN IMMEDIATE` transaction, so concurrent workers cannot overcommit; the
   reservation is committed with actual usage (or the estimate when the provider reports none) or
   released. Unknown price and no configured upper bound → the operation is blocked. Default budget is
   0, so live calls are impossible until the user sets one.

7. **Providers are plugins with declared capabilities.** `ProviderCapabilities` lists durations, sizes,
   reference inputs, audio, negative prompt, seed, idempotency, cancellation, usage reporting and the
   exact optional fields accepted. Adapters only send declared fields. Shipped backends:
   - image: OpenAI Images (`gpt-image-1`); generic OpenAI-compatible self-hosted image server
   - video: OpenAI Videos (`sora-2`); generic OpenAI-compatible video server (Wan/LTX wrapper)
   - reviewer: existing `VisualJudgeAgent` over a vision chat model (now with real reference images)
   - TTS: OpenAI `/audio/speech` (`gpt-4o-mini-tts`)
   - **fixture** backends for every kind (ffmpeg-generated, watermarked "OFFLINE FIXTURE"), used by
     tests and the offline sample mode. Fixture output is never evidence that an AI integration works.

8. **Caching and selective regeneration by content hash.** A job's `input_hash` covers its request
   parameters, provider/model and the hashes of the media it depends on. An accepted attempt with the
   same input hash is reused with zero calls. Editing shot 3's lighting changes only shot 3's keyframe
   hash, which cascades to its clip and the final assembly; other shots hit the cache.

9. **Edits are typed patches.** Natural-language edits resolve to a `ShotPatch`/`AssetPatch` list, an
   affected-job list and a cost estimate (deterministic parser for common Vietnamese/English phrasings;
   LLM resolver agent in YAML when a key and budget exist). Identity changes show their cross-episode
   impact and require confirmation.

10. **Audio and exports are deterministic code.** TTS durations are measured from the waveform; shot
    timing follows speech. SRT/ASS are generated from measured timings; titles/logos/prices/CTAs are
    ffmpeg overlays (exact). Mixing uses ffmpeg (`sidechaincompress` ducking, fades, peak check). Exports
    record native render size separately from scale/pad/crop to 1080x1920 or 1920x1080, and are probed
    with ffprobe (dimensions, fps, duration, audio streams, black frames).

11. **API + worker + UI.** FastAPI (`src/studio/api.py`), a worker process (`python -m src.studio worker`)
    or an in-process worker thread for one-command startup, React + TypeScript (Vite) UI in `studio-ui/`
    served by FastAPI after build. Playwright smoke tests drive the UI against the offline sample mode.

12. **Long scripts are segmented, not sliced.** `script_segmentation` splits on scene headings /
    paragraphs with source spans; the pipeline writes a coverage report listing every omitted span and
    an extractive summary of it, instead of silently dropping text.

## Consequences

- The CLI keeps working; its render loop now refuses to publish rejected/partial results as final.
- New dependencies: `fastapi`, `uvicorn`, `pillow` (comic carousel / contact sheets), `httpx` (tests).
  Node is needed only to build the UI.
- Not built now: multi-tenant auth, billing, social posting, a non-linear editor, music generation.
- Path to scale: PostgreSQL via the `Store` seam, S3-style `MediaStore`, multiple workers already safe
  thanks to transactional budget reservations and job leases.
