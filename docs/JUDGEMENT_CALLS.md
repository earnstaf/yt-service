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

### JC-031 — `include=speakers` returns 200 (not 202) with enrichment-job side-effect
- **Decision:** `GET /v1/transcript?include=speakers` when diarization isn't yet computed returns 200 with the transcript body plus `diarization_status: "queued"` and `diarization_job_id: <id>` fields. The diarization job is enqueued as a side-effect. Client polls `/v1/jobs/<id>` and re-fetches the transcript when complete.
- **Alternative:** Return 202 (matches the whisper-202 pattern); or refuse to enqueue side-effects on GETs.
- **Rationale:** Unlike Whisper-202 (where the transcript itself is not yet available), the transcript IS available at request time — diarization is enrichment that lands later. Returning 200 with the body is more useful. The side-effect is idempotent (SETNX lock prevents duplicate jobs). Spec §5.5 explicitly allows "Returns nulls if data not yet computed."

### JC-032 — Diarization refuses captions-sourced transcripts
- **Decision:** If `transcript.source == "youtube_captions"`, diarization jobs fail fast with error `"diarization not supported on captions-sourced transcripts; use force=whisper to re-transcribe via Whisper first"`. Only `whisper_openai` and `whisper_local` sources are eligible.
- **Alternative:** Accept misalignment risk and run diarization on captions transcripts with a tolerance window.
- **Rationale:** YT-served caption timestamps may not align precisely with audio extracted by yt-dlp (different stream sources, different timing). Overlap matching would produce wrong speaker tags. Spec §7.6 already notes "For captions-only path, diarization requires triggering audio download separately" — we surface this as an explicit refusal with a clear remediation path.

### JC-033 — Daily LLM cost cap is `DailyCostCapExceededError → 503`, not `LLMFailedError → 502`
- **Decision:** New `DailyCostCapExceededError` exception with `status_code=503` and `error_code="daily_cost_cap"`. Distinct from `LLMFailedError` (provider failure, 502).
- **Alternative:** Overload `LLMFailedError` for cost-cap (subagent's first cut would have).
- **Rationale:** Cost cap is a quota/policy decision, not an upstream provider failure. 503 with `Retry-After` (set to seconds until UTC midnight) signals the client should back off. 502 would suggest "try again" which is wrong.

### JC-034 — LLM cost per-worker 60s cache acknowledged as approximate
- **Decision:** Daily-cost-sum is cached in memory per worker process for 60s to avoid hammering Postgres. Worst-case overshoot bounded by `N_workers × concurrent_calls × max_single_call_cost`. Acceptable for single-org P2.
- **Alternative:** No cache (query DB every call) or distributed cache via Redis.
- **Rationale:** P2 traffic is low; the cost cap is a safety net, not a precision metering tool. Distributed cache adds complexity for marginal value. Documented as approximate.

### JC-035 — `chapters_jsonb`: `[]` means "tried, got nothing"; `NULL` means "never tried"
- **Decision:** Empty list `[]` is stored after a failed LLM derivation or after a yt-dlp metadata call returning no chapters. `NULL` only via direct purge. `get_or_compute_chapters` treats `IS NOT NULL` as "cached, return as-is."
- **Alternative:** Re-derive on every call when result is empty.
- **Rationale:** Chapter derivation is the most expensive `include=chapters` path. Cacheing the negative result prevents infinite re-derivation on short videos that legitimately have no chapter structure.

### JC-036 — Chapter LLM derivation refused for very long transcripts
- **Decision:** Refuse LLM chapter derivation when `len(full_text) > 200_000` chars (~50k tokens). Yt-dlp chapter discovery is still attempted; failing that, persist `[]` and move on.
- **Alternative:** Map-reduce the transcript into windowed chapter proposals.
- **Rationale:** Map-reduce is a P3+ project. For P2, capping at 50k tokens keeps a single LLM call within budget for cheap providers (gemini-2.5-flash handles it fine). Very long videos either come with YT-published chapters (most do) or stay chapterless.

### JC-037 — `provider_override` requires admin scope, validated at the route layer
- **Decision:** `POST /v1/summarize` depends on `require_scopes("summarize")`. If the request body sets `provider_override`, the route handler additionally checks `Token.has_scope("admin")` inline and raises `InsufficientScopeError` if missing. Schema validates the format with regex `^(anthropic_direct|openai_direct|gemini_direct|llmapi)/[\w\-.]+$`.
- **Alternative:** Dynamic dependency that switches scope based on body content (complex), or rejecting `provider_override` from the schema layer (loses route context for clear error).
- **Rationale:** Inline route-layer check keeps the dependency simple and the error clean. Admin tokens already have `summarize` scope implicitly, so no conflict.

### JC-039 — `put_transcript` always resets `has_diarization` to the new record's value (codex fix)
- **Decision:** When `put_transcript` upserts an existing row, `has_diarization` is always set to the incoming record's value. Speaker tags are tied to the snippets they annotate; if snippets are replaced, the diarization invariant is broken.
- **Alternative:** Preserve `has_diarization=True` across writes (the original P2 plan implementation).
- **Rationale:** Codex flagged that the original behavior allowed `snippets_jsonb` to be replaced (e.g., on `force=refresh`) while `has_diarization=True` persisted, which would falsely tell clients diarization was current. The diarization worker uses `put_diarization` (partial UPDATE) and never goes through `put_transcript`, so the reset is correct.

### JC-040 — Cost cap response carries `Retry-After: <seconds-to-utc-midnight>`
- **Decision:** `DailyCostCapExceededError` includes a `retry_after` field in `details`, computed as seconds until the next UTC 00:00. The main exception handler reads it and sets the `Retry-After` HTTP header.
- **Alternative:** Omit the header (client guesses).
- **Rationale:** The cost cap is wall-clock-bounded (resets at UTC midnight). Telling clients the exact backoff window is straightforward and avoids them retrying every minute.

### JC-041 — `provider_override` bypasses summary cache + hashes into the cache key
- **Decision:** When `provider_override` is set, the cache lookup is skipped entirely, AND the override value is mixed into `custom_hash` so subsequent default-provider calls don't see the override-produced row.
- **Alternative:** Skip cache write entirely on override (codex's first suggestion).
- **Rationale:** Persisting the override result with a distinct hash gives us free benchmarking data (compare provider outputs side-by-side later) without breaking the default cache behavior.

### JC-042 — Monitors resolve channel handles to UC IDs at create time
- **Decision:** When creating a monitor, the route resolves `@handle` URLs to canonical `UC...` channel IDs via yt-dlp metadata before persisting. If resolution fails, return `invalid_channel` (400).
- **Alternative:** Store the handle verbatim and try to resolve at poll time.
- **Rationale:** YouTube's RSS feed only accepts `?channel_id=UC...`. Persisting unresolved handles means the scheduler can't poll. Fail fast at create time.

### JC-043 — Monitor scheduler is hand-rolled, not APScheduler
- **Decision:** `app.monitor_scheduler` uses a simple async loop with periodic reload (5 min) instead of APScheduler.
- **Alternative:** APScheduler (spec §7.9 names it).
- **Rationale:** APScheduler adds dependency weight and is awkward to combine with async polling. A 200-line loop covers the spec's requirements (per-monitor cadence, reload on new monitors, crash recovery via DB read) without extra machinery.

### JC-044 — Channel expansion failure is hard error, not silent empty list
- **Decision:** `expand_channel_or_playlist` raises `InvalidChannelError` on yt-dlp failure (network, blocked, deleted channel). Ingest surfaces this as 400.
- **Alternative:** Return `[]` and let the ingest succeed with `video_count=0` (the codex-reviewed original).
- **Rationale:** Silent success hides real failures. A user who pasted a wrong URL or hit YouTube IP-block deserves a clear error, not a misleading "0 videos."

### JC-045 — Monitor advances `last_video_id` only through dispatched videos
- **Decision:** When per-video dispatch fails inside the scheduler's poll loop, we stop advancing `last_video_id` at the failed video. The next poll retries from that point.
- **Alternative:** Always advance to the newest video; lost dispatches are accepted.
- **Rationale:** Monitor callbacks are the user-visible contract. Skipping a failed dispatch permanently loses a new-video notification. Pausing on the failure means at most one duplicate dispatch on the next poll (caught by the SETNX lock).

### JC-046 — Ingest forwards monitor's callback_url instead of firing its own
- **Decision:** When the scheduler dispatches a new video, it passes `monitor.callback_url` into the `TranscriptRequest.callback_url` field. The Whisper worker fires it on completion via the existing P1 webhook path. Cache hits fire the callback synchronously from the scheduler.
- **Alternative:** Scheduler fires `monitor.new_video` synchronously regardless of outcome (the codex-reviewed original — which leaked queued-but-not-yet-transcribed events as if they were complete).
- **Rationale:** Spec §7.9: "On completion of each new video, fire monitor's callback_url with the full result." That contract is only honored when the callback fires after completion, not after enqueue.

### JC-047 — Chapter-granularity sentiment requires chapters; refuses fallback
- **Decision:** `sentiment.compute_sentiment(..., granularity="chapter")` raises `NotFoundError` when no chapters are cached for the video, instructing the caller to request `/v1/transcript?v=<id>&include=chapters` first.
- **Alternative:** Fall back to a single whole-video segment (the original implementation), persisted under the `(video_id, "chapter")` cache key.
- **Rationale:** Codex flagged that the fallback poisoned the chapter cache — a later call after chapters land would return the stale one-segment result. Hard error is the right surface; client retries trivially after computing chapters.

### JC-048 — P4 intelligence endpoints support `provider_override` (admin-only)
- **Decision:** `/v1/topics` and `/v1/diff` accept `provider_override` body field. Schema validates the regex; route enforces admin scope inline. Same pattern as `/v1/summarize` (JC-037).
- **Alternative:** Reserve override to summarize only.
- **Rationale:** Spec §12 P4 acceptance includes "`provider_override` works for admin-scope tokens" — that bullet is unreachable without the field on all three intelligence endpoints. Topics and diff also bypass cache when override is set (JC-041 pattern).

### JC-038 — Diarization audio is re-downloaded (no pass-through from Whisper)
- **Decision:** Each diarization job downloads audio fresh via yt-dlp. No coordination with the Whisper job.
- **Alternative:** Chain Whisper → diarization with audio path pass-through (delete-on-last-consumer pattern).
- **Rationale:** Job isolation is cleaner. Diarization is CPU-bound (~0.5× realtime on CPU), so the ~30-50s download overhead is dominated by inference. Pass-through is a P3+ optimization if metrics show download bandwidth becomes a bottleneck.

### JC-030 — Bootstrap admin advisory lock is a no-op on non-Postgres backends
- **Decision:** `bootstrap_admin_token` calls `pg_advisory_lock` only when the active dialect is `postgresql`. SQLite (used in some unit-test fixtures) skips the lock entirely.
- **Alternative:** Use a portable table-row lock or skip bootstrap entirely outside Postgres.
- **Rationale:** Production uses Postgres exclusively; the race the lock defends against is a 2-worker startup window. SQLite test contexts are single-process so the race cannot occur.
