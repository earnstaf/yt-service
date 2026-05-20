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

### JC-013 — Skill targets localhost for P1, switches to prod URL via env
- **Decision:** Skill reads `YT_SERVICE_URL` env var; defaults to `https://yt.ericmax.com`. P1 local testing sets `YT_SERVICE_URL=http://127.0.0.1:8765`.
- **Alternative:** Hardcoded prod URL with comment to change for testing.
- **Rationale:** Same skill artifact works for dev and prod. Token read from `YT_SERVICE_TOKEN` env var. No hardcoded creds anywhere.
