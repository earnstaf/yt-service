# YouTube Transcript Intelligence Platform — Design Spec v3

**Owner:** Eric Arnst
**Target host:** Existing DigitalOcean VPS (same box as SE Job Scraper, isolated service)
**Purpose:** Production-grade transcript and intelligence API for YouTube content. Captions when available, Whisper otherwise. Layered with chaptering, diarization, topic extraction, summarization, diff, sentiment, channel/playlist ingestion, and scheduled monitoring. Consumable by Claude.ai and downstream services.
**Status:** Ready to build, phased delivery.

---

## 1. Goals

1. Deterministic, fast transcript retrieval for any YouTube VOD, captions or audio-derived.
2. Cached, idempotent, batch-friendly, webhook-capable, designed for chaining.
3. Add intelligence layers on top of transcripts (topics, entities, summaries, diffs, sentiment).
4. Multi-provider LLM with per-task routing, direct API primary, LLMAPI as resilience fallback.
5. Observable, rate-limited, scoped tokens, restartable in seconds.
6. Phased delivery so Claude Code can ship working increments without rework.

## 2. Non-goals (v1)

- No public multi-tenant. Single-org, scoped tokens.
- No real-time / live stream transcription.
- No web UI for end users.
- No OCR of on-screen text.
- No translation endpoint.
- No video downloads for non-transcription purposes.

## 3. Build phases

Each phase is independently shippable. Acceptance criteria gate the move to the next phase.

| Phase | Scope | Why it ships separately |
|---|---|---|
| **P1 — Core** | API skeleton, auth, parsing, captions, Whisper, cache, jobs, batch, webhooks, skill | Everything below depends on these primitives. Skill ships here so Eric can use the service immediately. |
| **P2 — Enrichment** | Chapter detection, speaker diarization, timestamped deep links, deferred-summary endpoint | Pure additions to the transcript schema. No behavior changes for P1 callers. |
| **P3 — Discovery** | Channel/playlist ingestion, scheduled monitoring (RSS poller + callbacks) | Pulls *more* content through the pipeline. Useless without P1 stable. |
| **P4 — Intelligence** | Topic/entity extraction, sentiment (flagged), diff mode, multi-provider routing | Needs the LLM provider layer; needs cached transcripts to operate on. |

Total estimated effort: P1 ~3-5k LOC, P2 ~1k LOC, P3 ~1k LOC, P4 ~1.5k LOC.

## 4. Architecture

```
                      Claude.ai web_fetch / other clients / cron
                                      |
                                      | HTTPS + Bearer
                                      v
                          yt.<domain>  (Caddy or nginx, TLS)
                                      |
                                      v
                          FastAPI app  (127.0.0.1:8765)
                            |       |        |
              .-------------'       |        '----------------.
              v                     v                         v
       Postgres            Redis (queue, locks)        LLM Provider Layer
       (cache, jobs,              |                     |        |        |
        chapters, topics,         v                     v        v        v
        embeddings,         RQ workers (N)        Anthropic  OpenAI   Gemini   LLMAPI
        sentiment,                |                                  (fallback)
        monitors,                 v
        tokens)        +----------+-----------+--------+
                       v          v           v        v
                   captions    yt-dlp     Whisper   diarization
                   (library)   (audio)    backends  (pyannote)
```

- **API process:** FastAPI / uvicorn, 2 workers. HTTP handling, cache reads, job enqueue, LLM orchestration.
- **Worker pool:** RQ with priority queues: `default`, `whisper`, `enrichment`, `intelligence`. Each can be a separate systemd unit so heavy work doesn't block fast work.
- **Postgres:** Shared instance, own database `yt_transcript`, own user.
- **Redis:** Job queue, distributed locks, optional response cache.
- **LLM layer:** Provider registry + per-task routing. Direct APIs primary, LLMAPI fallback.

## 5. API surface

All endpoints under `/v1` unless noted. JSON in/out except `format=text|srt`.

### 5.1 Phase 1 endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/v1/transcript` | Sync fetch (cache, captions, Whisper trigger) |
| `POST` | `/v1/transcript:batch` | Submit ≤50 videos at once |
| `GET`  | `/v1/jobs/{job_id}` | Poll job status |
| `DELETE` | `/v1/cache/{video_id}` | Admin-only cache purge per video |
| `GET`  | `/v1/cache/stats` | Cache summary |
| `GET`  | `/healthz`, `/readyz` | Liveness, readiness |
| `GET`  | `/metrics` | Prometheus format, loopback-only |

### 5.2 Phase 2 endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/v1/transcript` | Now returns `chapters[]` and `snippets[].speaker` when available |
| `POST` | `/v1/summarize` | On-demand summarization of a cached transcript (deferred LLM, see §5.5) |

### 5.3 Phase 3 endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/ingest` | Channel or playlist URL → expand to all videos → enqueue |
| `POST` | `/v1/monitors` | Register a channel/playlist for ongoing polling + callback |
| `GET`  | `/v1/monitors` | List active monitors |
| `DELETE` | `/v1/monitors/{id}` | Stop a monitor |

### 5.4 Phase 4 endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/topics` | Extract topics + entities for a video (idempotent, cached) |
| `POST` | `/v1/sentiment` | Sentiment timeline for a video (flagged, opt-in) |
| `POST` | `/v1/diff` | Structural diff between two videos |
| `GET`  | `/v1/usage` | (Backlog — token usage + LLM cost per consumer) |

### 5.5 Endpoint contracts

#### `GET /v1/transcript` (P1, augmented in P2)

**Query params:**

| Name | Required | Default | Notes |
|---|---|---|---|
| `v` | yes | — | Video ID or any YouTube URL form |
| `lang` | no | `en` | Preferred caption language; ignored for Whisper |
| `format` | no | `json` | `json` \| `text` \| `srt` |
| `force` | no | — | `whisper` (bypass captions) \| `refresh` (bypass cache) |
| `wait` | no | `0` | Seconds to wait on running Whisper job before 202. Max 25. |
| `include` | no | — | Comma list: `chapters`,`speakers`,`topics`,`sentiment`. Returns nulls if data not yet computed. |

**200 (captions or cached):**

```json
{
  "video_id": "OMhKgQmeMhI",
  "source": "youtube_captions",
  "language": "en",
  "is_generated": true,
  "duration_seconds": 1847.3,
  "snippet_count": 412,
  "cached_at": "2026-05-20T14:02:11Z",
  "cache_hit": true,
  "chapters": [
    {"start": 0.0, "end": 245.0, "title": "Opening keynote"},
    {"start": 245.0, "end": 612.0, "title": "Product announcements"}
  ],
  "snippets": [
    {"start": 0.0, "duration": 4.2, "text": "Welcome to the keynote", "speaker": "SPEAKER_00", "deep_link": "https://youtu.be/OMhKgQmeMhI?t=0"}
  ],
  "full_text": "Welcome to the keynote..."
}
```

`source` ∈ {`youtube_captions`, `whisper_openai`, `whisper_local`}. `chapters` and `speaker` populated only when computed (P2). `deep_link` always present (computed client-side or server-side from start time).

**202 (Whisper job started):**

```json
{
  "job_id": "01HXY3...",
  "status": "queued",
  "video_id": "OMhKgQmeMhI",
  "poll_url": "/v1/jobs/01HXY3...",
  "estimated_seconds": 90
}
```

#### `POST /v1/transcript:batch` (P1)

```json
{
  "videos": ["OMhKgQmeMhI", "https://youtu.be/abc..."],
  "lang": "en",
  "include": ["chapters","speakers"],
  "callback_url": "https://other.service/yt-callback"
}
```

Response: array of per-video objects (200- or 202-shaped per §5.5). Callback fired per video on completion with HMAC-SHA256 signature.

#### `POST /v1/summarize` (P2)

```json
{
  "video_id": "OMhKgQmeMhI",
  "style": "exec_brief",       // exec_brief | exec_deep | technical | bulleted | competitive_intel | custom
  "audience": "SE team",       // free-form, fed into prompt
  "custom_prompt": null,       // required when style=custom
  "max_tokens": 800,
  "include_timestamps": true,
  "provider_override": null    // admin-only, e.g. "llmapi/claude-sonnet-4-6"
}
```

Response:

```json
{
  "video_id": "OMhKgQmeMhI",
  "style": "exec_brief",
  "audience": "SE team",
  "summary": "...",
  "key_timestamps": [{"t": 412, "label": "Pricing announcement"}],
  "provider_used": "anthropic_direct/claude-sonnet-4-6",
  "tokens_in": 18420,
  "tokens_out": 612,
  "cost_usd": 0.0571,
  "cached": false
}
```

Summaries cached by `(video_id, style, audience_hash, custom_prompt_hash)`. Cache TTL 90 days.

#### `POST /v1/ingest` (P3)

```json
{
  "url": "https://www.youtube.com/@TaniumOfficial",
  "max_videos": 100,
  "since": "2026-01-01",
  "include": ["chapters","speakers","topics"],
  "callback_url": "..."
}
```

Returns `ingest_id` and a list of resolved video IDs that will be processed.

#### `POST /v1/monitors` (P3)

```json
{
  "channel_url": "https://www.youtube.com/@CrowdStrike",
  "poll_interval_minutes": 60,
  "include": ["chapters","topics","summary:exec_brief"],
  "callback_url": "https://hooks.example.com/yt-new-video",
  "notes": "competitive tracking"
}
```

Returns monitor ID. Poller hits the channel RSS feed, enqueues new videos, fires the callback on each completion. Survives restart (persisted in `monitors` table).

#### `POST /v1/topics` (P4)

```json
{ "video_id": "OMhKgQmeMhI", "refresh": false }
```

Response:

```json
{
  "video_id": "OMhKgQmeMhI",
  "topics": ["endpoint security", "MDR pricing", "AI in security operations"],
  "entities": {
    "companies": ["Tanium", "CrowdStrike"],
    "people": ["Orion Hindawi"],
    "products": ["Tanium XEM", "Falcon Insight"]
  },
  "claims": [
    {"text": "Tanium reduced MTTR by 60%", "t": 412}
  ],
  "questions_raised": ["How does pricing compare to per-endpoint MDR?"],
  "provider_used": "gemini_direct/gemini-2.5-flash",
  "cached": false
}
```

Cached by `video_id`. Auto-computed on ingestion when `include` requests topics.

#### `POST /v1/sentiment` (P4, flagged)

```json
{ "video_id": "OMhKgQmeMhI", "granularity": "chapter" }   // overall | chapter | snippet
```

Response:

```json
{
  "video_id": "OMhKgQmeMhI",
  "granularity": "chapter",
  "overall": {"score": 0.42, "label": "slightly_positive"},
  "timeline": [
    {"start": 0, "end": 245, "score": 0.61, "label": "positive"},
    {"start": 245, "end": 612, "score": -0.12, "label": "neutral"}
  ],
  "provider_used": "gemini_direct/gemini-2.5-flash"
}
```

Disabled by default at the server level via `FEATURE_SENTIMENT=false`. Eric flips to `true` to enable.

#### `POST /v1/diff` (P4)

```json
{
  "video_a": "abc...",
  "video_b": "xyz...",
  "focus": "topics_and_emphasis"   // topics_and_emphasis | exact_changes | competitive_positioning
}
```

Response: structured diff with sections added, removed, and shifted in emphasis; key quotes from each side; LLM-generated executive summary of the delta.

### 5.6 Error codes (full)

| Status | `error` | When |
|---|---|---|
| 400 | `invalid_video_id` | Cannot parse |
| 400 | `invalid_request` | Schema validation failure |
| 400 | `invalid_channel` | Channel/playlist URL cannot be resolved |
| 401 | `unauthorized` | Missing/bad token |
| 403 | `insufficient_scope` | Token lacks required scope |
| 403 | `feature_disabled` | Server has feature flag off (e.g., sentiment) |
| 404 | `video_unavailable` | Private, deleted, region-blocked |
| 404 | `no_audio` | No audio stream |
| 404 | `not_found` | Resource (job/monitor/transcript) doesn't exist |
| 409 | `job_in_progress` | Duplicate submit, returns existing job_id |
| 413 | `video_too_long` | Exceeds `MAX_VIDEO_DURATION_SECONDS` (default 4h) |
| 413 | `batch_too_large` | > 50 videos in batch |
| 429 | `rate_limited` | Per-token or per-IP; includes `Retry-After` |
| 502 | `youtube_ip_blocked` | `IpBlocked` from captions library |
| 502 | `whisper_failed` | All Whisper backends failed |
| 502 | `llm_failed` | All LLM providers failed for this task |
| 503 | `queue_full` | Queue depth threshold exceeded |
| 500 | `internal_error` | Anything else, logged with trace |

## 6. Auth and scopes

- Bearer tokens in `Authorization: Bearer <token>`.
- Scopes: `read`, `batch`, `summarize`, `intelligence` (topics/sentiment/diff), `monitor` (create/manage monitors), `admin`.
- Token rows in Postgres: id, name, argon2 hash, scopes, webhook_secret, rate_overrides, timestamps, revoked_at.
- CLI: `python -m app.admin tokens create --name claude-ai --scopes read,batch,summarize,intelligence`. Prints token once; only hash persisted.

Default rate limits (override per token):

| Scope | Limit |
|---|---|
| Read | 60/min |
| Batch | 10/min (≤50 videos each) |
| Summarize | 30/min |
| Intelligence | 20/min |
| Whisper jobs | 30/hour |
| Monitor creates | 10/hour |

## 7. Implementation

### 7.1 Stack

- Python 3.11+
- `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`
- `youtube-transcript-api>=1.0`, `yt-dlp`
- `openai>=1.0`, `anthropic>=0.40`, `google-generativeai`
- `faster-whisper` (local fallback), `openai` SDK for OpenAI Whisper
- `pyannote.audio` (diarization, P2)
- `rq`, `redis`, `apscheduler` (RSS polling, P3)
- `sqlalchemy[asyncio]`, `asyncpg`, `alembic`
- `structlog`, `prometheus-client`, `slowapi`, `argon2-cffi`
- `feedparser` (RSS, P3)

### 7.2 Project layout

```
/opt/yt-transcript/src/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── auth.py
│   ├── parsing.py
│   ├── cache.py
│   ├── youtube.py
│   ├── chapters.py              # P2
│   ├── deep_links.py            # P2
│   ├── whisper/
│   │   ├── openai_backend.py
│   │   ├── local_backend.py
│   │   └── audio.py
│   ├── diarization.py           # P2
│   ├── ingest.py                # P3
│   ├── monitors.py              # P3
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── providers.py         # Registry
│   │   ├── routing.py           # Task → provider/model
│   │   ├── anthropic_client.py
│   │   ├── openai_client.py
│   │   ├── gemini_client.py
│   │   ├── llmapi_client.py     # OpenAI-compatible wrapper
│   │   ├── fallback.py          # Chain executor
│   │   └── cost.py              # Cost tracking
│   ├── tasks/
│   │   ├── summarize.py         # P2/P4
│   │   ├── topics.py            # P4
│   │   ├── sentiment.py         # P4
│   │   └── diff.py              # P4
│   ├── jobs.py
│   ├── worker.py
│   ├── webhooks.py
│   ├── admin.py
│   ├── models.py
│   ├── schemas.py
│   ├── metrics.py
│   └── logging.py
├── alembic/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── deploy/
│   ├── yt-transcript.service
│   ├── yt-transcript-worker-default.service
│   ├── yt-transcript-worker-whisper.service
│   ├── yt-transcript-worker-enrichment.service
│   ├── yt-transcript-worker-intelligence.service
│   ├── yt-transcript-monitor.service          # P3 scheduler
│   ├── Caddyfile.snippet
│   └── nginx.conf.snippet
├── scripts/
│   ├── install.sh
│   ├── make_token.sh
│   ├── smoke_test.sh
│   └── benchmark_llmapi.sh                    # A/B helper
├── .env.example
├── requirements.txt
├── pyproject.toml
├── alembic.ini
└── README.md
```

### 7.3 LLM provider abstraction

`app/llm/providers.py`:

```python
PROVIDERS = {
    "anthropic_direct": {
        "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openai_direct": {
        "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "gemini_direct": {
        "type": "gemini",
        "api_key_env": "GEMINI_API_KEY",
    },
    "llmapi": {
        "type": "openai_compatible",
        "base_url": "https://api.llmapi.ai/v1",
        "api_key_env": "LLMAPI_API_KEY",
        "optional": True,    # If env var missing, provider is skipped silently
    },
}
```

`app/llm/routing.py`:

```python
TASK_ROUTING = {
    "summarize": {
        "primary": "anthropic_direct/claude-sonnet-4-6",
        "fallbacks": ["llmapi/claude-sonnet-4-6", "openai_direct/gpt-4o"],
    },
    "summarize_exec_deep": {
        "primary": "anthropic_direct/claude-opus-4-7",
        "fallbacks": ["openai_direct/gpt-4o", "llmapi/claude-opus-4-7"],
    },
    "topics": {
        "primary": "gemini_direct/gemini-2.5-flash",
        "fallbacks": ["llmapi/gemini-2.5-flash", "openai_direct/gpt-4o-mini"],
    },
    "sentiment": {
        "primary": "gemini_direct/gemini-2.5-flash",
        "fallbacks": ["llmapi/gemini-2.5-flash"],
    },
    "diff": {
        "primary": "anthropic_direct/claude-sonnet-4-6",
        "fallbacks": ["openai_direct/gpt-4o", "llmapi/claude-sonnet-4-6"],
    },
}
```

`app/llm/fallback.py`: executes the chain. On any failure (timeout, 5xx, rate limit, unavailable model), moves to next provider. Each call wrapped with: timeout, retry-once, structured log of provider/model/latency/tokens/cost, increment `yt_llm_calls_total{task,provider,model,status}` metric.

**LLMAPI burn-in plan:**

1. P4 ships with LLMAPI in `fallbacks` slot only.
2. After 14 days of stable operation, flip `topics` primary to `llmapi/gemini-2.5-flash`, push `gemini_direct` to first fallback.
3. Compare cost and quality over 7 days. If LLMAPI is within 10% latency, within 5% accuracy on spot-checked outputs, and cheaper, expand to `sentiment` next. Otherwise revert.
4. Never make LLMAPI primary for `summarize`, `summarize_exec_deep`, or `diff` until burn-in is proven on lower-stakes tasks.

`provider_override` query parameter on summarize/topics/diff (admin scope only) allows manual one-off routing through any provider for spot-checks.

### 7.4 Cache schema

```sql
CREATE TABLE transcripts (
    video_id      TEXT NOT NULL,
    language      TEXT NOT NULL,
    source        TEXT NOT NULL,
    is_generated  BOOLEAN NOT NULL,
    duration_seconds REAL,
    snippets_jsonb JSONB NOT NULL,
    full_text     TEXT NOT NULL,
    chapters_jsonb JSONB,
    has_diarization BOOLEAN DEFAULT FALSE,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (video_id, language)
);
CREATE INDEX idx_transcripts_expires ON transcripts(expires_at);
CREATE INDEX idx_transcripts_source ON transcripts(source);

-- Reserved for MCP-era search; columns added now to avoid migrations later
ALTER TABLE transcripts ADD COLUMN full_text_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', full_text)) STORED;
CREATE INDEX idx_transcripts_fts ON transcripts USING GIN(full_text_tsv);

ALTER TABLE transcripts ADD COLUMN embedding vector(1536);  -- pgvector, nullable

CREATE TABLE summaries (
    video_id      TEXT NOT NULL,
    style         TEXT NOT NULL,
    audience_hash TEXT NOT NULL,
    custom_hash   TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL,
    key_timestamps_jsonb JSONB,
    provider_used TEXT NOT NULL,
    tokens_in     INT,
    tokens_out    INT,
    cost_usd      NUMERIC(10,6),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (video_id, style, audience_hash, custom_hash)
);

CREATE TABLE topics (
    video_id      TEXT PRIMARY KEY,
    topics_jsonb  JSONB NOT NULL,
    entities_jsonb JSONB NOT NULL,
    claims_jsonb  JSONB,
    questions_jsonb JSONB,
    provider_used TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE sentiment (
    video_id      TEXT NOT NULL,
    granularity   TEXT NOT NULL,
    overall_score REAL NOT NULL,
    overall_label TEXT NOT NULL,
    timeline_jsonb JSONB,
    provider_used TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (video_id, granularity)
);

CREATE TABLE jobs (
    job_id        TEXT PRIMARY KEY,
    video_id      TEXT NOT NULL,
    job_type      TEXT NOT NULL,   -- whisper | enrichment | intelligence
    status        TEXT NOT NULL,   -- queued | running | complete | failed
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    error         TEXT,
    token_id      TEXT NOT NULL,
    callback_url  TEXT,
    payload_jsonb JSONB
);

CREATE TABLE monitors (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    channel_url   TEXT NOT NULL,
    poll_interval_minutes INT NOT NULL,
    include_jsonb JSONB NOT NULL,
    callback_url  TEXT NOT NULL,
    notes         TEXT,
    last_polled_at TIMESTAMPTZ,
    last_video_id TEXT,
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paused        BOOLEAN DEFAULT FALSE
);

CREATE TABLE tokens (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL,
    scopes        TEXT[] NOT NULL,
    webhook_secret TEXT,
    rate_overrides JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);

CREATE TABLE llm_call_log (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL,
    latency_ms    INT,
    tokens_in     INT,
    tokens_out    INT,
    cost_usd      NUMERIC(10,6),
    video_id      TEXT,
    token_id      TEXT,
    error         TEXT
);
CREATE INDEX idx_llm_log_ts ON llm_call_log(ts DESC);
CREATE INDEX idx_llm_log_provider ON llm_call_log(provider, task);
```

The tsvector and embedding columns are reserved now so MCP-server work doesn't require migrations.

### 7.5 Chapter detection (P2)

Two sources:
1. **YouTube-provided chapters** via `yt-dlp` (metadata only, no audio download): `--skip-download --print-json`. Parses `chapters` array from response.
2. **LLM-derived fallback** when YouTube has none: prompt Claude/Gemini with full transcript, ask for 4-12 logical sections with start times rounded to nearest snippet boundary.

Persisted in `transcripts.chapters_jsonb`. Computed lazily on first request with `include=chapters` if not already cached.

### 7.6 Speaker diarization (P2)

- `pyannote.audio` 3.x pipeline, requires HuggingFace token (gated model, free).
- Runs on the same audio file Whisper used (kept in tmp during job, then deleted).
- Output aligned with Whisper snippets via timestamp overlap.
- `snippets[].speaker` populated as `SPEAKER_00`, `SPEAKER_01`, etc. No name resolution.
- For captions-only path, diarization requires triggering audio download separately. Document this in API response: `has_diarization: false` until `?force=diarize` is called.
- Expensive (~0.5x realtime on CPU). Runs on `enrichment` worker queue.

### 7.7 Deep links (P2)

Server-side computation when serving transcript:

```python
def deep_link(video_id: str, start_seconds: float) -> str:
    return f"https://youtu.be/{video_id}?t={int(start_seconds)}"
```

Added to every snippet in P2 and beyond. Skill uses these when quoting passages.

### 7.8 Channel/playlist ingestion (P3)

`yt-dlp --flat-playlist --print-json <channel_or_playlist_url>` enumerates videos without downloading. Filter by `since` date, cap at `max_videos`. Enqueue each as a normal transcript job with the requested `include` flags. Single `ingest_id` tracks the batch.

### 7.9 Scheduled monitoring (P3)

- APScheduler in a dedicated systemd unit `yt-transcript-monitor.service`.
- Every monitor polled at its configured interval.
- Poller hits `https://www.youtube.com/feeds/videos.xml?channel_id=<id>`. Parses with `feedparser`. Compares latest video IDs against `last_video_id`.
- New videos enqueue full transcript + requested enrichments + summary if requested.
- On completion of each new video, fire monitor's `callback_url` with the full result.
- Failure modes: RSS feed returns nothing → log + retry next interval; channel deleted → mark monitor as `paused`, alert via metric.

### 7.10 Topic extraction (P4)

Prompt template (sketch):

```
You are extracting structured intelligence from a video transcript.

Return JSON only, matching this schema:
{ "topics": [string, ...], "entities": {"companies": [string], "people": [string], "products": [string]}, "claims": [{"text": string, "approximate_timestamp_seconds": int}], "questions_raised": [string] }

Topics: 3-8 high-level themes. Entities: only those explicitly named. Claims: factual or comparative assertions ("X is faster than Y", "we reduced cost by 30%"). Questions: open questions the speaker raised or hung in the air.

Transcript:
{full_text_with_inline_timestamps}
```

Stored in `topics` table. Idempotent unless `refresh=true`. Auto-runs when ingestion requests `include=topics`.

### 7.11 Sentiment (P4, flagged)

Server-level feature flag: `FEATURE_SENTIMENT=false` by default. When false, endpoint returns 403 `feature_disabled`.

When enabled: send chapter-bounded or snippet-bounded chunks to Gemini Flash with a prompt asking for `{score: -1.0..1.0, label: negative|neutral|positive}`. Aggregate overall as duration-weighted average. Persist to `sentiment` table.

### 7.12 Diff mode (P4)

Two videos, both must be cached (or trigger transcription first). LLM prompted with both transcripts (chunked if needed via map-reduce). Output schema:

```json
{
  "video_a": "...", "video_b": "...",
  "focus": "topics_and_emphasis",
  "added_in_b": [{"topic": "...", "evidence": "..."}],
  "removed_from_a": [...],
  "shifted_emphasis": [{"topic": "...", "direction": "more|less", "delta_pct": int}],
  "key_quotes_a": [...], "key_quotes_b": [...],
  "executive_summary": "..."
}
```

### 7.13 Audio download

```python
yt_dlp.YoutubeDL({
    "format": "bestaudio[ext=m4a]/bestaudio",
    "outtmpl": f"{TMP_DIR}/%(id)s.%(ext)s",
    "quiet": True, "noplaylist": True,
    "max_filesize": 500 * 1024 * 1024,
})
```

Audio deleted after Whisper completes (and after diarization if also queued). Never persisted.

### 7.14 Concurrency

- Redis `SETNX` lock per `(video_id, operation)` (e.g., `lock:whisper:abc`, `lock:diarize:abc`).
- Duplicate request for in-flight operation returns existing job_id.

### 7.15 Webhooks

POST to `callback_url`. Headers:
- `X-YT-Signature: sha256=<hex>` (HMAC over body with per-token secret)
- `X-YT-Job-Id`, `X-YT-Video-Id`, `X-YT-Event` (e.g., `transcript.complete`, `monitor.new_video`)
- Retry: 3 attempts, exp backoff 10s/60s/300s.

### 7.16 Logging

Structlog JSON to stdout. Every request: ts, level, event, method, path, video_id, token_id, status, source, cache_hit, latency_ms. Every LLM call: task, provider, model, latency, tokens_in, tokens_out, cost_usd, status. Never log token values, full transcripts, or audio paths.

## 8. Deployment

### 8.1 System user

```bash
sudo useradd --system --home /opt/yt-transcript --shell /usr/sbin/nologin yttranscript
```

### 8.2 Postgres

```bash
sudo -u postgres psql -c "CREATE DATABASE yt_transcript;"
sudo -u postgres psql -c "CREATE USER yttranscript WITH PASSWORD '<gen>';"
sudo -u postgres psql -c "GRANT ALL ON DATABASE yt_transcript TO yttranscript;"
sudo -u postgres psql -d yt_transcript -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 8.3 Redis

Existing instance, dedicated DB index (e.g., 3). Bind 127.0.0.1 only.

### 8.4 Install

```bash
sudo mkdir -p /opt/yt-transcript /var/tmp/yt-transcript
sudo chown yttranscript:yttranscript /opt/yt-transcript /var/tmp/yt-transcript
sudo -u yttranscript git clone <repo> /opt/yt-transcript/src
cd /opt/yt-transcript/src
sudo -u yttranscript python3 -m venv /opt/yt-transcript/venv
sudo -u yttranscript /opt/yt-transcript/venv/bin/pip install -r requirements.txt
sudo -u yttranscript /opt/yt-transcript/venv/bin/alembic upgrade head
sudo bash scripts/install.sh
```

### 8.5 `.env` (mode 0600, owned by yttranscript)

```bash
# Server
YT_BIND_HOST=127.0.0.1
YT_BIND_PORT=8765
YT_LOG_LEVEL=info

# Postgres
DATABASE_URL=postgresql+asyncpg://yttranscript:<pw>@localhost/yt_transcript

# Redis
REDIS_URL=redis://localhost:6379/3

# Cache
CACHE_TTL_DAYS=30
SUMMARY_CACHE_TTL_DAYS=90
MAX_VIDEO_DURATION_SECONDS=14400

# Whisper
WHISPER_BACKEND=openai
WHISPER_OPENAI_MODEL=whisper-1
WHISPER_LOCAL_MODEL=base
WHISPER_FALLBACK_ON_OPENAI_ERROR=true

# yt-dlp
YTDLP_TMP_DIR=/var/tmp/yt-transcript
YTDLP_MAX_FILESIZE_MB=500

# Optional residential proxy for captions library
YT_HTTPS_PROXY=

# LLM providers
ANTHROPIC_API_KEY=<key>
OPENAI_API_KEY=<key>
GEMINI_API_KEY=<key>
LLMAPI_API_KEY=                # leave empty until burn-in
HUGGINGFACE_TOKEN=<for pyannote>

# Feature flags
FEATURE_SENTIMENT=false        # flip to true to enable
FEATURE_DIARIZATION=true
FEATURE_MONITORS=true

# Rate limits
RATE_LIMIT_READ=60/minute
RATE_LIMIT_BATCH=10/minute
RATE_LIMIT_SUMMARIZE=30/minute
RATE_LIMIT_INTELLIGENCE=20/minute
RATE_LIMIT_WHISPER=30/hour
RATE_LIMIT_MONITOR_CREATE=10/hour

# Webhooks
WEBHOOK_MAX_ATTEMPTS=3
```

### 8.6 systemd units

One unit per process type. All `EnvironmentFile=/opt/yt-transcript/.env`.

```
yt-transcript.service                  # API (uvicorn, 2 workers)
yt-transcript-worker-default.service   # default RQ queue
yt-transcript-worker-whisper.service   # whisper queue (1 concurrency)
yt-transcript-worker-enrichment.service # chapters, diarization (1 concurrency, CPU-heavy)
yt-transcript-worker-intelligence.service # topics, sentiment, diff, summarize (2 concurrency)
yt-transcript-monitor.service          # APScheduler for RSS polling (P3)
```

Standard hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `ReadWritePaths=/opt/yt-transcript /var/tmp/yt-transcript`.

### 8.7 Reverse proxy

Match SE Job Scraper stack (Caddy preferred). `yt.ericmax.com` → `127.0.0.1:8765`. Restrict `/metrics` to loopback or VPN range via matcher.

### 8.8 DNS

`yt.ericmax.com A <VPS-IP>`. TLS auto-issued.

### 8.9 Operator CLI

```bash
# Tokens
python -m app.admin tokens create --name claude-ai --scopes read,batch,summarize,intelligence
python -m app.admin tokens revoke --id tok_xxx
python -m app.admin tokens list

# Cache
python -m app.admin cache purge --before 2026-01-01
python -m app.admin cache stats
python -m app.admin cache backfill --video-id <id> --include chapters,topics

# LLM
python -m app.admin llm test --task summarize --provider llmapi/claude-sonnet-4-6
python -m app.admin llm benchmark --task topics --videos 10
python -m app.admin llm cost --since 2026-05-01 --group-by provider

# Monitors
python -m app.admin monitors list
python -m app.admin monitors pause --id mon_xxx
python -m app.admin monitors trigger --id mon_xxx   # force poll now
```

## 9. Tests

**Unit:** parsing, auth/scopes, cache hit/miss/expired, webhook signing, LLM routing (mocked providers, fallback chain), chapter merging, deep-link computation.

**Integration (mocked external):** captions happy/fallback paths, Whisper happy/OpenAI-fail-local-succeed, batch (mixed cached/new/duplicate), webhook delivery with retries, channel ingest with N videos, RSS poller detecting new video, summarize/topics/sentiment/diff happy paths, LLM provider failover (primary fails → fallback succeeds → log shows both).

**Smoke (`scripts/smoke_test.sh`):** health, captioned video, repeat for cache-hit, no-caption video → Whisper job → poll → complete, channel ingest of 3-video test channel, summarize the result, metrics endpoint format check.

Coverage target: 85% on `app/`.

## 10. Observability

Prometheus metrics (all phases as applicable):

- `yt_requests_total{endpoint,status}`
- `yt_transcript_source_total{source}`
- `yt_job_duration_seconds{type}` (histogram, type = whisper/enrichment/intelligence)
- `yt_cache_hits_total{table}` / `yt_cache_misses_total{table}`
- `yt_llm_calls_total{task,provider,model,status}`
- `yt_llm_cost_usd_total{provider,task}`
- `yt_llm_latency_seconds{provider,model}` (histogram)
- `yt_whisper_cost_usd_total`
- `yt_active_jobs{type}`
- `yt_monitor_polls_total{monitor_id,result}` (P3)
- `yt_webhook_deliveries_total{status}`

Grafana dashboards: per-provider cost trend, fallback rate by task, cache hit rate, queue depth by type, monitor poll health.

Alerts:
- `yt_active_jobs{type="whisper"} > 10` for 5 min
- Webhook failure rate > 10% over 15 min
- `rate(yt_llm_cost_usd_total[1h]) > 1.0` (cost anomaly)
- 5xx rate > 1% over 5 min
- LLMAPI primary fallback rate > 20% (during burn-in)

## 11. Companion Claude Skill (P1 deliverable)

Same as v2 §11, with these additions for later phases:

- Recognizes channel/playlist URLs and routes to `/v1/ingest` instead of `/v1/transcript`.
- On summary requests, calls `/v1/summarize` with style inferred from user phrasing ("exec summary" → `exec_brief`, "deep dive" → `exec_deep`, etc.).
- Uses `snippets[].deep_link` when quoting timestamps.
- Reads chapters and uses them to structure section-by-section summaries.
- Surfaces topics + entities when relevant to the user's downstream task.

### 11.1 Skill structure

```
deploy/skill/youtube-transcript/
├── SKILL.md
├── scripts/
│   ├── fetch_transcript.py
│   ├── ingest_channel.py
│   └── summarize.py
└── README.md
```

### 11.2 Skill triggers

- Any YouTube URL (video, channel, playlist, Shorts, live)
- Phrases: "summarize this video", "what does X say about Y in this video", "compare these two videos", "track this channel", "exec brief of this keynote"

### 11.3 Skill acceptance criteria

- [ ] Triggers on all YouTube URL forms
- [ ] Does NOT trigger on metadata-only questions (views, channel info, upload date)
- [ ] Polls 202 responses to completion (up to 5 min default, extensible)
- [ ] Quotes use timestamped deep links from `snippets[].deep_link`
- [ ] Channel/playlist URLs route to `/v1/ingest` (P3+)
- [ ] Summary requests pick the right style from user phrasing
- [ ] Errors surface without leaking the token
- [ ] CLI scripts work standalone against the live VPS endpoint

## 12. Acceptance criteria

### Phase 1
- [ ] `/healthz` returns 200 over HTTPS
- [ ] Valid token + captioned video → 200 with `source: youtube_captions`
- [ ] Repeat request → `cache_hit: true` in <100ms
- [ ] No-caption video → 202 → poll → final transcript with `source: whisper_openai`
- [ ] `force=whisper` produces Whisper output even when captions exist
- [ ] Batch of 10 videos returns mixed cached + queued
- [ ] Webhook fires with valid HMAC
- [ ] Same video submitted twice while in-flight returns same job_id
- [ ] Token without `admin` scope cannot DELETE cache
- [ ] Rate limit returns 429 with `Retry-After`
- [ ] `/metrics` is valid Prometheus format
- [ ] systemd shows API + worker(s) `active (running)`, restarts cleanly
- [ ] Logs are JSON, no tokens, no transcripts
- [ ] Skill installed on dev machine and triggers correctly
- [ ] `scripts/smoke_test.sh` exits 0

### Phase 2
- [ ] `include=chapters` returns chapters from yt-dlp when available
- [ ] No-chapter videos get LLM-derived chapters
- [ ] `include=speakers` triggers diarization, results align with snippets
- [ ] Every snippet response includes `deep_link`
- [ ] `/v1/summarize` with each style returns coherent output
- [ ] Summary cache hit on identical params returns in <100ms

### Phase 3
- [ ] `/v1/ingest` on a channel with 5 videos enqueues all 5
- [ ] Monitor created on a test channel polls successfully
- [ ] New video on monitored channel triggers callback with full result
- [ ] `/v1/monitors` lists active monitors; DELETE removes them

### Phase 4
- [ ] `/v1/topics` returns structured topics, entities, claims, questions
- [ ] `/v1/sentiment` returns 403 when feature flag is false
- [ ] With flag on, returns timeline at requested granularity
- [ ] `/v1/diff` returns structured delta between two videos
- [ ] LLM fallback chain: primary failure → fallback succeeds → both logged
- [ ] `provider_override` works for admin-scope tokens
- [ ] `llm_call_log` table populated for every LLM call
- [ ] Cost metrics increment correctly per provider

### LLMAPI burn-in (post-P4)
- [ ] LLMAPI configured as fallback only, never primary, for 14 days
- [ ] Fallback rate < 5% per task (proves primaries are reliable)
- [ ] `python -m app.admin llm benchmark --task topics --videos 10` runs the same 10 transcripts through `gemini_direct` and `llmapi/gemini-2.5-flash`, reports cost / latency / output similarity
- [ ] If pass: flip `topics` primary to LLMAPI for 7 days
- [ ] Decision point logged with go/no-go reasoning

## 13. Backlog (not in v1, designed for)

- `/v1/usage` endpoint exposing per-token cost and call counts (Feature 9 from prior discussion)
- Full-text search across cached transcripts (`tsvector` column reserved)
- Semantic search via pgvector (`embedding` column reserved)
- Translation endpoint (`task=translate` for Whisper or LLM-based)
- Live stream transcription
- OCR of on-screen text
- Speaker name resolution (cross-video identification)
- Web UI for browsing transcripts and monitors
- MCP server wrapping all of the above for native Claude integration

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| YouTube blocks VPS IP for captions | `YT_HTTPS_PROXY` slot in env; Webshare $3/mo as documented fallback |
| OpenAI Whisper outage | Local `faster-whisper` fallback, automatic |
| All LLM providers fail simultaneously | Return 502 `llm_failed`, do not silently degrade; cached results still served |
| pyannote model download fails | Diarization disabled gracefully; transcripts still return without `speaker` field |
| Cost overrun from monitors | Per-monitor rate limit; daily cost cap env var (`MAX_DAILY_LLM_COST_USD`) with hard stop |
| Disk fill from yt-dlp audio | `max_filesize` cap; cleanup in `finally` block; nightly orphan-file sweep |
| LLMAPI Claude versions lag direct API | Burn-in plan keeps LLMAPI off primary for `summarize`/`diff` until proven |
| Sensitive content routed through LLMAPI | Document policy: medical/private content sets `provider_override=anthropic_direct` |
