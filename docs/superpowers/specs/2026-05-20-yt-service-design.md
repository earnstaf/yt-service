# yt-transcript-service — Build Design (delta on spec v3)

**Date:** 2026-05-20
**Spec:** `docs/specs/yt-transcript-service-spec.md` (canonical)
**Status:** Approved, autonomous build in progress

This document captures only the **delta** between the canonical spec v3 and what we are actually building in this session. Everything in spec v3 is in effect unless called out here.

## 1. Confirmed scope

- **All four phases** (P1, P2, P3, P4) built sequentially in one session.
- **Local Windows dev** for the codebase, **remote VPS deploy** via SSH after each phase passes locally.
- **Repo root** = `D:\Claude Projects\Tools\yt-service\` (this directory).
- **Target host** = SE Job Scraper VPS at `yt.ericmax.com`.

## 2. Deviations from spec v3

| Spec default | This build | Reason |
|---|---|---|
| `FEATURE_SENTIMENT=false` | `FEATURE_SENTIMENT=true` | User wants sentiment usable on day one. |
| LLMAPI fallback-only, 14-day burn-in | LLMAPI **primary** for `topics`, `gemini_direct` first fallback | Aggressive cost optimization on a low-stakes task. Burn-in still required before adding more tasks. |
| Single VPS-side dev | Local-first dev + remote deploy | Iteration speed locally, real smoke tests on host. |

All other tasks (`summarize`, `summarize_exec_deep`, `sentiment`, `diff`) keep the spec's routing.

## 3. Build sequence

Per-phase loop, fully autonomous:
1. **Plan** → write `docs/plans/phaseN-plan.md`
2. **Plan review** → dispatch a sub-agent to critique the plan
3. **Plan adjustment** → fold in feedback we agree with
4. **Build** → subagent-driven implementation, one task at a time, fresh context per task
5. **Code review** → Codex review of the diff
6. **Fix** → address Codex findings
7. **Commit** → conventional commit on `main`

Order: setup → P1 → P1 VPS deploy → P2 → redeploy → P3 → redeploy → P4 → final redeploy.

## 4. Locked architectural decisions

Items the spec describes but leaves implementation-flexible. Pinning them now so subagents do not drift.

| Decision | Choice | Lives in |
|---|---|---|
| ORM | SQLAlchemy 2.0 async with `asyncpg` | `app/models.py` |
| Migrations | Alembic, one revision per phase boundary | `alembic/versions/` |
| Settings | `pydantic-settings`, single `Settings` object | `app/config.py` |
| Logging | `structlog` JSON to stdout with redaction processor | `app/logging.py` |
| Job IDs | ULIDs via `python-ulid` | `app/jobs.py` |
| Job status SoT | Postgres `jobs` table (Redis is the queue, not source of truth) | `app/jobs.py`, `app/worker.py` |
| Test stack | pytest + pytest-asyncio + respx + fakeredis + pytest-postgresql | `tests/` |
| Coverage target | 85% on `src/app` | `pyproject.toml` |
| Token format | `yt_` + 32 random urlsafe bytes; argon2id hash stored | `app/auth.py` |
| Webhook secret | per-token, 32 random bytes hex; HMAC-SHA256 over raw body | `app/webhooks.py` |
| Concurrency lock | Redis `SETNX lock:{op}:{video_id}` with 1h TTL | `app/jobs.py` |
| Audio storage | `/var/tmp/yt-transcript/{job_id}.{ext}`, deleted in `finally` | `app/whisper/audio.py` |
| LLMAPI routing | `topics` primary = LLMAPI; everything else per spec §7.3 | `app/llm/routing.py` |
| Bootstrap tokens | first admin token created by CLI; `claude-ai` skill token has `read,batch,summarize,intelligence` only | `app/admin.py` |
| Daily cost guard | `MAX_DAILY_LLM_COST_USD` hard-stop checked in `llm.execute()` | `app/llm/fallback.py` |
| Audio chunking | OpenAI Whisper split at `WHISPER_CHUNK_BYTES` (20 MB default) | `app/whisper/audio.py` |

## 5. Module boundaries (file ownership)

Reinforced so parallel subagents don't collide.

- `app/parsing.py` — pure functions, URL → IDs, no I/O.
- `app/cache.py` — Postgres reads/writes for transcripts/summaries/topics/sentiment. Imports nothing from `youtube.py` or `whisper/`.
- `app/youtube.py` — captions library + yt-dlp metadata. Returns dataclasses, callers persist.
- `app/whisper/` — backend dispatch + audio handling. Returns dataclasses.
- `app/llm/` — the only place that knows about provider API keys. Every LLM call routes through `llm.execute(task, prompt, ...)`.
- `app/tasks/` — task-specific orchestration. Composes `llm.execute()` + prompts + parsing + `cache.py`.
- `app/jobs.py` — enqueue + status. The only file that touches RQ from the API process.
- `app/worker.py` — entry points for each worker queue.
- `app/main.py` — FastAPI routes. Thin; logic lives in service modules.

Heuristic: if a route imports across three module families directly, refactor to a service module in between.

## 6. Risks acknowledged up front

| Risk | Mitigation in v1 |
|---|---|
| HuggingFace pyannote gating | Document HF account/terms steps in `deploy/HUGGINGFACE_SETUP.md`. Diarization disabled gracefully if model load fails. |
| YouTube blocks VPS IP | `YT_HTTPS_PROXY` slot empty by default. README notes Zyte Smart Proxy Manager (drop-in) and Webshare as known options. Zyte API itself is **not** a drop-in for the captions library. |
| OpenAI Whisper 25MB cap | Audio chunked at `WHISPER_CHUNK_BYTES=20MB`. Concatenated server-side. `faster-whisper` fallback has no such cap. |
| All LLM providers fail | Return 502 `llm_failed`, cached results still served. |
| Cost overrun from monitors | `MAX_DAILY_LLM_COST_USD` hard-stop applies to every LLM call. |
| Disk fill from yt-dlp | `yt_dlp` `max_filesize` cap, `finally` cleanup, nightly orphan sweep cron. |
| Smoke test stable videos | `scripts/smoke_test.sh` uses two hardcoded URLs; replace with channel videos the user controls once we have them. |

## 7. Skill (P1 deliverable)

`deploy/skill/youtube-transcript/SKILL.md` plus three scripts. Recognizes any YouTube URL form. Routes channel/playlist URLs to `/v1/ingest` in P3+. Picks summarize style from phrasing in P2+. Uses `snippets[].deep_link` from P2+. Token never logged.

## 8. Acceptance gates

The spec's §12 acceptance criteria are the gate per phase. Each phase commit message ends with the bullets it satisfies. Codex code review runs on the full phase diff before commit.

## 9. Out of scope for this session

- `/v1/usage` endpoint (backlog).
- Full-text search activation (column reserved, no endpoint).
- Semantic search via pgvector (column reserved, no endpoint).
- Translation.
- Live stream transcription.
- OCR.
- Speaker name resolution.
- Web UI.
- MCP server wrapping.
