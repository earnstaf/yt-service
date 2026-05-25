# Installing the youtube-transcript skill

## 1. Place the skill

Drop the entire `youtube-transcript/` folder into your Claude skills directory.

| Platform | Path |
|---|---|
| Windows | `%USERPROFILE%\.claude\skills\youtube-transcript\` |
| macOS / Linux | `~/.claude/skills/youtube-transcript/` |

After copy, the path `<skills_dir>/youtube-transcript/SKILL.md` must exist.

## 2. Set environment variables

The skill scripts read three env vars. Two are required, one optional.

| Var | Required? | Purpose |
|---|---|---|
| `YT_SERVICE_URL` | yes | Base URL of the backend (e.g. `https://yt.ericmax.com`) |
| `YT_SERVICE_TOKEN` | yes | Bearer token (mint via the admin CLI on the VPS) |
| `YT_SERVICE_ARCHIVE_DIR` | no | Local folder; when set, every transcript + summary is also written here (`.json` + `.txt` + `<style>.md`). Re-runs skip files that already exist. |
| `YT_SERVICE_POLL_TIMEOUT` | no | Seconds to wait on a 202 (Whisper) before giving up. Default `300`. |

### Windows (PowerShell, persistent)
```powershell
[Environment]::SetEnvironmentVariable("YT_SERVICE_URL", "https://yt.ericmax.com", "User")
[Environment]::SetEnvironmentVariable("YT_SERVICE_TOKEN", "<paste your token>", "User")
[Environment]::SetEnvironmentVariable("YT_SERVICE_ARCHIVE_DIR", "$env:USERPROFILE\Documents\yt-archive", "User")
```
Close and reopen Claude Code / any terminals for the new env to be visible.

### macOS / Linux (shell rc file)
```bash
cat >> ~/.bashrc <<'EOF'
export YT_SERVICE_URL=https://yt.ericmax.com
export YT_SERVICE_TOKEN=<paste your token>
export YT_SERVICE_ARCHIVE_DIR=~/yt-archive
EOF
```
(Use `~/.zshrc` if you're on zsh.)

## 3. Verify

In a new Claude Code conversation, paste:

> Summarize this video: https://youtu.be/zhWDdy_5v2w

Claude should:
1. Invoke the `youtube-transcript` skill (visible in the tool-call panel).
2. Hit your backend.
3. Return a summary with clickable deep-link timestamps.

If the skill doesn't trigger, restart Claude Code — it caches the skill list at startup.

## 4. Mint a token (one-time, server-side)

You need a token before any of this works. From the VPS:

```bash
ssh <vps-alias> "cd /opt/yt-transcript/src && sudo -u yttranscript bash -c 'set -a; source /opt/yt-transcript/.env; set +a; /opt/yt-transcript/venv/bin/python -m app.admin tokens create --name <device-name> --scopes read,batch,summarize,intelligence'"
```

Save the printed token (shown ONCE) in your password manager and paste it into `YT_SERVICE_TOKEN` above.

Use a separate token per device. Revoke any token via:
```bash
python -m app.admin tokens revoke --id <tok_id>
```

## 5. Updating later

The skill is just files. To pull updates from the upstream repo:

```bash
git clone --depth 1 https://github.com/earnstaf/yt-service.git /tmp/_yt
cp -r /tmp/_yt/deploy/skill/youtube-transcript/* ~/.claude/skills/youtube-transcript/
```

Or re-extract the zip on top of the existing directory.
