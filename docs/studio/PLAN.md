# CineCrew Studio — plan, completion matrix, handoff

Branch `claude/master-execution-4eq89h`, base commit `3eac773` (see [AUDIT.md](AUDIT.md), [ADR-001](ADR-001-studio-architecture.md)).
Status date: 2026-10-14. External API spend so far: **$0** (no live call has been made).

Two kinds of evidence are kept apart everywhere in this file:

- **OFFLINE** — verified with fixture providers (deterministic stand-ins, output stamped "OFFLINE FIXTURE"),
  real ffmpeg/ffprobe, real SQLite, real HTTP server and a real browser. Proves the plumbing, not model quality.
- **LIVE** — verified by rendering with real image/video/TTS/judge models. **Nothing is LIVE-verified yet.**

## Setup, start, test (reproduced from a fresh clone, Python 3.13, Node 22, ffmpeg 6.1)

```bash
git clone -b claude/master-execution-4eq89h https://github.com/vansyson1308/CineCrew && cd CineCrew
# system: ffmpeg + ffprobe, DejaVu fonts (Vietnamese glyphs), Node >= 18 for the UI build
scripts/start_studio.sh                       # creates .venv, installs, builds studio-ui, serves :8765
# or step by step:
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
(cd studio-ui && npm ci && npm run build)
python -m src.studio serve --port 8765        # API + worker + UI; STUDIO_HOME picks the data dir
python -m pytest tests -q                     # 48 tests, ~2 min, no network
(cd studio-ui && npm run test:e2e)            # Playwright smoke test (PW_CHROMIUM_PATH optional)
python -m src.studio demo --fresh --out out/  # OFFLINE end-to-end: 2 episodes, 9:16 + 16:9, comic, revision
```

Reproduction on 2026-10-14 (fresh `git clone` of the branch into an empty directory, new venv): 48/48 pytest
passed, the UI built, `GET /api/health` returned ok, the UI index served, and the Playwright smoke test passed.
CI: `.github/workflows/studio-tests.yml` runs the pytest gate on every PR (ubuntu, ffmpeg, DejaVu).

## Canary (the one command that unblocks the live gate)

1. Credentials in `.env`: `LLM_API_KEY` (OpenAI: images, TTS, judge), `GEMINI_API_KEY` (Veo 3.1).
2. Prices in `configs/studio/providers.yaml` → `live.*.price` from the providers' current price pages
   (`usd` per unit, or a conservative `max_usd_per_call`). A null price blocks that call by design.
3. Run:

```bash
python -m src.studio canary --budget 10
```

It creates a one-shot live project (shot 2 of episode 1: Minh asks for house no. 47), renders the reference
sheets, keyframe, voice and one Veo clip under a hard $10 ledger cap, reviews them with the VLM judge, prints
the ledger and the readiness, and exports a labelled MP4 with its manifest. Expected order of magnitude
(not verified against current price pages): a few dollars, dominated by one 8 s Veo clip.

## Alternative provider line-up (huobao-drama)

Seedance 2.0 / MiniMax H3 / Wan 3.0 video, Seedream / Gemini images, MiniMax speech, with an optional one-key
gateway: `STUDIO_PROVIDERS=configs/studio/providers.huobao.yaml`. Details, license note and limits:
[HUOBAO_INTEGRATION.md](HUOBAO_INTEGRATION.md). OFFLINE-verified only.

## Offline engineering gate (brief §Acceptance)

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Regression tests for verified upstream defects (short dialogue, rejected/unreviewed output) | **PASS (OFFLINE)** | `tests/test_upstream_regressions.py` 10 tests; all 10 fail on `3eac773` ([evidence](evidence/baseline_3eac773_regressions.txt)), pass on the branch |
| 2 | Provider contract tests: errors, unsupported params, missing audio, remote timeout, cancellation | **PASS (OFFLINE)** | `test_veo_*` (httpx MockTransport: 429/503/400, network drop → ambiguous, unsupported size/duration/field, missing key, filtered result), `test_silent_voice_is_rejected_and_blocks_final`, `test_remote_timeout_goes_to_review_and_is_never_resubmitted`, `test_cancel_running_clip` |
| 3 | Restart/recovery does not blindly duplicate remote jobs | **PASS (OFFLINE)** | `test_restart_mid_render_does_not_resubmit` (2 clips in flight, process dropped, new process resumes polling, provider counts 2 submissions), `test_interrupted_submit_idempotent_vs_unknown`, `test_ambiguous_submission_is_reconciled_not_duplicated` |
| 4 | Shot revision keeps unaffected hashes, no model calls for them | **PASS (OFFLINE)** | `test_revision_regenerates_only_affected_and_rollback_is_free` (2 new generations, every other selection identical, rollback = 0 generations); demo: 16 jobs kept from cache |
| 5 | Final export cannot silently omit failed mandatory shots | **PASS (OFFLINE)** | `test_failed_clip_blocks_readiness_and_export`, `test_rejected_clip_is_draft_not_final`, `test_judge_error_is_not_acceptance`; partial previews get a labelled slate + watermark |
| 6 | Budget reservations under simultaneous jobs and retries | **PASS (OFFLINE)** | `test_budget_reservations_are_atomic_under_threads` (30 threads, exactly 10 reservations fit), `test_engine_stops_at_budget_and_resumes_after_raise`, `test_rate_limit_is_retried_without_new_generation` (refused submit released, never billed), `test_live_project_without_price_is_blocked_before_any_call` |
| 7 | UI smoke tests: create, edit, retry, resume, export, error state | **PASS (OFFLINE)** | `studio-ui/tests/studio.smoke.spec.ts` (1 test, all steps by clicks/typing). "Resume" in the UI = cancel then retry; process-restart resume is covered by #3 |
| 8 | Clean-environment startup documented and reproduced | **PASS (OFFLINE)** | section above; fresh clone + venv reproduction |

## Live-media gate

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Two episodes, same two original characters; ep 1 six-shot Vietnamese 45–60 s | **BLOCKED / NOT VERIFIED** — template ready (`hanoi_delivery`, 2 × 6 shots, 48 s OFFLINE) |
| 2 | Lines, characters, locations traceable in plan and final artifact | **BLOCKED / NOT VERIFIED** — manifest + subtitles carry them OFFLINE |
| 3 | Real voice, correct subtitles, requested aspect | **BLOCKED / NOT VERIFIED** |
| 4 | One scene revised and re-exported without regenerating others | **BLOCKED / NOT VERIFIED** — works OFFLINE |
| 5 | Interrupted run resumes with provider reconciliation | **BLOCKED / NOT VERIFIED** — Veo has no idempotency key: an interrupted submit becomes `unknown` → needs_review (never resubmitted blindly) |
| 6 | Reviewer gets playable film, contact sheet, rubric | **BLOCKED / NOT VERIFIED** |
| 7 | Measured costs and native render resolution | **BLOCKED / NOT VERIFIED** — ledger and manifest fields exist |

Blocker: no approved budget and no provider prices/credentials. Unblock with the canary above.

## Milestones

| Milestone | Status | Notes |
|-----------|--------|-------|
| M0 audit, ADR, backlog, regression tests, offline fixture provider | **Done** | AUDIT F1–F9 + A1–A4; F2 worse than the brief said (11/12 dialogue shots lost their line) |
| M1 reliable complete rendering | **Done (OFFLINE)** | three state axes, immutable attempts, recover/reconcile, ledger reservations, canary command; CLI path also fixed |
| M2 assets, storyboard, locks, NL edits, cache, rollback, segmentation | **Done (OFFLINE)** | rules resolver offline; LLM resolver (`edit_resolver`) live-only and untested against a model |
| M3 TTS, subtitles, overlays, mixing, exports, manifest | **Done (OFFLINE)** | real TTS untested (fixture voice is a tone); lip sync disclosed as NOT AVAILABLE with Veo + TTS |
| M4 director UI | **Done (OFFLINE)** | React/TS, Vietnamese labels, one-command start, smoke test |
| M5 comic carousel, series/brand templates, 2nd episode, hooks, benchmark | **Partial** | comic, series template + ep 2, brand template with exact overlays and 3 hook variants done; **benchmark not run** (needs live providers and spend) |

## Known limitations (honest list)

- Live providers are code-complete but unverified: Veo is tested only against a mock transport; OpenAI
  image, TTS and the VLM judge have no automated test against their real API shape.
- Crew planning from a free script (`plan_episode_live`) has only been exercised with an injected fake pipeline.
- Keyframes render in parallel, so the "previous shot" continuity reference reaches the judge only when the
  previous keyframe was already accepted.
- Export is a blocking request (~25 s offline for 48 s); no progress events.
- Readiness reasons and job errors are English strings; the UI wraps them in Vietnamese labels.
- Product identity from a supplied image is *conditioned*, not guaranteed; only overlays/logo/subtitles are exact.
- Veo always generates its own audio; the studio mixes our TTS track and drops the clip audio by default
  (`settings.audio.keep_clip_audio_db`), so mouths are not synchronised to the voice.

## Approaches that failed (kept for the record)

- Fake clips that were not valid MP4 made the first regression test pass on the buggy baseline; switched to
  real ffmpeg test clips so all 10 fail on `3eac773`.
- `sqlite3.executescript` inside an explicit transaction ("cannot commit - no transaction is active"); schema
  now runs outside `tx()`.
- Counting attempts per job got messy across resets; replaced by a per-job `epoch`.
- Budget/contract blocks flip-flopped every tick; they now persist until `unblock()` (raise budget).
- A fixture 429 keyed by idempotency key never cleared (each transport retry is a new attempt); now keyed by prompt.
- Keyframe references omitted the shot's location; fixed (characters + location + props).

## Ranked roadmap (after the canary)

1. **Run the canary, then episode 1 live** with real prices; record cost per accepted minute and native resolution.
2. **Live contract tests** for OpenAI image/TTS/judge (recorded responses), and a reconcile tool for Veo `unknown` submits.
3. **Benchmark harness** (M5): fixed briefs, baseline `3eac773` vs studio under matched providers; complete-export
   rate, accepted/generated shots, cost per accepted minute, revision time/cost, duplicate submissions, coverage.
4. **Sequential continuity option**: render keyframe N after N−1 is accepted and condition on it.
5. **Async export job** with progress events; localise readiness/errors.
6. **Audio-driven lip sync** backend behind the same provider contract (only claimed when the backend declares it).
7. Research candidates from the brief (StreamEdit adapter, multi-view character assets, geometry-aware shot
   planning, learned provider routing), each as a bounded experiment first.
