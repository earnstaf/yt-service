#!/usr/bin/env bash
# =============================================================================
# smoke_test.sh — end-to-end smoke test against a live yt-transcript-service.
#
# Required env:
#   YT_SERVICE_TOKEN   API token with at least `read` scope.
#
# Optional env:
#   YT_SERVICE_URL     Base URL (default: https://yt.ericmax.com)
#
# Exit code:
#   0 if all tests pass
#   non-zero (count of failures) otherwise
#
# Tests (8 total):
#   1. GET /healthz                                  → 200 {"status":"ok"}
#   2. GET /readyz                                   → 200
#   3. GET /metrics  from public origin              → 403 (loopback-only)
#   4. GET /v1/transcript?v=<captioned>              → 200 source=youtube_captions
#   5. Repeat #4                                     → 200 cache_hit=true, <200ms
#   6. GET /v1/transcript?v=<no_caption>             → 202 with job_id, poll_url
#   7. Poll /v1/jobs/<id> up to 180s                 → status=complete
#   8. GET /v1/cache/stats                           → 200 total_rows >= 2
# =============================================================================

set -euo pipefail

# ---- Constants ---------------------------------------------------------------

# Stable captioned video. TED-style talks rarely change their caption status.
# NOTE: Operator should replace these with videos they control once available,
#       to remove the implicit dependency on third-party content.
CAPTIONED_VIDEO_ID="zhWDdy_5v2w"

# Short with no captions. YouTube Shorts often lack captions; pick one that
# is unlikely to disappear.
NO_CAPTION_VIDEO_ID="9bZkp7q19f0"

YT_SERVICE_URL="${YT_SERVICE_URL:-https://yt.ericmax.com}"
YT_SERVICE_URL="${YT_SERVICE_URL%/}"   # strip trailing slash

JOB_POLL_TIMEOUT_SECONDS=180
JOB_POLL_INTERVAL_SECONDS=3
CACHE_HIT_MAX_MS=200

# ---- Helpers -----------------------------------------------------------------

PASS_COUNT=0
FAIL_COUNT=0
TOTAL_TESTS=8

pass() {
  PASS_COUNT=$(( PASS_COUNT + 1 ))
  printf '[%d/%d] PASS  %s\n' "$(( PASS_COUNT + FAIL_COUNT ))" "${TOTAL_TESTS}" "$1"
}

fail() {
  FAIL_COUNT=$(( FAIL_COUNT + 1 ))
  printf '[%d/%d] FAIL  %s\n' "$(( PASS_COUNT + FAIL_COUNT ))" "${TOTAL_TESTS}" "$1" >&2
  if [[ -n "${2:-}" ]]; then
    printf '       %s\n' "$2" >&2
  fi
}

die() {
  printf '[smoke][FATAL] %s\n' "$*" >&2
  exit 2
}

require_env() {
  if [[ -z "${YT_SERVICE_TOKEN:-}" ]]; then
    die "YT_SERVICE_TOKEN is required. Export it before running."
  fi
}

# Portable JSON field extraction via python stdlib.
json_get() {
  # json_get FIELD_PATH < json_string_on_stdin
  # FIELD_PATH dot-notation: e.g. 'source', 'job.id', 'data.0.name'
  python3 - "$1" <<'PY'
import json, sys
path = sys.argv[1].split(".")
try:
    data = json.load(sys.stdin)
except Exception as e:
    sys.stderr.write(f"json parse error: {e}\n")
    sys.exit(1)
cur = data
for part in path:
    if part.isdigit() and isinstance(cur, list):
        cur = cur[int(part)]
    elif isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
        break
if cur is None:
    sys.exit(2)
if isinstance(cur, (dict, list)):
    print(json.dumps(cur))
else:
    print(cur)
PY
}

# Issue GET, write body to $2, return the integer HTTP status on stdout.
# Sends Authorization unless $3 == "noauth".
http_get() {
  local url="$1"
  local body_file="$2"
  local mode="${3:-auth}"
  local args=( -sS -o "${body_file}" -w '%{http_code}' -m 30 )
  if [[ "${mode}" != "noauth" ]]; then
    args+=( -H "Authorization: Bearer ${YT_SERVICE_TOKEN}" )
  fi
  curl "${args[@]}" "${url}" || printf '000'
}

# Same as http_get but also prints elapsed time in seconds.
http_get_timed() {
  local url="$1"
  local body_file="$2"
  curl -sS -o "${body_file}" \
       -w '%{http_code} %{time_total}' \
       -m 30 \
       -H "Authorization: Bearer ${YT_SERVICE_TOKEN}" \
       "${url}" || printf '000 0'
}

# ---- Test 1: /healthz --------------------------------------------------------

test_healthz() {
  local body code
  body="$(mktemp)"
  code="$(http_get "${YT_SERVICE_URL}/healthz" "${body}" noauth)"
  if [[ "${code}" == "200" ]]; then
    local status
    status="$(json_get status < "${body}" || true)"
    if [[ "${status}" == "ok" ]]; then
      pass "/healthz returns 200 ok"
    else
      fail "/healthz body missing status=ok" "got: $(cat "${body}")"
    fi
  else
    fail "/healthz expected 200 got ${code}" "$(cat "${body}")"
  fi
  rm -f "${body}"
}

# ---- Test 2: /readyz ---------------------------------------------------------

test_readyz() {
  local body code
  body="$(mktemp)"
  code="$(http_get "${YT_SERVICE_URL}/readyz" "${body}" noauth)"
  if [[ "${code}" == "200" ]]; then
    pass "/readyz returns 200"
  else
    fail "/readyz expected 200 got ${code}" "$(cat "${body}")"
  fi
  rm -f "${body}"
}

# ---- Test 3: /metrics is loopback-only ---------------------------------------

test_metrics_forbidden() {
  local body code
  body="$(mktemp)"
  code="$(http_get "${YT_SERVICE_URL}/metrics" "${body}" noauth)"
  if [[ "${code}" == "403" ]]; then
    pass "/metrics returns 403 from public origin"
  else
    fail "/metrics expected 403 got ${code}" "$(cat "${body}")"
  fi
  rm -f "${body}"
}

# ---- Test 4: captioned video, cold ------------------------------------------

test_transcript_cold() {
  local body code
  body="$(mktemp)"
  code="$(http_get "${YT_SERVICE_URL}/v1/transcript?v=${CAPTIONED_VIDEO_ID}" "${body}")"
  if [[ "${code}" != "200" ]]; then
    fail "/v1/transcript (captioned) expected 200 got ${code}" "$(cat "${body}")"
    rm -f "${body}"
    return
  fi
  local src
  src="$(json_get source < "${body}" || true)"
  if [[ "${src}" == "youtube_captions" ]]; then
    pass "/v1/transcript (captioned) returns source=youtube_captions"
  else
    fail "/v1/transcript (captioned) wrong source" "source=${src}"
  fi
  rm -f "${body}"
}

# ---- Test 5: captioned video, warm (cache hit) ------------------------------

test_transcript_warm() {
  local body line code time_s time_ms
  body="$(mktemp)"
  line="$(http_get_timed "${YT_SERVICE_URL}/v1/transcript?v=${CAPTIONED_VIDEO_ID}" "${body}")"
  code="${line%% *}"
  time_s="${line##* }"
  # Convert float seconds to integer ms via python (portable, no bc dep).
  time_ms="$(python3 -c "print(int(float('${time_s}') * 1000))")"

  if [[ "${code}" != "200" ]]; then
    fail "/v1/transcript (warm) expected 200 got ${code}" "$(cat "${body}")"
    rm -f "${body}"
    return
  fi

  local cache_hit
  cache_hit="$(json_get cache_hit < "${body}" || true)"
  if [[ "${cache_hit}" != "True" && "${cache_hit}" != "true" ]]; then
    fail "/v1/transcript (warm) cache_hit not true" "cache_hit=${cache_hit}"
    rm -f "${body}"
    return
  fi

  if (( time_ms < CACHE_HIT_MAX_MS )); then
    pass "/v1/transcript (warm) cache_hit=true in ${time_ms}ms"
  else
    fail "/v1/transcript (warm) cache hit too slow" "${time_ms}ms >= ${CACHE_HIT_MAX_MS}ms"
  fi
  rm -f "${body}"
}

# ---- Test 6: no-caption video → job submission ------------------------------

JOB_ID=""

test_no_caption_submit() {
  local body code
  body="$(mktemp)"
  code="$(http_get "${YT_SERVICE_URL}/v1/transcript?v=${NO_CAPTION_VIDEO_ID}" "${body}")"
  if [[ "${code}" != "202" ]]; then
    fail "/v1/transcript (no-caption) expected 202 got ${code}" "$(cat "${body}")"
    rm -f "${body}"
    return
  fi
  JOB_ID="$(json_get job_id < "${body}" || true)"
  local poll_url
  poll_url="$(json_get poll_url < "${body}" || true)"
  if [[ -z "${JOB_ID}" || "${JOB_ID}" == "None" ]]; then
    fail "/v1/transcript (no-caption) missing job_id" "$(cat "${body}")"
  elif [[ -z "${poll_url}" || "${poll_url}" == "None" ]]; then
    fail "/v1/transcript (no-caption) missing poll_url" "$(cat "${body}")"
  else
    pass "/v1/transcript (no-caption) returns 202 job_id=${JOB_ID}"
  fi
  rm -f "${body}"
}

# ---- Test 7: poll job until complete ----------------------------------------

test_job_complete() {
  if [[ -z "${JOB_ID}" ]]; then
    fail "job poll skipped (no job_id from prior test)" ""
    return
  fi
  local deadline=$(( $(date +%s) + JOB_POLL_TIMEOUT_SECONDS ))
  local body status code
  body="$(mktemp)"
  while (( $(date +%s) < deadline )); do
    code="$(http_get "${YT_SERVICE_URL}/v1/jobs/${JOB_ID}" "${body}")"
    if [[ "${code}" == "200" ]]; then
      status="$(json_get status < "${body}" || true)"
      case "${status}" in
        complete)
          pass "job ${JOB_ID} reached status=complete"
          rm -f "${body}"
          return
          ;;
        failed)
          fail "job ${JOB_ID} reported status=failed" "$(cat "${body}")"
          rm -f "${body}"
          return
          ;;
      esac
    fi
    sleep "${JOB_POLL_INTERVAL_SECONDS}"
  done
  fail "job ${JOB_ID} did not complete within ${JOB_POLL_TIMEOUT_SECONDS}s" "last status=${status:-unknown}"
  rm -f "${body}"
}

# ---- Test 8: /v1/cache/stats -------------------------------------------------

test_cache_stats() {
  local body code rows
  body="$(mktemp)"
  code="$(http_get "${YT_SERVICE_URL}/v1/cache/stats" "${body}")"
  if [[ "${code}" != "200" ]]; then
    fail "/v1/cache/stats expected 200 got ${code}" "$(cat "${body}")"
    rm -f "${body}"
    return
  fi
  rows="$(json_get total_rows < "${body}" || true)"
  if [[ -z "${rows}" || "${rows}" == "None" ]]; then
    fail "/v1/cache/stats missing total_rows" "$(cat "${body}")"
  elif (( rows >= 2 )); then
    pass "/v1/cache/stats returns total_rows=${rows}"
  else
    fail "/v1/cache/stats total_rows too low" "total_rows=${rows} (expected >= 2)"
  fi
  rm -f "${body}"
}

# ---- Main --------------------------------------------------------------------

main() {
  require_env
  printf 'Smoke testing %s\n' "${YT_SERVICE_URL}"
  printf '\n'

  test_healthz
  test_readyz
  test_metrics_forbidden
  test_transcript_cold
  test_transcript_warm
  test_no_caption_submit
  test_job_complete
  test_cache_stats

  printf '\n'
  if (( FAIL_COUNT == 0 )); then
    printf 'PASSED %d/%d\n' "${PASS_COUNT}" "${TOTAL_TESTS}"
    exit 0
  else
    printf 'FAILED %d tests (PASSED %d/%d)\n' "${FAIL_COUNT}" "${PASS_COUNT}" "${TOTAL_TESTS}" >&2
    exit "${FAIL_COUNT}"
  fi
}

main "$@"
