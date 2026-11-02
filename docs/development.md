# Development

## Layout

```
compose.yaml              the standalone stack (worker, web; Prometheus and Alertmanager with --profile alerts)
Dockerfile                one image: the backend's tests, the frontend's tests and build, then the runtime
config/site.example.yaml  a complete example site file (the tests use it too)
deploy/                   Prometheus (config, rules, rule tests) and an Alertmanager example
worker/reef/              the worker
worker/reefweb/           the web app's API and MCP endpoint
worker/tests/             pytest
frontend/                 the pages: React 19, Mantine 9, TanStack Query, Vite, Vitest
docs/                     these documents
```

## Tests

The image build is the test run: `docker compose build` (or `docker build --target runtime .`) runs every check
before it builds anything that runs:

1. the backend: `ruff check`, `ruff format --check`, and pytest. The web tests also write the API's answers to JSON
   files;
2. the frontend: `tsc`, ESLint, and Vitest, which renders every page against those JSON files, so the pages are
   tested against the real API rather than a guess of it; then `vite build`.

Only the tests:

```bash
docker build --target frontend .     # backend and frontend checks
docker build --target test .         # the backend's only
```

Without Docker, the backend needs Python 3.13. The versions are pinned in the `Dockerfile`:

```bash
python -m venv ~/.venvs/reef && . ~/.venvs/reef/bin/activate
pip install httpx==0.28.1 prometheus-client==0.26.0 pyyaml==6.0.3 fastapi==0.142.2 starlette==1.7.0 \
  pydantic==2.13.5 uvicorn==0.54.0 mcp==2.2.0 pytest==9.1.1 pytest-asyncio==1.4.0 ruff==0.16.9
cd worker && ruff check reef reefweb tests && python -m pytest -q
```

The Prometheus rules have their own tests:

```bash
docker run --rm -v "$PWD/deploy/prometheus:/p" --entrypoint promtool prom/prometheus:v3.15.0 test rules /p/tests/reef.yml
```

The frontend's tests import the API answers from `frontend/src/test/api/`, which the backend's tests write. To run
them outside the image build, write them first:

```bash
cd worker && REEF_FIXTURE_DIR=../frontend/src/test/api python -m pytest -q
cd ../frontend && npm ci && npm run typecheck && npm run lint && npm test
```

## The pages against a running reef-web

```bash
cd frontend && npm ci
VITE_API=http://reef.lan:8001 npm run dev      # proxies /reef/api to that reef-web
```

The dev server's requests carry no sign-in of their own, so use a reef-web in `none` mode, or one whose sign-in the
browser already holds.

## Style

- Python: ruff (line length 130), type hints, docstrings that say what and why. The tests read as sentences
  (`test_a_probe_alerts_after_its_pending_time_and_ends_inside_its_band`).
- Everything about one tank lives in the site file, never in the code. A new kind of check gets a key in `site.py`
  with a default that changes nothing for existing files.
- The worker is the only thing that talks to the Apex, and it never writes to it.

## Contributing

Issues and pull requests are welcome, especially from other Classic owners: a module or firmware this has not seen, a
status.json that parses differently. If you attach a status.json, remove the `link` field first (your Fusion key).
