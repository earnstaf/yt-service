# Phase 2 — Implementation Plan

**Goal:** Add chapter detection, speaker diarization, and on-demand summarization on top of the P1 transcript pipeline. Wire the LLM provider abstraction (used here for chapter generation and summaries; P4 expands the catalog).

**Acceptance criteria:** spec §12 Phase 2 bullets, all green at the code level.

**Out of scope this phase:** channels/monitors (P3), topics, sentiment, diff (P4). Multi-provider fallback chain ships in skeleton form here; the LLMAPI burn-in plan and full provider catalog land in P4.

**Build on:**
- `app/transcript_service.py` returns transcripts; we wrap it with chapters/diarization enrichment.
- `app/jobs.py` already has Redis lock + ULID job-id semantics; we add `enrichment` queue use.
- `app/cache.py` already writes `chapters_jsonb` and `has_diarization` columns; we just fill them.
- `app/summaries` model already exists in `models.py`; we add the orchestration.

---

## Task graph

### Group A0 — Cache + jobs extension (do FIRST, before B/C/D/E)

**A0-1 — Extend cache.py with partial-update helpers**

Files modified:
- `src/app/cache.py`:
  - Add `async def put_chapters(session, video_id, language, chapters: list[Chapter]) -> None`. Partial UPDATE of `chapters_jsonb` only. Does NOT touch `snippets_jsonb`, `full_text`, `has_diarization`, `expires_at`, `fetched_at`. Stores `[]` to mean "tried, got nothing" (so we don't re-derive on every call); stores `None` only via direct purge. Caller passes `[]` when LLM derivation yields nothing.
  - Add `async def put_diarization(session, video_id, language, snippets: list[Snippet], has_diarization: bool) -> None`. Partial UPDATE of `snippets_jsonb` + `has_diarization` only. Used by the diarization worker to re-write speaker-tagged snippets on top of an existing row.
  - Fix `_row_to_record` to read `row.chapters_jsonb` into `TranscriptRecord.chapters` and `row.has_diarization` into `TranscriptRecord.has_diarization`. (Currently hardcodes None / False — P1 stub.)
  - Fix `put_transcript`'s ON CONFLICT DO UPDATE clause to PRESERVE existing `chapters_jsonb` and `has_diarization` when the new record doesn't carry them. Concretely: only overwrite those columns when `record.chapters is not None` (chapters) or `record.has_diarization` is explicitly True (diarization). Use `excluded.chapters_jsonb` only when `excluded.chapters_jsonb IS NOT NULL`. Easier path: omit those two columns from the SET clause entirely when the new record doesn't supply them.
- Tests `tests/integration/test_cache.py`: add tests for partial-update behavior — write transcript, write chapters, refetch transcript → chapters preserved; write transcript again → chapters STILL preserved.

**A0-2 — Generalize jobs.py for non-whisper job types**

Files modified:
- `src/app/jobs.py`:
  - Rename `_WHISPER_QUEUE` and `_WHISPER_TASK_PATH` to a per-job-type registry: `_JOB_REGISTRY = {"whisper": {"queue": "whisper", "task": "app.worker.run_whisper_job"}, "enrichment": {"queue": "enrichment", "task": "app.worker.run_diarization_job"}}`.
  - `_find_in_progress_job(session, video_id, job_type)` — add `job_type` parameter, filter by it.
  - `enqueue_whisper` becomes `enqueue_job(session, redis_sync, redis_async, *, video_id, job_type, payload, token_id)` — generic, dispatches via registry. Backwards-compat: keep `enqueue_whisper` as a thin wrapper.
  - Add `enqueue_diarization(session, redis_sync, redis_async, video_id, token_id, language)` calling the generic helper.

Tests `tests/unit/test_jobs.py` (new file, separate from integration test_jobs): assert that `enqueue_diarization` uses the enrichment queue + correct task path. Mock RQ.

---

### Group A — LLM provider scaffold (do AFTER A0; required by chapters + summarize)

**A1 — Minimal LLM layer (just enough for P2)**

Files:
- `src/app/llm/__init__.py` — re-exports `execute`, `LLMResponse`.
- `src/app/llm/providers.py` — provider registry (`PROVIDERS` dict per spec §7.3). All four providers registered now: `anthropic_direct`, `openai_direct`, `gemini_direct`, `llmapi`. `llmapi` is marked `optional=True` so empty API key skips it silently in fallback chains.
- `src/app/llm/routing.py` — `TASK_ROUTING` per spec §7.3, with JC-004 deviation for `topics`. The full table lands now (even though `topics`/`sentiment`/`diff` tasks don't exist until P4) so P4 only adds tasks, not routing:
  - `chapters`: primary `gemini_direct/gemini-2.5-flash`, fallbacks `[anthropic_direct/claude-sonnet-4-6, openai_direct/gpt-4o-mini]`.
  - `summarize`: primary `anthropic_direct/claude-sonnet-4-6`, fallbacks `[openai_direct/gpt-4o, llmapi/claude-sonnet-4-6]`.
  - `summarize_exec_deep`: primary `anthropic_direct/claude-opus-4-7`, fallbacks `[openai_direct/gpt-4o, llmapi/claude-opus-4-7]`.
  - `topics`: primary `llmapi/gemini-2.5-flash` (per JC-004), fallbacks `[gemini_direct/gemini-2.5-flash, openai_direct/gpt-4o-mini]`. ← Note: this is P4-used but lands here.
  - `sentiment`: primary `gemini_direct/gemini-2.5-flash`, fallbacks `[llmapi/gemini-2.5-flash]`.
  - `diff`: primary `anthropic_direct/claude-sonnet-4-6`, fallbacks `[openai_direct/gpt-4o, llmapi/claude-sonnet-4-6]`.

**`LLMResponse` exact shape** (pin, do NOT drift):
```python
@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    provider: str   # e.g. "anthropic_direct"
    model: str      # e.g. "claude-sonnet-4-6"
    latency_ms: int
```
- `src/app/llm/anthropic_client.py` — thin async wrapper. `acomplete(model, system, user, max_tokens) -> LLMResponse`. Returns text + tokens_in + tokens_out + cost_usd.
- `src/app/llm/gemini_client.py` — same surface via google-generativeai SDK.
- `src/app/llm/openai_client.py` — same surface via openai SDK.
- `src/app/llm/llmapi_client.py` — same surface via openai SDK pointed at LLMAPI_BASE_URL. (Skipped at call time when LLMAPI_API_KEY is empty — handled by fallback executor.)
- `src/app/llm/cost.py` — static price table per model: `{model: (in_per_million, out_per_million)}`. Function `compute_cost(model, tokens_in, tokens_out) -> Decimal`.
- `src/app/llm/fallback.py` — `async def execute(task, system_prompt, user_prompt, *, max_tokens=2048, video_id=None, token_id=None, provider_override=None) -> LLMResponse`:
  - Look up routing, build provider list [primary] + fallbacks.
  - Skip any provider whose API key env var is empty (treated as not configured — applies primarily to `llmapi` during P2/P3).
  - For each provider/model entry: try the call with a per-call timeout (60s default). On any failure (timeout, 5xx, rate limit, unavailable model), log + increment `yt_llm_calls_total{status="error"}`, move on. On the FIRST success, log + increment `status="ok"`, write a row to `llm_call_log`, increment `yt_llm_cost_usd_total`, return.
  - On all-failed: raise `LLMFailedError`.
  - **Daily cost guard** (JC-009 / P-11): before each call, sum `llm_call_log.cost_usd` for current UTC date. If `> settings.max_daily_llm_cost_usd`, raise `DailyCostCapExceededError("daily cost cap reached: ${spent} > ${cap}")`. New exception class with `status_code=503`, `error_code="daily_cost_cap"`. Cache the daily sum in memory for 60s to avoid hammering the DB.
  - **Approximate-cap acknowledgement (per-worker 60s cache)**: with N workers and concurrent calls, the cap can be exceeded by up to `N × per_call_cost` in the worst case. Documented limit; acceptable for P2 single-org use.
  - `provider_override` parameter (admin only at the route layer): if set, use that single provider/model entry with no fallback chain.

**Cost price table (source of truth):**
Documented inline in `cost.py` with citation comments. Use vendor public pricing as of build date (May 2026). Each entry: `# Source: <vendor pricing URL>, fetched <date>`. PR review must confirm.
- Tests `tests/unit/test_llm_fallback.py` + per-client unit tests with mocked SDKs.

### Group B — Chapter detection

**B1 — `src/app/chapters.py`**

Two sources:
1. **YouTube-provided** via yt-dlp metadata. Function `fetch_yt_chapters(video_id) -> list[Chapter] | None`. Uses `youtube.fetch_video_metadata(video_id)` (added in P1's H12 fix); parses the `chapters` field if present.
2. **LLM-derived** fallback. Function `derive_chapters_from_transcript(record: TranscriptRecord) -> list[Chapter]`. Builds a prompt with the full text + timestamp anchors at every 30s. Asks the model for 4-12 sections with `{start, end, title}`. Validates: starts in monotonic order, end of last chapter ≤ duration_seconds. Snaps starts to nearest snippet boundary.

`async def get_or_compute_chapters(session, video_id, language) -> list[Chapter]`:
- Read row. If `chapters_jsonb IS NOT NULL` (including `[]`), return it as-is. `[]` means "tried, got nothing — don't retry."
- Else fetch yt-dlp chapters; persist (even if empty list) + return.
- Else call LLM derivation; persist (even if empty list, or on JSON parse failure) + return.
- All three branches update the row's `chapters_jsonb` via `cache.put_chapters(...)` (partial update, doesn't touch other columns).

**Token cap guard:** if `full_text` length exceeds 200,000 chars (~50k tokens), refuse LLM derivation and persist `[]`. Yt-dlp chapters still attempted first. Subagent rationale: chapter derivation is best-effort; a 4h video without YT chapters can stay chapterless.

Tests `tests/unit/test_chapters.py` — mock yt-dlp and the LLM call.

### Group C — Diarization

**C1 — `src/app/diarization.py`**

Module-level lazy load of `pyannote.audio.Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=settings.huggingface_token)`. On `HUGGINGFACE_TOKEN` empty OR import failure, mark diarization unavailable and return a flag/sentinel.

**Captions vs Whisper source rule:** diarization aligns turn boundaries against snippet start/duration via overlap matching. Audio downloaded fresh by the diarization job may have slightly different timing than YT-served caption tracks. To avoid wrong speaker tags:
- If `transcript.source == "youtube_captions"`, refuse diarization with error `"diarization not supported on captions-sourced transcripts; use force=whisper to re-transcribe via Whisper first"`. Mark job failed with that message.
- If `transcript.source in {"whisper_openai", "whisper_local"}`, proceed normally — Whisper timestamps align with the freshly downloaded audio (same source extraction).

`async def diarize(audio_path: Path, snippets: list[Snippet]) -> list[Snippet]`:
- Run the pipeline in `asyncio.to_thread`.
- For each turn `(speaker_label, start, end)`, find overlapping snippets via interval overlap (use a 200ms tolerance); assign that speaker to those snippets. Snippets without overlap keep `speaker=None`.
- Returns a new list of Snippets with `speaker` populated.

`enqueue_diarization` lives in `jobs.py` (per A0-2). This module exposes only `diarize()` and `run_diarization_job(job_id)`.

`def run_diarization_job(job_id)` — worker entrypoint. Loads job → loads cached `TranscriptRecord` → checks source (refuses captions per above) → downloads audio fresh → runs diarize → calls `cache.put_diarization(session, video_id, language, snippets, has_diarization=True)` → releases lock → cleans audio.

If diarization unavailable (no HF token, gated model not accepted), the job marks itself failed with error `"diarization unavailable: huggingface model not accessible"`. The API surface still works — clients just see `has_diarization=False`.

**Audio pass-through deferred to P3.** P2 always re-downloads. Documented trade-off: enrichment queue is CPU-bound by the diarization model run (~0.5× realtime), so download latency is dominated by inference time anyway.

Tests `tests/unit/test_diarization.py` — mock pyannote.

### Group D — Summarize endpoint

**D1 — `src/app/tasks/summarize.py`**

`async def summarize(session, video_id, style, audience, custom_prompt, max_tokens, include_timestamps, provider_override, token_id) -> Summary`:
- Look up cached summary by `(video_id, style, audience_hash, custom_hash)`. If present + not expired, return it.
- Load `TranscriptRecord` via `cache.get_transcript`. If missing, raise `NotFoundError("transcript not cached; fetch transcript first")`.
- Build a style-specific prompt. Styles: `exec_brief`, `exec_deep`, `technical`, `bulleted`, `competitive_intel`, `custom`. Each gets a system prompt template + user prompt template; transcript inserted with inline timestamps `[MM:SS] text`.
- Call `llm.execute(task="summarize_exec_deep" if style=="exec_deep" else "summarize", ...)`.
- Parse `key_timestamps` from the response if `include_timestamps`. The prompt asks the model to return JSON with `summary` + `key_timestamps[]`; we tolerate plain prose summary if JSON parse fails.
- Persist to `summaries` table.
- Return.

Tests `tests/unit/test_summarize.py`.

### Group E — API surface additions

**E1 — `POST /v1/summarize`**

Route in `src/app/main.py`:
- Apply `enforce_rate_limit("summarize", request, redis_client)`.
- Body: spec §5.5 request schema. Add `SummarizeRequest` and `SummarizeResponse` in `schemas.py`.
- **Scope check pattern:** dependency `require_scopes("summarize")`. If `provider_override` is set on the body, additionally check `Token.has_scope("admin")` inline; raise `InsufficientScopeError("provider_override requires admin scope")` if missing.
- Validate style ∈ allowed set; `style=custom` requires non-null `custom_prompt`.
- Validate `provider_override` regex: `^(anthropic_direct|openai_direct|gemini_direct|llmapi)/[\w\-.]+$`. Pydantic field validator on `SummarizeRequest.provider_override`.
- Call `tasks.summarize.summarize(...)`.
- Return `SummarizeResponse` with `key_timestamps[]` enriched with per-entry `deep_link` (`https://youtu.be/<id>?t=<int_t>`).

**E2 — `GET /v1/transcript` include semantics**

The `include` query param already exists in the route. Phase 2 makes it actually work:
- `include=chapters`: after the orchestrator returns a transcript (cache hit or fresh captions), call `chapters.get_or_compute_chapters(session, video_id, language)` and merge into the response.
- `include=speakers`:
  - If `transcripts.has_diarization` is True, the cached snippets already have `speaker` populated — return 200 with speakers (NULL-OK per spec §5.5 "Returns nulls if data not yet computed").
  - If `has_diarization` is False AND transcript source is captions: return 200 with snippets and `has_diarization: false` plus a top-level note field `"diarization_status": "captions_source_unsupported"`. Do NOT enqueue (the diarization job will fail anyway per H6).
  - If `has_diarization` is False AND transcript source is whisper: side-effect on GET is awkward but acceptable here because the URL is keyed only by `v` (idempotent enqueue via SETNX lock). Enqueue a diarization job, return 200 transcript body with `diarization_status: "queued"` and `diarization_job_id: <id>` fields added to `TranscriptResponse`. Client polls `/v1/jobs/<id>` and re-fetches when complete. (Decision: NOT 202 — the transcript itself is available; diarization is enrichment that lands later. JC entry covers this.)

**`JobAcceptedResponse.job_type` field added** so clients can distinguish whisper vs enrichment jobs. Pydantic Literal: `Literal["whisper","enrichment"]`. Default `"whisper"` for backward-compat.

The merge step lives in the route handler in `main.py` (not in `transcript_service.py`) — this is a deliberate route-layer concern so the orchestrator stays focused on transcript availability.

Add tests covering all four include-speakers branches in `tests/unit/test_endpoint_transcript.py` extension.

### Group F — Worker entry point update

**F1 — `src/app/worker.py` + enrichment systemd unit**

- Register `run_diarization_job` (no chapters job for P2 — chapters compute synchronously inline; if too slow we revisit in P3).
- Add `make_enrichment_worker()` function for the enrichment queue using sync Redis (matches whisper-worker pattern).
- Add the worker's `_run_diarization_job_async` async pipeline (parallel to `_run_whisper_job_async`): preserves the `expires_at` and other transcript fields, only updates speakers and `has_diarization`.
- **Important**: Whisper worker must NOT clobber existing speaker tags. Update `_run_whisper_job_async` to call `cache.put_transcript` with `has_diarization=False` only when it's a fresh transcription (lock acquisition implies no concurrent diarization to clobber). Re-runs from `force=refresh` intentionally reset diarization.
- Update `deploy/yt-transcript-worker-enrichment.service` ExecStart to invoke `python -m app.worker enrichment` (a new CLI dispatcher in `worker.py` that picks the queue worker based on argv).

### Group G — Migration

**G1 — `alembic/versions/<ts>_p2_summaries_columns.py`**

The `summaries` table already exists from P1 baseline. P2 does NOT need a new DDL — all P2-P4 tables landed in P1 per the design plan to avoid migration churn. Verify and skip if true.

If we DO need a tweak (e.g., adding `audience_hash` index), add a small migration here.

### Group H — Skill update

**H1 — `deploy/skill/youtube-transcript/scripts/summarize.py` + SKILL.md**

Replace the stub with a real implementation:
- Reads `YT_SERVICE_URL` / `YT_SERVICE_TOKEN`.
- Accepts video URL/ID + `--style exec_brief|exec_deep|technical|bulleted|competitive_intel|custom` + `--audience` + `--max-tokens`.
- POSTs to `/v1/summarize`.
- Prints summary JSON to stdout.

`SKILL.md` updates (per spec §11.2 style auto-detection):
- New section "Choosing a summary style" with explicit mapping for Claude to follow:
  - "exec summary" / "executive summary" / "brief" → `exec_brief`
  - "deep dive" / "executive deep dive" / "detailed exec" → `exec_deep`
  - "technical" / "engineering" / "implementation details" → `technical`
  - "bullets" / "bulleted" / "list" → `bulleted`
  - "competitive" / "compete" / "vs <competitor>" → `competitive_intel`
  - Anything not matching → `exec_brief` (safe default).
- New section "Using deep links in summaries": when the response includes `key_timestamps[]`, each entry has a `deep_link` field — render quotes as `"[label](deep_link)"`.

---

## Execution order

```
A0-1 (cache helpers) → A0-2 (jobs generalization)
  ↓
A1 (LLM scaffold)
  ↓
B1 (chapters)   C1 (diarization)   D1 (summarize)      ← parallel
  ↓                                  ↓
E1 (summarize route)   E2 (include semantics)          ← parallel
  ↓
F1 (worker register + systemd)   H1 (skill summarize)  ← parallel
```

---

## Pinned implementation details

| ID | Detail |
|---|---|
| P2-1 | LLM cost cap check at start of every `execute()`, cached 60s in-memory. |
| P2-2 | Provider client wrappers all return the same `LLMResponse` dataclass. |
| P2-3 | Chapter LLM prompt MUST request JSON; we fall back to no-chapters on parse error rather than erroring. |
| P2-4 | Diarization is best-effort: a failure marks the job failed but the transcript still serves. |
| P2-5 | Summary cache TTL = `summary_cache_ttl_days` (default 90, per spec). |
| P2-6 | `provider_override` is admin-only. Route checks scope at the dependency level. |
| P2-7 | `include=speakers` returning 202 when diarization not yet computed is consistent with the existing whisper-202 pattern. |
| P2-8 | `audience_hash` and `custom_hash` are SHA-256 hex digests of the raw input strings (lowercased, stripped). Empty string → SHA-256 of empty. |

## Definition of done (P2)

- `pytest -m "not integration"` green locally; count > 260.
- `ruff check src tests` green.
- All §12 Phase 2 acceptance bullets satisfied at code level.
- Codex review: no blockers.
- Conventional commit message listing satisfied bullets.

## Known gaps deferred

- Chapter computation latency: if the LLM chapter derivation is slow (>10s), users see a hang on first `include=chapters` call. Acceptable for P2; revisit in P3 once we have real workloads.
- LLM provider routing burn-in plan stays at default for P2; full burn-in (LLMAPI as primary for topics) lands in P4.
