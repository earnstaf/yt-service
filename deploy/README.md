# yt-transcript-service — Deploy Playbook

Operator playbook for deploying yt-transcript-service to the SE Job Scraper
VPS (or any equivalent Linux host). Every out-of-band step is documented
here; nothing assumed.

Target host conventions:
- Code lives at `/opt/yt-transcript/src`
- Virtualenv at `/opt/yt-transcript/venv`
- Environment file at `/opt/yt-transcript/.env` (mode 0600, owned by service user)
- Working temp at `/var/tmp/yt-transcript`
- Service user / group: `yttranscript`
- API listens on `127.0.0.1:8765`; reverse proxy terminates TLS at `yt.ericmax.com`

---

## 1. Prerequisites on the VPS

- Python 3.11+
- Postgres 14+ with `postgresql-contrib` (provides the `vector` extension via pgvector)
- Redis 6+
- Caddy (preferred) or nginx with certbot
- ffmpeg (required by Whisper audio chunking)

Verify:
```bash
python3 --version
psql --version
redis-cli --version
caddy version   # or: nginx -v
ffmpeg -version
```

If pgvector is not yet installed on the host:
```bash
sudo apt-get install -y postgresql-14-pgvector  # adjust version as needed
```

---

## 2. One-time database setup (as `postgres` superuser)

`CREATE EXTENSION` requires superuser privileges. The application's service
user (`yttranscript`) does not have that, so the extension is installed
here, not inside alembic. See `docs/JUDGEMENT_CALLS.md` JC-015.

```bash
sudo -u postgres psql <<'SQL'
CREATE DATABASE yt_transcript;
CREATE USER yttranscript WITH ENCRYPTED PASSWORD '<generate-a-strong-password>';
GRANT ALL PRIVILEGES ON DATABASE yt_transcript TO yttranscript;
\c yt_transcript
GRANT ALL ON SCHEMA public TO yttranscript;
CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector; required by JC-015
SQL
```

Record the password you generated. It goes into `DATABASE_URL` in step 5.

---

## 3. System user and directories

```bash
sudo useradd --system --home /opt/yt-transcript --shell /usr/sbin/nologin yttranscript
sudo mkdir -p /opt/yt-transcript /var/tmp/yt-transcript
sudo chown -R yttranscript:yttranscript /opt/yt-transcript /var/tmp/yt-transcript
```

---

## 4. Code and virtualenv

```bash
sudo -u yttranscript git clone <REPO_URL> /opt/yt-transcript/src
sudo -u yttranscript python3 -m venv /opt/yt-transcript/venv
sudo -u yttranscript /opt/yt-transcript/venv/bin/pip install -r /opt/yt-transcript/src/requirements.txt
sudo -u yttranscript /opt/yt-transcript/venv/bin/pip install -e /opt/yt-transcript/src
```

The editable install of the repo itself is what makes `from app import ...`
importable from the venv without setting `PYTHONPATH`. The `src/` layout
declared in `pyproject.toml` makes this the only correct way to install the
package — bare `pip install -r requirements.txt` does not register `app`.

---

## 5. Environment file

```bash
sudo -u yttranscript cp /opt/yt-transcript/src/.env.example /opt/yt-transcript/.env
sudo chmod 600 /opt/yt-transcript/.env
sudo chown yttranscript:yttranscript /opt/yt-transcript/.env
```

Now edit `/opt/yt-transcript/.env` and fill in real values:

- `DATABASE_URL` with the password set in step 2.
- `REDIS_URL` — database index `3` to coexist with SE Job Scraper on the
  shared Redis instance.
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `LLMAPI_API_KEY`
  — provider keys.
- `HUGGINGFACE_TOKEN` — P2 diarization prerequisite. Leave empty until then.
- `YT_BOOTSTRAP_ADMIN_TOKEN` — set to a strong random value. On first
  startup the app will mint an admin token row using this plaintext, then
  the value should be rotated out (see step 9).

---

## 6. Reverse proxy

### Caddy (preferred)

Append the contents of `deploy/Caddyfile.snippet` to `/etc/caddy/Caddyfile`
(the same file that already serves SE Job Scraper). Then:

```bash
sudo systemctl reload caddy
```

Caddy will auto-issue TLS for `yt.ericmax.com` once the DNS record (step 7)
resolves to this host.

### nginx (fallback)

```bash
sudo cp /opt/yt-transcript/src/deploy/nginx.conf.snippet /etc/nginx/sites-available/yt-transcript.conf
sudo ln -s /etc/nginx/sites-available/yt-transcript.conf /etc/nginx/sites-enabled/
sudo certbot --nginx -d yt.ericmax.com
sudo systemctl reload nginx
```

---

## 7. DNS

A record:
```
yt.ericmax.com → <VPS public IP>     TTL 300s
```

Confirm with `dig +short yt.ericmax.com` before reloading the proxy.

---

## 8. Install systemd units and start services

```bash
sudo bash /opt/yt-transcript/src/scripts/install.sh
```

This will:
- Run `alembic upgrade head` to bring the schema current.
- Copy the five unit files from `deploy/` to `/etc/systemd/system/`.
- `systemctl daemon-reload`, then `enable --now` each unit:
  - `yt-transcript.service`
  - `yt-transcript-worker-default.service`
  - `yt-transcript-worker-whisper.service`
  - `yt-transcript-worker-enrichment.service` (queue empty in P1)
  - `yt-transcript-worker-intelligence.service` (queue empty in P1)
- Poll `http://127.0.0.1:8765/healthz` (local loopback, not the public hostname)
  until it returns 200, or fail loudly. The public hostname is exercised
  separately in step 10.

---

## 9. First token rotation

The `YT_BOOTSTRAP_ADMIN_TOKEN` env value minted a hashed admin token row on
first start. Rotate it immediately by issuing replacement tokens and
revoking the bootstrap one:

```bash
sudo -u yttranscript bash /opt/yt-transcript/src/scripts/make_token.sh --name claude-ai --scopes read,batch,summarize,intelligence
sudo -u yttranscript bash /opt/yt-transcript/src/scripts/make_token.sh --name eric-admin --scopes admin
# then revoke the bootstrap token:
sudo -u yttranscript /opt/yt-transcript/venv/bin/python -m app.admin tokens revoke --id <bootstrap-id>
```

Store the new tokens in your password manager. Plaintext is shown once.

---

## 10. Smoke test

```bash
export YT_SERVICE_URL=https://yt.ericmax.com
export YT_SERVICE_TOKEN=<claude-ai token from step 9>
bash /opt/yt-transcript/src/scripts/smoke_test.sh
```

Expected: all probes (healthz, readyz, a canonical transcript request,
metrics from loopback) return 200 / 2xx with no errors in journalctl.

---

## 11. HuggingFace pyannote (P2 prerequisite — not required for P1)

Diarization in P2 depends on two gated HuggingFace models. Doing this in
advance avoids a deploy stall when P2 ships.

1. Create a HuggingFace account.
2. Visit `https://huggingface.co/pyannote/segmentation-3.0` and
   `https://huggingface.co/pyannote/speaker-diarization-3.1` and accept the
   gated terms on both pages (one click per page, signed in).
3. Generate a read token at `https://huggingface.co/settings/tokens`.
4. Set `HUGGINGFACE_TOKEN=<token>` in `/opt/yt-transcript/.env`.
5. Restart the enrichment worker so the new env is picked up:
   ```bash
   sudo systemctl restart yt-transcript-worker-enrichment.service
   ```

---

## 12. Logs

```bash
sudo journalctl -u yt-transcript -f
sudo journalctl -u yt-transcript-worker-whisper -f
sudo journalctl -u yt-transcript-worker-default -f
sudo journalctl -u yt-transcript-worker-enrichment -f
sudo journalctl -u yt-transcript-worker-intelligence -f
```

Add `--since "10 minutes ago"` to inspect a recent window without
tailing live.

---

## 13. Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| 502 `youtube_ip_blocked` | YouTube blocking VPS IP for caption / yt-dlp requests | Set `YT_HTTPS_PROXY=http://user:pass@proxy.zyte.com:8011` (Zyte Smart Proxy Manager) in `.env`, then `sudo systemctl restart yt-transcript.service`. |
| 502 `whisper_failed` | Disk pressure or missing ffmpeg | Check disk space in `/var/tmp/yt-transcript`; confirm `ffmpeg -version` succeeds; check `journalctl -u yt-transcript-worker-whisper` for the stack. |
| 503 `queue_full` | Whisper saturation | Inspect `yt_active_jobs{type="whisper"}` metric. To scale: add a second `yt-transcript-worker-whisper-2.service` unit pointing at the same queue. |
| Daily cost cap hit | Cost guard tripped | Inspect `yt_llm_cost_usd_total`; raise `MAX_DAILY_LLM_COST_USD` in `.env`; restart the API. |
| `/metrics` returns 403 from the public hostname | Working as designed | Hit `http://127.0.0.1:8765/metrics` from the VPS instead. The proxy and the app both restrict this endpoint to loopback. |
| `alembic upgrade head` fails on `CREATE EXTENSION` | Service user is not a Postgres superuser | Re-run the step 2 SQL block as the `postgres` superuser. |

---

## File inventory

This directory ships:

- `yt-transcript.service` — API (uvicorn, 2 workers)
- `yt-transcript-worker-default.service` — RQ worker for the `default` queue
- `yt-transcript-worker-whisper.service` — RQ worker for the `whisper` queue
- `yt-transcript-worker-enrichment.service` — RQ worker for `enrichment` (P2+)
- `yt-transcript-worker-intelligence.service` — RQ worker for `intelligence` (P4+)
- `Caddyfile.snippet` — site stanza for Caddy
- `nginx.conf.snippet` — equivalent nginx server block (fallback)
- `README.md` — this file
