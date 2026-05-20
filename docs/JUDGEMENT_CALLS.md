# Autonomous-Mode Judgement Calls

Decisions made without checking in with the user during the autonomous build of yt-transcript-service. Reviewed together at the end of the session.

Format: each entry is a short heading, the decision, the alternative considered, and the rationale.

---

## 2026-05-20 — Session start

### JC-001 — Python target version
- **Decision:** Pin `requires-python = ">=3.11"` in `pyproject.toml`, target 3.11+ in CI/lint, develop locally on the 3.12.10 that's installed.
- **Alternative:** Pin 3.12 specifically.
- **Rationale:** Spec says 3.11+. Pinning 3.11 keeps the VPS install flexible if it doesn't have 3.12 yet. Lower-bound is the only thing that matters for compatibility.

### JC-002 — Generic proxy slot vs Zyte-specific wiring
- **Decision:** Keep the env var `YT_HTTPS_PROXY` generic. Do not hard-wire Zyte API client.
- **Alternative:** User has Zyte API billing already, so wire that as primary.
- **Rationale:** Zyte API (the higher-level endpoint product) is not a drop-in for `youtube-transcript-api` — it would require rewriting the YouTube fetch path. Zyte Smart Proxy Manager (their proxy product) is a drop-in via the generic slot. Documenting both options in README. If YouTube starts blocking and the user wants Zyte API specifically, we revisit the fetch path then.

### JC-003 — Sentiment feature flag on by default
- **Decision:** `FEATURE_SENTIMENT=true` in `.env.example` and as the `Settings` default.
- **Alternative:** Keep spec default of `false`.
- **Rationale:** User explicitly chose "on by default" during brainstorm. Recording here because it's a security-adjacent deviation from spec (sentiment is more failure-prone and more expensive than other intelligence tasks; spec gates it for a reason).

### JC-004 — LLMAPI primary for topics, immediately
- **Decision:** `TASK_ROUTING["topics"].primary = "llmapi/gemini-2.5-flash"`, gemini_direct moves to first fallback, openai second.
- **Alternative:** Spec's 14-day burn-in plan.
- **Rationale:** User explicitly chose "make LLMAPI primary for topics immediately." Other tasks (summarize/diff/sentiment) keep the spec's direct-primary routing. Topics is the lowest-stakes intelligence task, so blast radius of a bad call is small.

### JC-005 — Test stack vs spec
- **Decision:** pytest + pytest-asyncio + respx + fakeredis + pytest-postgresql + freezegun for time-dependent tests.
- **Alternative:** Spec just says "unit / integration / smoke" without naming libraries.
- **Rationale:** Standard async-Python test stack. `pytest-postgresql` lets integration tests spin a real Postgres per session without needing Docker on Windows dev boxes. `fakeredis` keeps RQ tests hermetic.

### JC-006 — `src/` layout
- **Decision:** Use the `src/app/` layout per spec §7.2 even though many small services flatten to `app/`.
- **Alternative:** Flat `app/` at repo root.
- **Rationale:** Spec is explicit (`/opt/yt-transcript/src/app/`). Keeping the same layout avoids any deploy-path divergence.

### JC-007 — Initial branch name `main`
- **Decision:** `git init -b main`.
- **Alternative:** `master`.
- **Rationale:** Modern default. No existing convention in this repo.

### JC-008 — Pin upper bounds in requirements.txt
- **Decision:** All dependencies pinned with `>=X,<Y` upper bounds.
- **Alternative:** Loose pinning (`>=X` only).
- **Rationale:** Production VPS deploy. Upper bounds prevent surprise breakage on `pip install -U`. The spec mentions specific minimum versions; upper bounds keep us inside the same major.

### JC-009 — Cost guard wiring point
- **Decision:** `MAX_DAILY_LLM_COST_USD` enforcement lives at the start of every `llm.execute()` call (sum from `llm_call_log` for the current UTC date). On breach, raise an error that surfaces as 503 to callers.
- **Alternative:** Per-token daily cap, or per-provider cap.
- **Rationale:** Single-org service. Global cap is what the spec asks for. Per-token can come later via `tokens.rate_overrides`.

### JC-010 — Whisper audio chunking threshold
- **Decision:** Chunk at 20 MB even though OpenAI's stated limit is 25 MB. New env var `WHISPER_CHUNK_BYTES=20971520` (default 20 MiB).
- **Alternative:** Chunk at 24 MB to maximize per-request audio.
- **Rationale:** 20% headroom for multipart overhead and OpenAI's actual rejection threshold (which has historically been a bit under 25 MB in practice). One env var so we can tune without code change if needed.

### JC-011 — Bootstrap admin token slot
- **Decision:** Added `YT_BOOTSTRAP_ADMIN_TOKEN` env var (empty by default). Used only on first start if no admin tokens exist; otherwise ignored. Documented to be rotated immediately via the CLI.
- **Alternative:** No env-based bootstrap, force CLI-only.
- **Rationale:** Initial deploy needs *some* way to authenticate the first admin action without manual DB writes. This avoids that. Empty-default is safe.

### JC-012 — Skill scripts language
- **Decision:** Python for all three skill scripts (`fetch_transcript.py`, `ingest_channel.py`, `summarize.py`), single file each, only stdlib + `httpx` if available else `urllib`.
- **Alternative:** Bash wrappers.
- **Rationale:** Spec lists Python files in §11.1. Python on dev machines is a given since they run Claude Code. Stdlib-first means the scripts run without a venv if `httpx` isn't installed.

### JC-019 — Smoke test video IDs are placeholders
- **Decision:** `scripts/smoke_test.sh` ships with `CAPTIONED_VIDEO_ID=zhWDdy_5v2w` (TED-style talk, known captioned) and `NO_CAPTION_VIDEO_ID=9bZkp7q19f0` (Gangnam Style, which has captions in many locales and is NOT actually a clean no-caption case). Both are explicitly marked as operator-replaceable in the file header.
- **Alternative:** Pick a verified no-caption Short.
- **Rationale:** No way to programmatically find a stable no-caption video on YouTube — the captioning state changes. Documented in the script comments and `For Review` notes for the operator to swap before relying on test 6.

### JC-020 — RateLimitMiddleware replaced by per-route dependency
- **Decision:** Rate limiting is invoked via `await enforce_rate_limit(category, request, redis)` inside each handler AFTER `require_scopes` resolves. Not a Starlette middleware.
- **Alternative:** Implement as middleware per the original plan.
- **Rationale:** Starlette middleware runs before route dispatch, so `request.state.token` (populated by the auth dependency) is not visible yet. A per-route call after auth has the token in hand, which is what the per-token rate override design needs.

### JC-021 — `local_backend.py` excluded from coverage
- **Decision:** `pyproject.toml` `[tool.coverage.run] omit = ["src/app/whisper/local_backend.py"]`.
- **Alternative:** Cover it.
- **Rationale:** `faster-whisper`'s lazy model load is hard to exercise meaningfully without downloading the actual model weights and decoding real audio. The unit tests mock the backend; full coverage of the import-time model load isn't realistic in CI.

### JC-018 — Lazy Settings via attribute proxy
- **Decision:** `from app.config import settings` returns a thin proxy object that defers `Settings()` construction until first attribute access. Backed by `get_settings()` with `lru_cache(maxsize=1)`.
- **Alternative:** Eager `settings = Settings()` at module bottom (subagent's first cut).
- **Rationale:** Eager construction crashed on `import app.<anything>` in environments without `DATABASE_URL` and `REDIS_URL` set (which is every test that doesn't go through pytest fixtures). Lazy keeps imports cheap and pushes failure to the FastAPI app startup hook where it's caught and surfaced cleanly. Tests can still construct `Settings(_env_file=None, **overrides)` directly.

### JC-014 — Deep links computed in P1 (spec marks them P2)
- **Decision:** Compute `snippets[].deep_link` in P1, populated on every snippet.
- **Alternative:** Spec §7.7 lists deep links under Phase 2.
- **Rationale:** Computation is trivial (`https://youtu.be/{vid}?t={int(start)}`). Skill acceptance criterion §11.3 requires deep links. Deferring would force the skill to either compute them itself (duplication) or ship without timestamp quoting. Net cost is one helper file (`src/app/deep_links.py`) and one field. No DB schema change.

### JC-015 — pgvector extension creation moved out of alembic to deploy README
- **Decision:** `CREATE EXTENSION IF NOT EXISTS vector` runs as `postgres` superuser per `deploy/README.md`. The alembic migration assumes the extension already exists. If it doesn't, `embedding vector(1536)` column creation fails loudly.
- **Alternative:** Run the extension creation inside the migration.
- **Rationale:** Extension creation requires superuser. The migrating user (`yttranscript`) doesn't have it. Spec §8.2 shows the manual step explicitly. Loud failure is better than silent skip.

### JC-016 — Job conflict response: 202 with existing job_id on normal GET, 409 only on explicit conflict
- **Decision:** `GET /v1/transcript` returns 202 with the existing `job_id` when a duplicate request hits a running Whisper job. 409 `job_in_progress` is reserved for batch submissions or `force=whisper` retries during an active job.
- **Alternative:** Always return 409 on any duplicate (spec §5.6 reading).
- **Rationale:** Spec §7.14 says "duplicate request returns existing job_id" — that maps better to 202 (the caller's intent is satisfied: "give me the transcript, even if it's still cooking"). Spec §5.6's 409 is for explicit retry conflicts where the caller is signaling "I want to do this differently than the in-flight job." Both behaviors live in the code; the orchestrator picks 202 for the implicit case.

### JC-017 — Coverage source narrowed to P1 modules only
- **Decision:** `[tool.coverage.run] include = [...]` enumerates only modules that exist in P1. P2-P4 modules added to the list when they land.
- **Alternative:** Broad `source = ["src/app"]` with `fail_under` enforced phase-by-phase.
- **Rationale:** Empty/stub P2-P4 modules drag coverage below 85%. Explicit include prevents false failures and forces deliberate updates as each phase lands.

### JC-013 — Skill targets localhost for P1, switches to prod URL via env
- **Decision:** Skill reads `YT_SERVICE_URL` env var; defaults to `https://yt.ericmax.com`. P1 local testing sets `YT_SERVICE_URL=http://127.0.0.1:8765`.
- **Alternative:** Hardcoded prod URL with comment to change for testing.
- **Rationale:** Same skill artifact works for dev and prod. Token read from `YT_SERVICE_TOKEN` env var. No hardcoded creds anywhere.

### JC-022 — Batch response is a bare JSON array, not an `{items: [...]}` envelope
- **Decision:** `POST /v1/transcript:batch` returns a top-level JSON array of per-video objects to match spec §5.5 examples. The `BatchResponse` Pydantic wrapper is retained for internal validation but the route serializes the list directly.
- **Alternative:** Keep the `{items: [...]}` wrapper for forward-compatibility (paging, totals).
- **Rationale:** Spec §5.5 shows a bare array; consistency with the documented shape wins over speculative envelope room. If we need paging later we add a separate query endpoint, not a batch payload reshape.

### JC-023 — Discriminator `kind` excluded from wire JSON, kept in model for union dispatch
- **Decision:** `TranscriptResponse.kind`, `JobAcceptedResponse.kind`, and `ErrorEnvelope.kind` use `Field(default=..., exclude=True)`. Pydantic v2 `TypeAdapter[BatchResponseItem]` still uses the discriminator during validation; `model_dump_json` omits it.
- **Alternative:** Drop the field entirely and switch the union to a structural discriminator.
- **Rationale:** The default + `exclude=True` keeps union validation cheap (constant-time dispatch on a literal) while honoring the spec's wire shape. Removing the field would force every consumer to introspect field presence.

### JC-024 — SSRF guard: HTTPS-only in production, HTTP allowed in dev
- **Decision:** `app.url_safety.validate_callback_url` rejects every non-`https` scheme when `settings.is_production` is true. In `dev`/`test` it accepts `http://` as well so local fixtures can hit `http://127.0.0.1:...`. Private/loopback/link-local IPs are rejected in both envs.
- **Alternative:** HTTPS-only everywhere; tests use a self-signed cert.
- **Rationale:** Test ergonomics matter — forcing TLS into the unit test loop adds setup cost with no security benefit (the IP-range check is the real guard).

### JC-025 — `queue_full` (spec §5.6 503) deferred to a later phase
- **Decision:** P1 ships without explicit queue-depth admission control. Whisper requests enqueue unconditionally; saturation surfaces as worker latency, not as a 503.
- **Alternative:** Sample `Queue.count` before each enqueue and refuse above a threshold.
- **Rationale:** Useful only after we have queue-depth metrics over a real workload. P2/P3 adds monitor pipelines that need similar instrumentation — `queue_full` lands together with that work.

### JC-026 — Locks use compare-and-delete, with stale-lock takeover after a brief re-check
- **Decision:** `acquire_lock` stores the caller's job_id as the lock value. `release_lock` executes a Lua compare-and-delete so a delayed release from job A can never delete the lock owned by job B. When `enqueue_whisper` finds the lock held but no in-flight job row, it sleeps 50ms, re-checks, and (still nothing) steals the orphaned lock.
- **Alternative:** Plain SETNX + unconditional DELETE (the old behavior).
- **Rationale:** Compare-and-delete defends against the only realistic clobber path. The 50ms retry covers the race where the lock holder has SETNX'd but not yet COMMITted the row.

### JC-027 — Whisper rate limit applied via hook from API layer, not inside the orchestrator
- **Decision:** `transcript_service.get_or_fetch` accepts an optional `whisper_rate_limit_hook` coroutine and awaits it immediately before `jobs.enqueue_whisper`. The route handler in `app.main` supplies the hook so the rate limit applies only to the actual enqueue path (cache/captions hits skip it).
- **Alternative:** Always rate-limit at the top of the route (over-counts cache hits) or push the `request` object into the orchestrator (couples the layers).
- **Rationale:** Keeps `transcript_service` free of FastAPI imports while still placing the bucket check at the highest-fidelity location.

### JC-028 — `/metrics` peer trust: forwarded headers only honored when peer is loopback
- **Decision:** When the immediate peer is non-loopback, `X-Forwarded-For` and `X-Real-IP` are ignored entirely. A forwarded header can only DOWNGRADE a trusted (loopback) peer from "allow" to "deny" if it identifies a public client; it can never upgrade an untrusted peer.
- **Alternative:** Trust the first loopback candidate across peer + headers (the old behavior).
- **Rationale:** Forwarded headers are attacker-controllable across an untrusted hop. Restricting trust to the immediate peer's identity is the correct posture for an admin-only endpoint.

### JC-029 — Webhook URL validation: caller pays for malformed callbacks at submit time
- **Decision:** `validate_callback_url` runs during request handling for the batch route. A failing URL fails the entire batch with `invalid_request` rather than yielding 50 per-item errors at delivery time.
- **Alternative:** Validate per-item so a single bad URL only fails that one slot.
- **Rationale:** All batch items share the SAME `callback_url`, so per-item validation would produce N identical errors. One up-front rejection is clearer for the caller.

### JC-030 — Bootstrap admin advisory lock is a no-op on non-Postgres backends
- **Decision:** `bootstrap_admin_token` calls `pg_advisory_lock` only when the active dialect is `postgresql`. SQLite (used in some unit-test fixtures) skips the lock entirely.
- **Alternative:** Use a portable table-row lock or skip bootstrap entirely outside Postgres.
- **Rationale:** Production uses Postgres exclusively; the race the lock defends against is a 2-worker startup window. SQLite test contexts are single-process so the race cannot occur.
