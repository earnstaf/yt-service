# Phase 1 — Implementation Plan (v2, post-review)

**Goal:** Working transcript API + Claude skill. Captions when available, Whisper otherwise. Cached, batched, webhook-capable, deployable.

**Acceptance criteria:** spec §12 Phase 1 bullets, all green at the code level.

**Out of scope this phase:** chapters, diarization, summaries, channels/monitors, topics, sentiment, diff, LLM provider abstraction (intelligence stack). Diarization stub exists only as a no-op import path. **Deep links DO ship in P1** (see JC-014) since computation is trivial and the skill needs them.

**Plan review notes:** Significantly revised after sub-agent critique. Adds Group A0 (shared contracts), Group D3 (transcript orchestrator), pins behaviors that were ambiguous, splits oversize test files, fixes pgvector deployment gotcha.

## Task graph

Each numbered task is dispatched to a fresh subagent. Order is strict within a group; groups parallelize per the diagram at the bottom.

### Group A0 — Shared contracts (do FIRST, before everything else in B/C/D/E)

**A0-1 — Exceptions, domain dataclasses, pydantic schemas**
- `src/app/exceptions.py`: full hierarchy mapping to spec §5.6 error codes:
  - `YTServiceError(Exception)` base; subclasses `InvalidVideoIdError`, `InvalidRequestError`, `UnauthorizedError`, `InsufficientScopeError`, `FeatureDisabledError`, `VideoUnavailableError`, `NoAudioStreamError`, `NotFoundError`, `JobInProgressError`, `VideoTooLongError`, `BatchTooLargeError`, `RateLimitedError`, `YouTubeBlockedError`, `WhisperFailedError`, `LLMFailedError`, `QueueFullError`, `InternalError`. Each has `error_code: str` (e.g. `"invalid_video_id"`) and `status_code: int` class attrs. `JobInProgressError` also carries `existing_job_id: str` and `poll_url: str`.
- `src/app/domain.py`: frozen dataclasses (Python `@dataclass(frozen=True)`):
  - `Snippet(start: float, duration: float, text: str, speaker: str | None = None, deep_link: str = "")`
  - `CaptionsResult(video_id: str, language: str, is_generated: bool, snippets: list[Snippet], duration_seconds: float | None, full_text: str)`
  - `WhisperResult(video_id: str, source: Literal["whisper_openai","whisper_local"], language: str, snippets: list[Snippet], duration_seconds: float, full_text: str)`
  - `TranscriptRecord(video_id, language, source, is_generated, duration_seconds, snippet_count, cached_at, snippets, full_text, chapters=None, has_diarization=False)`
  - `JobPayload`: TypedDict spelling the exact JSON shape stored in `jobs.payload_jsonb` for whisper jobs: `{"video_id": str, "language": str, "force_whisper": bool, "include": list[str], "callback_url": str | None}`.
- `src/app/schemas.py`: pydantic v2 models. For each P1 endpoint, request + response shapes per spec §5.5:
  - `TranscriptResponse` (200) — matches §5.5 example exactly. Includes `chapters` (always null in P1), `snippets[].speaker` (null), `snippets[].deep_link` (computed in P1).
  - `JobAcceptedResponse` (202) — `{job_id, status, video_id, poll_url, estimated_seconds}`.
  - `JobStatusResponse` — joins jobs table.
  - `BatchRequest` / `BatchResponseItem` (union of TranscriptResponse | JobAcceptedResponse | ErrorEnvelope).
  - `ErrorEnvelope` — `{error: str, message: str, details: dict | None, job_id: str | None}`. `job_id` populated only for 409 `job_in_progress`.
  - `CacheStatsResponse` — `{total_rows: int, by_source: dict[str,int], oldest_cached_at: datetime | None, newest_cached_at: datetime | None}`.
  - `CachePurgeResponse` — `{video_id: str, rows_deleted: int}`.
- Helpers: `compute_deep_link(video_id, start) -> str` lives in `src/app/deep_links.py` (file exists in spec §7.2 but used immediately in P1, see JC-014).
- Tests: `tests/unit/test_schemas.py` — round-trips for every model, plus an explicit check that `TranscriptResponse` JSON matches the spec §5.5 example byte-for-byte after key sort.

This group lands BEFORE any B/C/D/E task.

### Group A — Foundations (sequential, in order)

**A1 — Settings, logging, metrics**
- `src/app/config.py`: single `Settings(BaseSettings)`. Every field in `.env.example` mapped. `settings` instance for direct import.
- `src/app/logging.py`: structlog JSON to stdout. Redaction processor strips substrings (case-insensitive): `authorization`, `bearer`, `api_key`, `token`, `full_text`, `audio_path`, `password`. `configure_logging(level)`.
- `src/app/metrics.py`: Prometheus collectors for all metrics in spec §10. Even the P3/P4 ones — labels are cheap and avoid future churn. Export `REGISTRY` for `/metrics`.
- Tests: `tests/unit/test_config.py`, `tests/unit/test_logging.py` (asserts redaction), `tests/unit/test_metrics.py`.

**A2 — Database models + alembic baseline**
- `src/app/models.py`: SQLAlchemy 2.0 declarative for ALL tables in spec §7.4 (`transcripts`, `summaries`, `topics`, `sentiment`, `jobs`, `monitors`, `tokens`, `llm_call_log`). Empty tables for future phases are fine — schema lands now to avoid migration churn.
- `src/app/db.py`: async engine, `async_sessionmaker`, `get_session()` FastAPI dependency. `check_db_health()` runs `SELECT 1`.
- `src/app/redis.py`: connection pool helper, `get_redis()` dependency, `check_redis_health()` runs `PING`.
- `alembic/env.py`: async + offline modes wired.
- `alembic/versions/<ts>_p1_initial.py`: creates ALL tables from §7.4. Includes `tsvector` generated column and `embedding vector(1536)` column. **Does NOT include `CREATE EXTENSION vector`** — that runs out of band (see deploy README, per H-3 from review). The migration assumes the extension already exists; if not, the `embedding` column add will fail and that's the expected loud failure.
- Tests: `tests/integration/test_models.py` marked `integration`.

**A3 — Auth + admin CLI**
- `src/app/auth.py`:
  - `make_token() -> str` returns `yt_` + 32 url-safe bytes.
  - `hash_token(plain) -> str` argon2id.
  - `verify_token(session, plain) -> Token | None` (checks `revoked_at IS NULL`, updates `last_used_at`).
  - `require_scopes(*scopes)` FastAPI dependency returning the `Token` row.
  - On scope mismatch raise `InsufficientScopeError`.
  - Bootstrap path: on first startup, if `YT_BOOTSTRAP_ADMIN_TOKEN` env is set AND no admin tokens exist in DB, insert one with that plaintext hashed + scopes `[admin]`. Logged once with the JC-011 reminder.
- `src/app/admin.py`: Click CLI `python -m app.admin`. Subcommands:
  - `tokens create --name <n> --scopes <csv>` → prints plaintext once.
  - `tokens list` → table.
  - `tokens revoke --id <tok_id>`.
- Tests: `tests/unit/test_auth.py` (hash/verify/scope), `tests/integration/test_admin_tokens.py`.

### Group B — Pure logic (parallel after A0+A1)

**B1 — URL/ID parsing**
- `src/app/parsing.py`:
  - `parse_video_id(input: str) -> str` accepting: bare 11-char ID, `watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`, `m.youtube.com`, query-fragment combinations. Raises `InvalidVideoIdError`.
  - `parse_channel_or_playlist(url) -> ChannelRef | PlaylistRef` — stub for P3, raises `NotImplementedError` for now.
- Tests: 15+ shapes in `tests/unit/test_parsing.py`.

**B2 — Cache layer**
- `src/app/cache.py`:
  - `get_transcript(session, video_id, language) -> TranscriptRecord | None` — None if expired (compares `expires_at` to `func.now()`).
  - `put_transcript(session, record, ttl_days)` — upsert; computes `expires_at = fetched_at + interval`.
  - `purge_transcript(session, video_id) -> int` — deletes ALL language rows for that video, returns count.
  - `stats(session) -> CacheStatsResponse-shaped dict`.
- Tests: `tests/integration/test_cache.py` using freezegun for TTL expiry.

### Group C — External adapters (parallel after A0+A1)

**C1 — Captions adapter**
- `src/app/youtube.py`:
  - `fetch_captions(video_id, lang, proxy=None) -> CaptionsResult | None` — None on `NoTranscriptFound`. Raises `YouTubeBlockedError` on `IpBlocked`, `VideoUnavailableError` on private/deleted/region-locked.
  - Honors `YT_HTTPS_PROXY` from settings if `proxy=None`.
- Tests: `tests/unit/test_youtube.py` with the library calls mocked. Three scenarios: happy, no-caption (returns None), IP-block.

**C2 — yt-dlp audio download + split**
- `src/app/whisper/audio.py`:
  - `download_audio(video_id, tmp_dir) -> Path` per spec §7.13. Raises `NoAudioStreamError`.
  - `split_audio(path, max_bytes) -> list[Path]` — uses ffmpeg subprocess. Strategy: try `-c copy -f segment -segment_time <est>`, fall back to time-based re-encode if needed. Skips entirely when file fits in `max_bytes`.
  - `cleanup(path)` for use in `finally`.
- Tests: `tests/unit/test_audio.py` with subprocess and yt-dlp mocked. ffmpeg presence is NOT required for tests.

**C3 — Whisper backends + dispatch**
- `src/app/whisper/openai_backend.py`: `transcribe(audio_path: Path) -> WhisperResult`. Chunks via `audio.split_audio` when over `WHISPER_CHUNK_BYTES`. Merges segments with proper time offsets.
- `src/app/whisper/local_backend.py`: `transcribe(audio_path: Path) -> WhisperResult` via `faster-whisper`. Lazy-loads model on first call.
- `src/app/whisper/__init__.py`: `transcribe(audio_path: Path) -> WhisperResult` dispatcher. Honors `WHISPER_BACKEND`. Fallback rules (pinned per H-10):
  - If `WHISPER_BACKEND=openai` and `WHISPER_FALLBACK_ON_OPENAI_ERROR=true`:
    - On `httpx.HTTPStatusError` with 5xx, `httpx.TimeoutException`, `httpx.NetworkError`, `openai.RateLimitError`: try local.
    - On `openai.AuthenticationError` (401), `openai.BadRequestError` (400): do NOT fallback — raise `WhisperFailedError` directly (mis-configuration, not outage).
  - If local also fails: raise `WhisperFailedError`.
- Tests: `tests/unit/test_whisper.py`.

### Group D — Service plumbing

**D2 — Webhook delivery (do BEFORE D1)**
- `src/app/webhooks.py`:
  - `deliver_webhook_task(callback_url, event, payload_dict, secret) -> None` is the RQ task that runs in workers.
  - Uses `httpx.AsyncClient` synchronously inside the RQ worker via `asyncio.run()`.
  - HMAC-SHA256 hex signature on the raw JSON body, header `X-YT-Signature: sha256=<hex>`.
  - Headers also include `X-YT-Job-Id`, `X-YT-Video-Id`, `X-YT-Event`.
  - Retries 3 times, 10s/60s/300s back-off, but done by re-enqueueing self with delay (not blocking inside the task) so a stuck callback doesn't tie up a worker (per H-11).
- `enqueue_webhook(...)` helper called by D1 — never blocks.
- Tests: `tests/unit/test_webhooks.py` with respx mocking the callback URL.

**D1 — Jobs + Whisper worker**
- `src/app/jobs.py`:
  - `enqueue_whisper(session, redis_, payload: JobPayload, token_id) -> Job` — generate ULID, acquire Redis `SETNX lock:whisper:{video_id}` with 1h TTL; if lock fails, look up the existing job_id by `(video_id, status in ['queued','running'])` and raise `JobInProgressError(existing_job_id=..., poll_url=...)`.
  - `get_job(session, job_id) -> Job | None`.
  - `mark_running / mark_complete / mark_failed`.
- `src/app/worker.py`:
  - `run_whisper_job(job_id)` worker entry. Loads job, downloads audio, transcribes, writes `TranscriptRecord` to cache, computes deep_links on every snippet, releases lock, enqueues webhook delivery (D2) if `callback_url` set. Audio cleanup in `finally`.
  - Module exposes worker setup helpers for the systemd units.
- Tests: `tests/integration/test_jobs.py` with fakeredis + real Postgres.

**D3 — Transcript orchestrator (the brain behind GET /v1/transcript)**
- `src/app/transcript_service.py` (new module):
  - `get_or_fetch(session, redis_, request) -> TranscriptResponse | JobAcceptedResponse` where `request` is a typed value object with fields `video_id, language, force, wait_seconds, include, callback_url, token_id`.
  - Logic per spec §5.5:
    1. Validate `wait_seconds <= 25`.
    2. If `force == "refresh"`: call `cache.purge_transcript` first.
    3. If `force != "whisper"`: try cache; on hit return immediately.
    4. If `force != "whisper"`: try `youtube.fetch_captions`; on success, `cache.put_transcript` and return.
    5. Otherwise: `jobs.enqueue_whisper(payload)`. On `JobInProgressError` we still return 202 with that job's id and poll URL (NOT 409 — see below).
    6. If `wait_seconds > 0`: poll the job for up to `wait_seconds`; if it completes inside the window, return cached transcript; otherwise return 202.
  - Returns the appropriate pydantic model.
- **409 vs 202 clarification (per review H-6):** spec §5.6 lists 409 `job_in_progress`. Spec §7.14 says "duplicate request returns existing job_id." Reconciliation: 409 applies to admin-targeted endpoints (e.g., a second batch submission of the same video, or a `force=whisper` retry while one is running). The normal GET path returns 202 with the existing `job_id` — caller perspective is "your transcript is being worked on." Implementation: the orchestrator catches `JobInProgressError` and converts to a 202 `JobAcceptedResponse`. The 409 path is exercised by batch with explicit force conflict.
- Tests: `tests/integration/test_transcript_service.py` — covers each branch with externals mocked, freezegun for `wait` semantics.

### Group E — API surface (after D1, D2, D3)

**E1 — FastAPI app, routes, middleware**
- `src/app/main.py`:
  - `create_app() -> FastAPI` app factory.
  - Routes:
    - `GET /v1/transcript` — calls `transcript_service.get_or_fetch`. Supports `format=json|text|srt`. SRT writer lives in `src/app/serialization.py` (new tiny module) and converts `TranscriptRecord` snippets to SRT cues with correct HH:MM:SS,mmm timing.
    - `POST /v1/transcript:batch` — caps at 50, parallelizes via `asyncio.gather`, returns array of per-video results.
    - `GET /v1/jobs/{job_id}` — joins jobs table.
    - `DELETE /v1/cache/{video_id}` — requires `admin` scope.
    - `GET /v1/cache/stats` — requires `read` scope.
    - `GET /healthz` — always 200 with `{"status":"ok"}`.
    - `GET /readyz` — checks db + redis; 503 if either fails.
    - `GET /metrics` — Prometheus format; loopback-only enforcement = checks `request.client.host in ("127.0.0.1","::1","localhost")` OR `X-Forwarded-For` first hop matches loopback. If reverse proxy injects `X-Real-IP`, that wins. Otherwise 403.
  - Exception handler maps `YTServiceError` subclasses to `ErrorEnvelope` with correct status code. Unhandled exceptions → 500 `internal_error` and log full traceback (without leaking token).
  - **Rate limiting (per review H-7):** custom middleware runs AFTER auth dependency populates `request.state.token`. Limits read from `tokens.rate_overrides` if set, else from `Settings.RATE_LIMIT_*`. Backed by Redis via `slowapi`'s `RedisStorage` (so limits survive process restart). Falls back to IP-keyed bucket for unauthenticated requests.
  - Access log middleware structured per spec §7.16.
- Tests split into focused files (per review M-5):
  - `tests/integration/test_endpoint_transcript.py` — happy path, cache hit, no-caption→202, force=whisper, force=refresh, wait param, format=text, format=srt, language fallback.
  - `tests/integration/test_endpoint_batch.py` — mixed cached/new/dup, callback fires.
  - `tests/integration/test_endpoint_jobs.py` — job status polling.
  - `tests/integration/test_endpoint_cache.py` — stats + purge + admin scope.
  - `tests/integration/test_endpoint_auth.py` — missing/bad/insufficient scope.
  - `tests/integration/test_rate_limit.py` — 429 with Retry-After.
  - `tests/integration/test_endpoint_meta.py` — healthz, readyz, metrics (loopback enforcement).

### Group F — Skill

**F1 — Claude skill scaffold**
- `deploy/skill/youtube-transcript/SKILL.md` — name + description frontmatter; triggers + non-triggers; how to invoke `fetch_transcript.py` and `summarize.py` (latter is stub in P1, expanded in P2); never-log-token rule; polling pattern for 202.
- `deploy/skill/youtube-transcript/scripts/fetch_transcript.py`:
  - Reads `YT_SERVICE_URL` (default `https://yt.ericmax.com`) and `YT_SERVICE_TOKEN`.
  - Stdlib + `httpx` if available, urllib fallback.
  - Accepts URL or ID arg. Calls `/v1/transcript`. Polls `/v1/jobs/<id>` on 202 up to 5 min with 5s interval (configurable via `YT_SERVICE_POLL_TIMEOUT`).
  - Prints final JSON to stdout. Non-zero exit on error. Never prints token.
- `deploy/skill/youtube-transcript/scripts/summarize.py`: stub script that prints `"summarize endpoint added in Phase 2"` in P1; will be fleshed out in P2.
- `deploy/skill/youtube-transcript/scripts/ingest_channel.py`: stub for P3 same pattern.
- `deploy/skill/youtube-transcript/README.md` — install instructions.

### Group G — Smoke + ops scripts

**G1 — scripts/ contents**
- `scripts/install.sh`: copies `deploy/*.service` to `/etc/systemd/system/`, `systemctl daemon-reload`, enable + start units. Runs `alembic upgrade head` as the service user. Idempotent.
- `scripts/make_token.sh`: thin wrapper that activates the venv and runs `python -m app.admin tokens create`.
- `scripts/smoke_test.sh`: reads `YT_SERVICE_URL` and `YT_SERVICE_TOKEN`; runs health, captioned video, repeat for cache hit (<200ms), no-caption video, metrics format check. Exits non-zero on any failure. Hardcoded URLs: keep an obviously stable captioned video (TED talk) and a Short with no captions; user will swap later if desired.

### Group H — Deploy artifacts

**H1 — systemd units + reverse proxy + deploy README**
- `deploy/yt-transcript.service` — API uvicorn 2 workers.
- `deploy/yt-transcript-worker-default.service` — RQ on `default` queue.
- `deploy/yt-transcript-worker-whisper.service` — RQ on `whisper`, concurrency 1.
- `deploy/yt-transcript-worker-enrichment.service` — RQ on `enrichment`, concurrency 1. Created in P1 even though no enrichment work runs yet (per review M-3, makes future redeploys atomic).
- `deploy/yt-transcript-worker-intelligence.service` — RQ on `intelligence`, concurrency 2. Same reasoning.
- Standard hardening (`NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=yes`, `ReadWritePaths=/opt/yt-transcript /var/tmp/yt-transcript`).
- `deploy/Caddyfile.snippet` and `deploy/nginx.conf.snippet` — `yt.ericmax.com` → `127.0.0.1:8765`. `/metrics` matcher restricts to loopback.
- `deploy/README.md` (new):
  - One-time DB setup as `postgres` superuser: `CREATE DATABASE yt_transcript;`, `CREATE USER yttranscript ...`, `GRANT ...`, `CREATE EXTENSION vector;`.
  - One-time system user: `useradd --system --home /opt/yt-transcript yttranscript`.
  - Redis DB index 3 reservation note.
  - HuggingFace pyannote terms-acceptance steps (referenced in P2 but documented now).
  - First-run bootstrap admin token flow via `YT_BOOTSTRAP_ADMIN_TOKEN`.

## Execution order

```
A0-1
  ↓
A1 → A2 → A3
       └──> B1, B2 (parallel) ── C1, C2, C3 (parallel)
                                       ↓
                                      D2
                                       ↓
                                      D1
                                       ↓
                                      D3
                                       ↓
                                      E1
                                       ↓
                                  F1, G1, H1 (parallel)
```

## Pinned implementation details (from review)

These are the items the review flagged as ambiguous and that must NOT be left to subagent discretion:

| ID | Detail |
|---|---|
| P-1 | Job in-progress: GET returns **202 with existing job_id**, NOT 409. 409 only on explicit retry conflict (batch with `force=whisper` while job running). |
| P-2 | `wait` param max 25s. Implementation polls in-process via async sleep loop with 0.5s interval. |
| P-3 | `force=refresh`: purge cache row BEFORE captions attempt. `force=whisper`: skip captions entirely, go straight to enqueue. |
| P-4 | `format=text`: return `full_text` as `text/plain`. `format=srt`: regenerate cues from snippets. Both bypass JSON schema entirely. |
| P-5 | Whisper fallback: 5xx + network/timeout + rate-limit triggers local. Auth/400 does NOT trigger local. |
| P-6 | Webhook delivery: separate RQ task, never blocking inside `run_whisper_job`. Retries by self-re-enqueue with delay. |
| P-7 | Loopback `/metrics`: accept `127.0.0.1`, `::1`, `localhost`, plus X-Forwarded-For first hop loopback OR X-Real-IP loopback (reverse proxy is on the loopback interface). |
| P-8 | `pgvector` extension creation lives in `deploy/README.md`, NOT in alembic. Migration assumes extension already exists. |
| P-9 | Coverage scope in `pyproject.toml`: limit `source` to files that exist in P1 (use a `[tool.coverage.run] include` with explicit P1 module list, drop the broad `source = ["src/app"]`). |
| P-10 | Rate limit middleware runs AFTER auth, keyed by `token.id`, falls back to IP for unauth. |
| P-11 | `MAX_DAILY_LLM_COST_USD` is configured but unused in P1 — wired in P4. Note in `app/config.py` docstring. |
| P-12 | `tokens.rate_overrides` column exists; not consulted in P1; commented as "P2+ wire-up" in `auth.py`. |
| P-13 | Deep links computed in P1, populated on every snippet in every response (JC-014). |
| P-14 | Test layering: integration tests opt-in via `pytest -m integration` OR `--run-integration` flag. Default `pytest` runs unit only. |
| P-15 | `bootstrap admin token` insertion runs at FastAPI app startup hook, only once on first start when zero admin tokens exist. |

## Definition of done (P1)

- All §12 Phase 1 acceptance bullets satisfied at the code level (deploy-only bullets like "/healthz over HTTPS" defer to the VPS step).
- `pytest` (unit only) green locally.
- `pytest -m integration` green when Postgres + fakeredis are wired (integration suite is the gate for P1 cycle commit, even if Windows ffmpeg test cases are skipped).
- `ruff check src tests` green.
- Coverage ≥ 85% on the explicit P1 module list per P-9.
- Codex review of full P1 diff: no blockers.
- Deployment artifacts (`deploy/`) collectively allow a clean `bash scripts/install.sh` followed by `bash scripts/smoke_test.sh` to pass on the VPS.
- Conventional commit message lists which §12 P1 bullets are satisfied.

## Known gaps deferred to deploy cycle (NOT in this phase's commit)

- Actually running on the VPS.
- Caddy `yt.ericmax.com` block live.
- TLS issued.
- DNS A record.
- Postgres + Redis live with the right user/DB.
- Bootstrap admin token in `.env` on host.
