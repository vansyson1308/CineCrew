# CineCrew Studio UI

Director workspace for `src/studio` (React + TypeScript + Vite, plain CSS). Vietnamese labels.
All state lives in the Studio API; the UI only reflects it.

```bash
cd studio-ui
npm install
npm run build            # -> studio-ui/dist, served by `python -m src.studio serve` at /
```

Run (from the repo root, with the Python venv active):

```bash
STUDIO_HOME=/tmp/studio_home python -m src.studio serve --port 8765   # API + worker + built UI
# open http://127.0.0.1:8765/
```

Dev with hot reload (API must be running on 127.0.0.1:8765; `/api` and `/media` are proxied):

```bash
npm run dev              # STUDIO_API=http://host:port to point elsewhere
```

## Browser smoke test

```bash
npm run test:e2e         # builds dist, starts the server on :8777 with a fresh temp STUDIO_HOME, runs Playwright
```

It drives the UI in OFFLINE FIXTURE mode (no paid calls): create from the `hanoi_delivery` template,
export refusal before clips exist + ambiguous edit question (error states), cancel a clip job → approve →
retry it and see it finish ("resume" = cancel + retry), approve episode 1, natural-language edit of shot 3
(preview → apply; only `kf:shot_03` / `clip:shot_03` re-run), retry a clip + compare versions, export 9:16
(player + inspection table). Screenshots land in `test-results/screenshots/`.

Env knobs: `STUDIO_PYTHON` (default `../.venv/bin/python`), `STUDIO_TEST_PORT` (8777),
`PW_CHROMIUM_PATH` (a Chromium binary to use, if set;
otherwise Playwright's own browser).
