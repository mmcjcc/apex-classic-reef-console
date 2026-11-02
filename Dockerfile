# syntax=docker/dockerfile:1
# reef-console: one image for the worker (python -m reef.main) and the pages (uvicorn reefweb.app:app). The backend's
# lint and tests, then the frontend's type check, lint, tests (against the API answers the backend's tests wrote) and
# build must all pass before the runtime image is built: docker compose build (or docker build --target runtime .).
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# mcp 2.2.0 with these fastapi, starlette and pydantic versions: a set known to work together
RUN pip install --no-cache-dir httpx==0.28.1 prometheus-client==0.26.0 pyyaml==6.0.3 \
    fastapi==0.142.2 starlette==1.7.0 pydantic==2.13.5 uvicorn==0.54.0 mcp==2.2.0

# ---- backend: lint, format check, tests; the tests also write the API answers the frontend's tests use ----------
FROM base AS test
RUN pip install --no-cache-dir pytest==9.1.1 pytest-asyncio==1.4.0 ruff==0.16.9
COPY config ./config
COPY worker ./worker
WORKDIR /app/worker
RUN ruff check reef reefweb tests && ruff format --check reef reefweb tests
RUN REEF_FIXTURE_DIR=/fixtures python -m pytest -q

# ---- frontend: type check, lint, tests, build ---------------------------------------------------------------
FROM node:24-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
COPY --from=test /fixtures ./src/test/api
RUN npm run typecheck && npm run lint && npm run test && npm run build

# ---- runtime ------------------------------------------------------------------------------------------------
FROM base AS runtime
COPY worker/reef ./reef
COPY worker/reefweb ./reefweb
COPY --from=frontend /fe/dist ./reefweb/dist
RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin reef && mkdir /data && chown 10002 /data
USER 10002
EXPOSE 9106 8001
CMD ["python", "-m", "reef.main"]
