# Phase 3 — Implementation Plan

**Goal:** Channel and playlist ingestion plus scheduled monitoring with callbacks.

**Acceptance criteria:** spec §12 Phase 3 bullets.

**Out of scope:** topics/sentiment/diff (P4).

## Task graph

### Group A — Parsing + yt-dlp expansion

**A1 — Channel/playlist parsing**

`src/app/parsing.py`:
- Replace the P1 `parse_channel_or_playlist` stub. Recognize:
  - `https://www.youtube.com/@<handle>` → `("channel_handle", "@handle")`
  - `https://www.youtube.com/channel/UC...` → `("channel_id", "UC...")`
  - `https://www.youtube.com/playlist?list=PL...` → `("playlist", "PL...")`
  - `https://www.youtube.com/c/<name>` → `("channel_handle", name)`
- Return `ChannelRef(kind: Literal["channel_handle","channel_id","playlist"], value: str)` dataclass in `domain.py`.
- Raise `InvalidChannelError` on anything else.

Tests `tests/unit/test_parsing.py` extension.

**A2 — yt-dlp playlist expansion**

`src/app/youtube.py`:
- `async def expand_channel_or_playlist(ref: ChannelRef, max_videos: int, since: date | None) -> list[VideoSummary]` where `VideoSummary` is `{video_id, title, upload_date}`.
- Uses `yt_dlp.YoutubeDL({"extract_flat": True, "playlist_items": f"1-{max_videos}", ...}).extract_info(url)`. URL built from the ref.
- For channels, hit the channel's videos tab.
- Filters by `since` date Python-side (yt-dlp's `dateafter` isn't reliable with flat extraction).
- `asyncio.to_thread`. Returns empty list on failure (log warning).

Tests with mocked yt-dlp.

### Group B — Ingest endpoint

**B1 — `POST /v1/ingest`**

`src/app/ingest.py`:
- `async def ingest(session, redis, body: IngestRequest, token_id) -> IngestResponse`.
- Parse URL → ChannelRef. Expand → list of video IDs.
- For each video: call `transcript_service.get_or_fetch` with a minimal request (no wait). Collect 200 (immediate) or 202 (queued) outcomes.
- Persist an `Ingest` record (new table) tracking the run for audit. Or skip the table and just return synchronously — simpler.
- Return `IngestResponse(ingest_id, video_count, videos: list[{video_id, status: "cached|queued|failed"}], callback_url)`.

Schema additions in `schemas.py`: `IngestRequest` (url, max_videos=100, since: date | None, include[], callback_url), `IngestResponse`.

`src/app/main.py`:
- `POST /v1/ingest` with `require_scopes("batch")` (reuses batch rate-limit bucket — ingest is heavy).

Tests cover URL parsing dispatch, partial failure handling, the `since` filter.

### Group C — Monitors

**C1 — Monitor CRUD endpoints**

`src/app/monitors.py`:
- Postgres-backed CRUD over the existing `monitors` table.
- `async def create_monitor(session, body) -> Monitor`, `list_monitors`, `delete_monitor`.

`src/app/main.py`:
- `POST /v1/monitors` (scope=monitor) + rate limit `monitor_create`
- `GET /v1/monitors` (scope=read)
- `DELETE /v1/monitors/{id}` (scope=monitor)

Schemas: `MonitorCreateRequest`, `MonitorResponse`.

**C2 — RSS poller (APScheduler)**

`src/app/monitor_scheduler.py`:
- New process entrypoint `python -m app.monitor_scheduler` invoked by `deploy/yt-transcript-monitor.service`.
- APScheduler with `BlockingScheduler`. On startup: load all unpaused monitors from DB and schedule each at its interval.
- Poll function: hit `https://www.youtube.com/feeds/videos.xml?channel_id=<id>`. Parse with feedparser. Compare against `monitors.last_video_id`. For new entries:
  - Call `transcript_service.get_or_fetch` via the API (HTTP loopback) so we share the same lock + rate-limit infrastructure. Or directly via Python — but that requires a DB session inside the scheduler. Direct call is simpler.
  - Update `monitors.last_video_id` + `last_polled_at`.
  - Fire monitor's `callback_url` with the result (HMAC-signed, same webhooks module).
- Reload monitor list every 5 min so newly-created monitors get scheduled without a restart.
- Failure modes: RSS empty → log + skip; channel deleted → pause monitor.

systemd unit `deploy/yt-transcript-monitor.service` (the spec's §8.6 file).

Tests focus on the poll loop with mocked feedparser + transcript_service.

### Group D — Skill update

**D1 — `deploy/skill/youtube-transcript/scripts/ingest_channel.py`**

Replace stub with real implementation. POSTs to `/v1/ingest`. Same env-var contract as `fetch_transcript.py`.

SKILL.md updated to document channel/playlist URL routing.

## Pinned details

- P3-1: Ingest is synchronous-per-video (enqueues Whisper jobs as needed). Don't try to do a single bulk Whisper batch.
- P3-2: Monitor scheduler runs in its own process (separate systemd unit). Crash-recovery: APScheduler reads jobs from DB on startup.
- P3-3: RSS polls have a 30s timeout. On HTTP failure, log + retry next interval (no immediate retry).
- P3-4: Monitor callbacks reuse `webhooks.enqueue_webhook` + per-token webhook secret. The monitor table needs a `webhook_secret` column OR uses the creator-token's secret. Use the creator-token's secret; add `created_by` token_id lookup at fire-time.
- P3-5: `IngestResponse.videos[].status` is "cached" (cache hit), "queued" (whisper started), "skipped" (already in flight), or "failed" (parse error).

## DoD

- 304+ unit tests still green; new P3 module tests added.
- Codex review clean.
- Commit lists §12 P3 bullets covered.
