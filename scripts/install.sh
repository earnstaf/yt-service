#!/usr/bin/env bash
# =============================================================================
# install.sh — yt-transcript-service installer
#
# Responsibilities:
#   1. Validate prerequisites (python>=3.11, psql, redis-cli, caddy or nginx)
#   2. Validate /opt/yt-transcript/.env (exists, mode 0600, owned by
#      yttranscript:yttranscript)
#   3. Validate venv at /opt/yt-transcript/venv
#   4. Run `alembic upgrade head` as the service user
#   5. Copy deploy/*.service to /etc/systemd/system/
#   6. systemctl daemon-reload
#   7. systemctl enable --now for each unit
#   8. Wait up to 30s for http://127.0.0.1:8765/healthz to return 200
#
# Idempotent. Re-running on an already-installed host is safe.
#
# Run as: sudo bash scripts/install.sh
#
# Required env: none. Reads /opt/yt-transcript/.env via the systemd units.
# =============================================================================

set -euo pipefail

# ---- Constants ---------------------------------------------------------------

APP_USER="yttranscript"
APP_GROUP="yttranscript"
APP_ROOT="/opt/yt-transcript"
APP_SRC="${APP_ROOT}/src"
APP_VENV="${APP_ROOT}/venv"
APP_ENV="${APP_ROOT}/.env"
SYSTEMD_DIR="/etc/systemd/system"
HEALTH_URL="http://127.0.0.1:8765/healthz"
HEALTH_TIMEOUT_SECONDS=30

# Resolve absolute path to the repo root (parent of scripts/) so this script
# can be invoked from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEPLOY_DIR="${REPO_ROOT}/deploy"

# ---- Helpers -----------------------------------------------------------------

log() {
  printf '[install] %s\n' "$*"
}

die() {
  printf '[install][ERROR] %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "Must be run as root (use sudo)."
  fi
}

version_ge() {
  # version_ge A B  — returns 0 iff A >= B (dotted versions, up to 3 parts)
  local a="$1" b="$2"
  printf '%s\n%s\n' "$b" "$a" | sort -V -C
}

# ---- Step 1: Prerequisites ---------------------------------------------------

check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found. Install Python 3.11+."
  fi
  local ver
  ver="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  if ! version_ge "${ver}" "3.11.0"; then
    die "python3 ${ver} is too old. Need >= 3.11."
  fi
  log "python3 ${ver} OK"
}

check_postgres_client() {
  if ! command -v psql >/dev/null 2>&1; then
    die "psql not found. Install postgresql-client."
  fi
  log "psql $(psql --version | awk '{print $3}') OK"
}

check_redis_client() {
  if ! command -v redis-cli >/dev/null 2>&1; then
    die "redis-cli not found. Install redis-tools."
  fi
  log "redis-cli $(redis-cli --version | awk '{print $2}') OK"
}

check_reverse_proxy() {
  if command -v caddy >/dev/null 2>&1; then
    log "caddy $(caddy version 2>&1 | head -1) OK"
    return 0
  fi
  if command -v nginx >/dev/null 2>&1; then
    log "nginx $(nginx -v 2>&1 | awk -F/ '{print $2}') OK"
    return 0
  fi
  die "Neither caddy nor nginx found. Install one of them to terminate TLS for yt.ericmax.com."
}

check_prereqs() {
  log "Step 1/8: Validating prerequisites"
  check_python
  check_postgres_client
  check_redis_client
  check_reverse_proxy
}

# ---- Step 2: .env validation -------------------------------------------------

check_env_file() {
  log "Step 2/8: Validating ${APP_ENV}"
  if [[ ! -f "${APP_ENV}" ]]; then
    die "${APP_ENV} does not exist. Copy .env.example, fill in values, then re-run."
  fi

  # POSIX stat is not portable; GNU coreutils has -c.
  local mode owner group
  mode="$(stat -c '%a' "${APP_ENV}")"
  owner="$(stat -c '%U' "${APP_ENV}")"
  group="$(stat -c '%G' "${APP_ENV}")"

  if [[ "${mode}" != "600" ]]; then
    die "${APP_ENV} has mode ${mode}, expected 600. Run: chmod 600 ${APP_ENV}"
  fi
  if [[ "${owner}" != "${APP_USER}" || "${group}" != "${APP_GROUP}" ]]; then
    die "${APP_ENV} owned by ${owner}:${group}, expected ${APP_USER}:${APP_GROUP}. Run: chown ${APP_USER}:${APP_GROUP} ${APP_ENV}"
  fi
  log ".env mode/ownership OK"
}

# ---- Step 3: venv validation -------------------------------------------------

check_venv() {
  log "Step 3/8: Validating venv at ${APP_VENV}"
  if [[ ! -x "${APP_VENV}/bin/python" ]]; then
    die "venv missing or broken: ${APP_VENV}/bin/python not executable. Create per docs/specs §8.4."
  fi
  if [[ ! -x "${APP_VENV}/bin/alembic" ]]; then
    die "alembic not installed in venv. Run: ${APP_VENV}/bin/pip install -r ${APP_SRC}/requirements.txt"
  fi
  log "venv OK"
}

# ---- Step 4: alembic upgrade head --------------------------------------------

run_migrations() {
  log "Step 4/8: Running alembic upgrade head as ${APP_USER}"
  if [[ ! -d "${APP_SRC}" ]]; then
    die "${APP_SRC} not found. Clone the repo there first."
  fi
  # Editable install of the application itself so `from app import ...`
  # resolves from the venv. Required because pyproject.toml uses an
  # `src/`-layout package which `pip install -r requirements.txt` does not
  # register. Idempotent: pip detects the existing install and short-circuits.
  if ! sudo -u "${APP_USER}" bash -c "'${APP_VENV}/bin/pip' install -e '${APP_SRC}'"; then
    die "pip install -e failed. Inspect the output above."
  fi
  # Run as the service user. cd into the src tree so alembic.ini is found.
  if ! sudo -u "${APP_USER}" bash -c "cd '${APP_SRC}' && '${APP_VENV}/bin/alembic' upgrade head"; then
    die "alembic upgrade head failed. Inspect the output above."
  fi
  log "alembic OK"
}

# ---- Step 5/6/7: systemd units -----------------------------------------------

install_units() {
  log "Step 5/8: Copying systemd units from ${DEPLOY_DIR}"
  if [[ ! -d "${DEPLOY_DIR}" ]]; then
    die "${DEPLOY_DIR} not found. Cannot install systemd units."
  fi
  shopt -s nullglob
  local units=( "${DEPLOY_DIR}"/*.service )
  shopt -u nullglob
  if [[ "${#units[@]}" -eq 0 ]]; then
    die "No *.service files found in ${DEPLOY_DIR}."
  fi
  for unit in "${units[@]}"; do
    local base
    base="$(basename "${unit}")"
    # cp -p preserves perms; install ensures 0644
    install -m 0644 "${unit}" "${SYSTEMD_DIR}/${base}"
    log "  installed ${base}"
  done

  log "Step 6/8: systemctl daemon-reload"
  systemctl daemon-reload

  log "Step 7/8: Enabling and starting units"
  for unit in "${units[@]}"; do
    local base
    base="$(basename "${unit}")"
    # enable --now is idempotent: if already running, restart picks up new unit content.
    if systemctl is-enabled --quiet "${base}" && systemctl is-active --quiet "${base}"; then
      log "  ${base} already enabled+active; restarting to pick up any unit changes"
      systemctl restart "${base}"
    else
      systemctl enable --now "${base}"
    fi
    log "  ${base} active"
  done
}

# ---- Step 8: health probe ----------------------------------------------------

wait_for_health() {
  log "Step 8/8: Waiting up to ${HEALTH_TIMEOUT_SECONDS}s for ${HEALTH_URL}"
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
  while (( $(date +%s) < deadline )); do
    if curl -fsS -o /dev/null -m 2 "${HEALTH_URL}" 2>/dev/null; then
      log "service healthy"
      return 0
    fi
    sleep 1
  done
  printf '[install][ERROR] service did not become healthy in %ss\n' "${HEALTH_TIMEOUT_SECONDS}" >&2
  journalctl -u yt-transcript -n 50 --no-pager || true
  return 1
}

# ---- Main --------------------------------------------------------------------

main() {
  require_root
  check_prereqs
  check_env_file
  check_venv
  run_migrations
  install_units
  if wait_for_health; then
    log "install complete"
    exit 0
  else
    exit 1
  fi
}

main "$@"
