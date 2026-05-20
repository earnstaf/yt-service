# yt-transcript-service

Production-grade transcript and intelligence API for YouTube content. Captions when available, Whisper otherwise. Layered with chaptering, diarization, topics, summarization, diff, sentiment, channel/playlist ingestion, scheduled monitoring.

## Status

Built in phases. See `docs/specs/yt-transcript-service-spec.md` for the canonical design.

| Phase | Scope | Status |
|---|---|---|
| P1 | API, auth, captions, Whisper, cache, jobs, batch, webhooks, skill | in-progress |
| P2 | Chapters, diarization, deep links, on-demand summaries | pending |
| P3 | Channel/playlist ingestion, RSS monitor poller | pending |
| P4 | Topics, sentiment, diff, multi-provider LLM routing | pending |

## Local development

```bash
python -m venv .venv
. .venv/bin/activate                     # bash
# or:  .venv\Scripts\activate            # cmd

pip install -r requirements.txt
cp .env.example .env                     # then fill in keys
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8765
```

In a second terminal, run a worker pool:

```bash
rq worker -u $REDIS_URL default whisper enrichment intelligence
```

## Production deploy

Target: `yt.ericmax.com` on the SE Job Scraper VPS. See `deploy/` for systemd units and reverse proxy snippets.

```bash
sudo bash scripts/install.sh
```

## Documentation

- `docs/specs/yt-transcript-service-spec.md` — spec v3 (canonical)
- `docs/superpowers/specs/2026-05-20-yt-service-design.md` — build delta on the spec
- `docs/plans/` — per-phase implementation plans
- `docs/JUDGEMENT_CALLS.md` — autonomous-mode decisions log
