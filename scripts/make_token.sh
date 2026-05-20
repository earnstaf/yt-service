#!/usr/bin/env bash
# =============================================================================
# make_token.sh — thin wrapper around `python -m app.admin tokens create`
#
# Usage:
#   sudo -u yttranscript bash scripts/make_token.sh \
#     --name claude-ai \
#     --scopes read,batch,summarize,intelligence
#
# Optional:
#   --venv /path/to/venv    Override venv (default: /opt/yt-transcript/venv)
#
# All other args pass through verbatim to `python -m app.admin tokens create`.
#
# Notes:
#   - Plaintext token is printed ONCE by app.admin. Capture it immediately;
#     it cannot be recovered from the database.
#   - Run as the service user (yttranscript) so the venv and DB perms align.
#
# Required env: none. .env at /opt/yt-transcript/.env is loaded by app.config.
# =============================================================================

set -euo pipefail

DEFAULT_VENV="/opt/yt-transcript/venv"
APP_SRC_DEFAULT="/opt/yt-transcript/src"

VENV="${DEFAULT_VENV}"
APP_SRC="${APP_SRC_DEFAULT}"

log() {
  printf '[make_token] %s\n' "$*"
}

die() {
  printf '[make_token][ERROR] %s\n' "$*" >&2
  exit 1
}

# Parse --venv (and pop it from $@); leave all other flags for app.admin.
PASSTHROUGH_ARGS=()
while (( $# > 0 )); do
  case "$1" in
    --venv)
      [[ -n "${2:-}" ]] || die "--venv requires a path argument"
      VENV="$2"
      shift 2
      ;;
    --venv=*)
      VENV="${1#--venv=}"
      shift
      ;;
    --src)
      [[ -n "${2:-}" ]] || die "--src requires a path argument"
      APP_SRC="$2"
      shift 2
      ;;
    --src=*)
      APP_SRC="${1#--src=}"
      shift
      ;;
    *)
      PASSTHROUGH_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ! -x "${VENV}/bin/python" ]]; then
  die "venv python not found at ${VENV}/bin/python. Pass --venv /path/to/venv to override."
fi

if [[ ! -d "${APP_SRC}" ]]; then
  die "app source dir not found at ${APP_SRC}. Pass --src /path/to/src to override."
fi

log "About to mint a token. The plaintext will print ONCE — store it immediately."

# Run from src so `python -m app.admin` resolves the package. Activate venv by
# invoking its python directly; no need to source bin/activate.
cd "${APP_SRC}"
exec "${VENV}/bin/python" -m app.admin tokens create "${PASSTHROUGH_ARGS[@]}"
