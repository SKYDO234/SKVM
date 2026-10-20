#!/usr/bin/env bash
# End-to-end smoke test: let autosetup.sh bring the panel up, then prove the
# real routes answer - HTML pages, the CSRF-protected login flow, the JSON API
# and the static assets the frontend needs. Used by CI and by anyone who wants
# a quick "is it actually working" check.
#
#   bash .scripts/smoke.sh              # start, test, leave it running
#   CI=1 bash .scripts/smoke.sh         # start, test, stop again
#   PORT=9000 bash .scripts/smoke.sh
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="${REPO_DIR:-$(cd -- "$SCRIPT_DIR/.." && pwd -P)}"
cd "$REPO_DIR" || exit 2

PORT="${1:-${PORT:-${SKVM_PORT:-8080}}}"
HOST=127.0.0.1
BASE="http://${HOST}:${PORT}"
STARTED_BY_US=0
FAILURES=0
CHECKS=0
BODY="$(mktemp)"
JAR="$(mktemp)"
trap 'rm -f "$BODY" "$JAR"' EXIT

# Set SMOKE_PROTO=https when the panel enforces TLS (SKVM_FORCE_HTTPS) and you
# are testing it over plain http on localhost, e.g. behind an https-only proxy.
PROTO_HEADER=()
if [ -n "${SMOKE_PROTO:-}" ]; then PROTO_HEADER=(-H "X-Forwarded-Proto: ${SMOKE_PROTO}"); fi

say() { printf '%s\n' "$*"; }

# On GitHub Actions every failure also becomes an annotation: the log itself is
# not readable through the API, the annotations are. Keep each one on a line.
emit() {
  printf '  !! %s\n' "$1" >&2
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    printf '::error::%s\n' "$(printf '%s' "$1" | tr '\n\r' '  ' | cut -c1-480)"
  fi
  FAILURES=$((FAILURES + 1))
}

notice() {
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    printf '::notice::%s\n' "$(printf '%s' "$*" | tr '\n\r' '  ' | cut -c1-480)"
  fi
}

bad() { emit "$*"; }

# A status code alone rarely explains anything, so include the response head.
brief_body() {
  sed -e 's/<[^>]*>//g' -e 's/[[:space:]][[:space:]]*/ /g' "$BODY" 2>/dev/null | tr -d '\n' | cut -c1-160
}

start_if_needed() {
  if curl -fsS -m 2 "${BASE}/healthz" >/dev/null 2>&1; then
    say "· reusing the panel already listening on ${BASE}"
    return 0
  fi
  say "· booting the panel with autosetup.sh (port ${PORT})"
  bash autosetup.sh --port "$PORT" --skip-apt >/dev/null 2>&1 &
  local job=$! waited=0
  STARTED_BY_US=1
  while [ "$waited" -lt 90 ]; do
    if curl -fsS -m 2 "${BASE}/healthz" >/dev/null 2>&1; then return 0; fi
    if ! kill -0 "$job" 2>/dev/null; then
      say "  autosetup.sh exited early:"
      bash autosetup.sh --status --port "$PORT" || true
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  say "  the panel did not become healthy in 90s - log tail:"
  tail -n 40 instance/skvm.log >&2 || true
  return 1
}

expect_status() { # expect_status <wanted> <path> [accept]
  local wanted="$1" path="$2" accept="${3:-text/html}" code
  CHECKS=$((CHECKS + 1))
  code="$(curl -s -o "$BODY" -w '%{http_code}' -m 25 -b "$JAR" "${PROTO_HEADER[@]}" -H "Accept: ${accept}" "${BASE}${path}")"
  if [ "$code" = "$wanted" ]; then
    printf '  ok   %-46s %s\n' "$path" "$code"
  else
    emit "GET $path -> $code (wanted $wanted) :: $(brief_body)"
  fi
}

expect_contains() { # expect_contains <needle> <path> [accept]
  local needle="$1" path="$2" accept="${3:-text/html}"
  CHECKS=$((CHECKS + 1))
  curl -s -o "$BODY" -m 25 -b "$JAR" "${PROTO_HEADER[@]}" -H "Accept: ${accept}" "${BASE}${path}"
  if grep -qF -- "$needle" "$BODY"; then
    printf '  ok   %-46s contains %s\n' "$path" "$needle"
  else
    emit "$path is missing '$needle' :: $(brief_body)"
  fi
}

login() { # one cookie jar, one CSRF token, exactly like the browser does
  local pw="$1" token
  token="$(curl -s -c "$JAR" -m 20 "${PROTO_HEADER[@]}" "${BASE}/login" | sed -n 's/.*csrf-token" content="\([^"]*\)".*/\1/p' | head -1)"
  if [ -z "$token" ]; then
    bad "no csrf token on /login"
    return 1
  fi
  local code
  code="$(curl -s -b "$JAR" -c "$JAR" -o "$BODY" -w '%{http_code}' -m 20 -X POST "${BASE}/login" \
    "${PROTO_HEADER[@]}" -H 'Content-Type: application/json' -H "X-CSRF-Token: ${token}" \
    -d "{\"username\":\"admin\",\"password\":\"${pw}\"}")"
  CHECKS=$((CHECKS + 1))
  if [ "$code" = "302" ] || [ "$code" = "200" ]; then
    say "  ok   logged in as admin"
  else
    emit "login failed ($code) :: $(brief_body)"
    return 1
  fi
}

csrf() { curl -s -b "$JAR" -m 20 "${PROTO_HEADER[@]}" "${BASE}/dashboard" | sed -n 's/.*csrf-token" content="\([^"]*\)".*/\1/p' | head -1; }

expect_api() { # expect_api <method> <path> [json]
  local method="$1" path="$2" data="${3:-}" token code
  token="$(csrf)"
  CHECKS=$((CHECKS + 1))
  local -a c=(curl -s -b "$JAR" -o "$BODY" -w '%{http_code}' -m 25 -X "$method" "${PROTO_HEADER[@]}" "${BASE}${path}"
              -H 'Accept: application/json' -H "X-CSRF-Token: ${token}")
  if [ -n "$data" ]; then c+=(-H 'Content-Type: application/json' -d "$data"); fi
  code="$("${c[@]}")"
  if [ "$code" = "200" ]; then
    printf '  ok   %-46s api %s\n' "$path" "$method"
  else
    emit "api $method $path -> $code :: $(brief_body)"
  fi
}

if ! start_if_needed; then
  bad "panel never became healthy"
  printf '\n%s checks failed: %s\n' "$CHECKS" "$FAILURES" >&2
  exit 1
fi

say ""
say "· public endpoints"
expect_status 200 "/healthz" "application/json"
expect_contains '"backend"' "/healthz" "application/json"
expect_status 200 "/readyz" "application/json"
expect_status 200 "/version" "application/json"
expect_status 200 "/manifest.webmanifest"
expect_status 200 "/robots.txt"
expect_status 200 "/metrics" "text/plain"
expect_status 200 "/status"
expect_status 302 "/dashboard"
expect_status 200 "/login"
expect_status 200 "/"
expect_status 404 "/definitely-not-a-page"

say ""
say "· assets"
for asset in /static/css/skvm.css /static/css/pages.css /static/js/skvm.js /static/js/charts.js \
             /static/js/pages/dashboard.js /static/vendor/xterm/xterm.js /static/img/favicon.svg; do
  expect_status 200 "$asset"
done

say ""
say "· auth + pages"
PW=""
if [ -f instance/admin-credentials.txt ]; then
  PW="$(sed -n 's/^[[:space:]]*password[[:space:]]*//p' instance/admin-credentials.txt | head -1)"
fi
PW="${PW:-${SKVM_ADMIN_PASSWORD:-}}"
if [ -n "$PW" ]; then
  login "$PW"
  for page in /dashboard /servers/ /servers/new /alerts/ /metrics /docs/ /account/ \
              /admin/ /admin/users /admin/servers /admin/nodes /admin/settings /admin/audit \
              /admin/announcements /admin/incidents; do
    expect_status 200 "$page"
  done
  say ""
  say "· api (cookie + csrf, like the frontend)"
  expect_api GET "/api/v1/servers"
  expect_api GET "/admin/api/v1/overview"
  expect_api GET "/admin/api/v1/nodes"
  # harmless round-trip: writes the value it already has
  expect_api POST "/admin/api/v1/settings" '{"default_os":"ubuntu-24.04"}'
  UID_="$(curl -s -b "$JAR" "${PROTO_HEADER[@]}" -H 'Accept: application/json' "${BASE}/api/v1/servers" \
          | tr ',' '\n' | sed -n 's/.*"uid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  if [ -n "$UID_" ]; then
    expect_status 200 "/servers/${UID_}"
    expect_status 200 "/servers/${UID_}/console"
    expect_api GET "/servers/${UID_}/api/v1/metrics?window=live"
    expect_api GET "/servers/${UID_}/api/v1/events"
  else
    bad "no server uid in the API response"
  fi
else
  bad "no admin password to log in with: export SKVM_ADMIN_PASSWORD or run this next to instance/admin-credentials.txt"
fi

if [ "${CI:-0}" = "1" ] && [ "$STARTED_BY_US" = 1 ]; then
  bash autosetup.sh --stop --port "$PORT" >/dev/null 2>&1 || true
fi

say ""
notice "smoke: ${CHECKS} checks, ${FAILURES} failed · pid $(cat "instance/skvm-${PORT}.pid" 2>/dev/null || echo none) · healthz $(curl -s -o /dev/null -w '%{http_code}' -m 3 "${BASE}/healthz" 2>/dev/null || echo unreachable)"

if [ "$FAILURES" -eq 0 ]; then
  printf '\n\033[1mSKVM smoke: %s checks, all green\033[0m\n' "$CHECKS"
  exit 0
fi
printf '\n\033[1mSKVM smoke: %s checks, %s failed\033[0m\n' "$CHECKS" "$FAILURES" >&2
exit 1
