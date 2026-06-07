#!/usr/bin/env bash
# Shared secret scanner for the pre-commit and pre-push hooks.
#
#   scan-secrets.sh staged          # scan staged changes (pre-commit)
#   scan-secrets.sh range <range>   # scan a git-log range (pre-push)
#
# Runs BOTH gitleaks (regex rules + Shannon-entropy heuristics, when installed)
# AND a bundled regex/entropy fallback, and blocks if EITHER flags something.
# Belt-and-suspenders: gitleaks catches random high-entropy secrets, the
# fallback catches secret-shaped assignments (e.g. long hex) regardless of
# entropy, and the fallback also keeps protection working where gitleaks is
# not installed (other clones / worker containers).
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
MODE="${1:-staged}"
RANGE="${2:-}"

GL=""
for c in gitleaks "$HOME/.local/bin/gitleaks" /usr/local/bin/gitleaks /usr/bin/gitleaks; do
  if command -v "$c" >/dev/null 2>&1; then GL="$(command -v "$c")"; break; fi
  if [ -x "$c" ]; then GL="$c"; break; fi
done

CFGARG=()
[ -f "$ROOT/.gitleaks.toml" ] && CFGARG=(-c "$ROOT/.gitleaks.toml")

fail=0
msg=""

run_gitleaks() {
  [ -n "$GL" ] || { msg+=$'\n[gitleaks] not installed - using fallback only'; return; }
  local out
  if [ "$MODE" = "staged" ]; then
    out="$("$GL" git --staged --redact --no-banner "${CFGARG[@]}" "$ROOT" 2>&1)" || { fail=1; msg+=$'\n[gitleaks]\n'"$out"; }
  else
    out="$("$GL" git --redact --no-banner "${CFGARG[@]}" --log-opts="$RANGE" "$ROOT" 2>&1)" || { fail=1; msg+=$'\n[gitleaks]\n'"$out"; }
  fi
}

run_fallback() {
  local out
  out="$(python3 "$ROOT/.githooks/secret-fallback.py" "$MODE" "$RANGE" 2>&1)" || { fail=1; msg+=$'\n[fallback]\n'"$out"; }
}

run_gitleaks
run_fallback

if [ "$fail" = "1" ]; then
  {
    echo ""
    echo "==================== SECRET SCAN: BLOCKED ===================="
    echo "Potencjalny sekret w zmianach - operacja git przerwana."
    echo "$msg" | sed 's/^/  /'
    echo ""
    echo "Falszywy alarm? Popraw wartosc lub dodaj wpis do .gitleaks.toml [allowlist]."
    echo "Awaryjne pominiecie (NIE zalecane): dodaj  --no-verify"
    echo "============================================================="
  } >&2
  exit 1
fi
exit 0
