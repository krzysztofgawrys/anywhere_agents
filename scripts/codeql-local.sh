#!/usr/bin/env bash
# -------------------------------------------------------------------
# codeql-local.sh - Run CodeQL analysis locally, matching the GitHub
# Actions "default setup" code-scanning workflow.
#
# Reproduces the same steps as:
#   https://github.com/krzysztofgawrys/anywhere_agents/actions
#   (CodeQL default setup - python, javascript, actions)
#
# Usage:
#   ./scripts/codeql-local.sh              # all three languages
#   ./scripts/codeql-local.sh python        # python only
#   ./scripts/codeql-local.sh javascript    # javascript/typescript only
#   ./scripts/codeql-local.sh actions       # GitHub Actions YAML only
#   ./scripts/codeql-local.sh python javascript  # multiple languages
#
# Prerequisites:
#   - curl, tar, git
#   - ~2 GB disk for the CodeQL bundle (cached after first run)
#
# The script downloads CodeQL CLI 2.25.5 (same version as CI) on first
# run and caches it under ~/.codeql-cli. Subsequent runs reuse it.
# -------------------------------------------------------------------
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────
CODEQL_VERSION="2.25.5"
CODEQL_BUNDLE_TAG="codeql-bundle-v${CODEQL_VERSION}"
CODEQL_BUNDLE_URL="https://github.com/github/codeql-action/releases/download/${CODEQL_BUNDLE_TAG}/codeql-bundle-linux64.tar.gz"

CACHE_DIR="${CODEQL_CACHE_DIR:-$HOME/.codeql-cli}"
CODEQL_DIR="${CACHE_DIR}/codeql-${CODEQL_VERSION}"
CODEQL_BIN="${CODEQL_DIR}/codeql/codeql"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="${REPO_ROOT}/.codeql-local"
DB_DIR="${WORK_DIR}/databases"
RESULTS_DIR="${WORK_DIR}/results"

# Match CI resource allocation (scaled down for local)
THREADS="${CODEQL_THREADS:-0}"  # 0 = auto-detect
RAM="${CODEQL_RAM:-4096}"

ALL_LANGUAGES=(python javascript actions)

# ── Helpers ────────────────────────────────────────────────────────
info()  { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()    { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m  %s\n' "$*"; }
err()   { printf '\033[1;31m[ERR]\033[0m   %s\n' "$*" >&2; }
die()   { err "$@"; exit 1; }

# ── Parse arguments ───────────────────────────────────────────────
LANGUAGES=()
for arg in "$@"; do
    case "$arg" in
        python|javascript|actions)
            LANGUAGES+=("$arg")
            ;;
        -h|--help)
            sed -n '2,/^# ---/{ /^#/s/^# \?//p }' "$0"
            exit 0
            ;;
        *)
            die "Unknown argument: $arg (expected: python, javascript, actions)"
            ;;
    esac
done
if [[ ${#LANGUAGES[@]} -eq 0 ]]; then
    LANGUAGES=("${ALL_LANGUAGES[@]}")
fi

# ── Step 1: Install CodeQL CLI ────────────────────────────────────
install_codeql() {
    if [[ -x "$CODEQL_BIN" ]]; then
        local installed
        installed=$("$CODEQL_BIN" version --format=terse 2>/dev/null || echo "")
        if [[ "$installed" == "$CODEQL_VERSION" ]]; then
            ok "CodeQL CLI ${CODEQL_VERSION} already installed"
            return
        fi
    fi

    info "Downloading CodeQL CLI ${CODEQL_VERSION} bundle..."
    mkdir -p "$CODEQL_DIR"

    local tarball="${CACHE_DIR}/codeql-bundle-${CODEQL_VERSION}.tar.gz"
    if [[ ! -f "$tarball" ]]; then
        curl -fsSL -o "$tarball.tmp" "$CODEQL_BUNDLE_URL"
        mv "$tarball.tmp" "$tarball"
        ok "Downloaded $(du -h "$tarball" | cut -f1) bundle"
    else
        ok "Using cached bundle: $tarball"
    fi

    info "Extracting CodeQL CLI..."
    tar -xzf "$tarball" -C "$CODEQL_DIR"
    ok "CodeQL CLI ${CODEQL_VERSION} installed at ${CODEQL_DIR}"
}

# ── Step 2: Create database ──────────────────────────────────────
create_database() {
    local lang="$1"
    local db_path="${DB_DIR}/${lang}"

    info "[${lang}] Creating CodeQL database..."

    rm -rf "$db_path"

    "$CODEQL_BIN" database create \
        "$db_path" \
        --language="$lang" \
        --source-root="$REPO_ROOT" \
        --build-mode=none \
        --threads="$THREADS" \
        --overwrite \
        2>&1 | while IFS= read -r line; do
            # Show progress without flooding the terminal
            case "$line" in
                *"Successfully"*|*"Created"*|*"Finalizing"*|*"Running"*)
                    info "[${lang}]   $line" ;;
            esac
        done

    ok "[${lang}] Database created at ${db_path}"
}

# ── Step 3: Run queries ──────────────────────────────────────────
run_queries() {
    local lang="$1"
    local db_path="${DB_DIR}/${lang}"

    # Use the same query suite as GitHub's default setup (code-scanning).
    # The bundled pack includes the default suite; run-queries with just
    # the db path and no explicit suite triggers the default suite for
    # the language, which matches CI behavior.
    info "[${lang}] Running queries..."
    "$CODEQL_BIN" database run-queries \
        "$db_path" \
        --ram="$RAM" \
        --threads="$THREADS" \
        -v \
        2>&1 | while IFS= read -r line; do
            case "$line" in
                *"Loaded"*|*"Evaluating"*|*"Shutting"*|*"evaluated"*)
                    info "[${lang}]   $line" ;;
            esac
        done

    ok "[${lang}] Queries completed"
}

# ── Step 4: Interpret results into SARIF ─────────────────────────
interpret_results() {
    local lang="$1"
    local db_path="${DB_DIR}/${lang}"
    local sarif_path="${RESULTS_DIR}/${lang}.sarif"

    info "[${lang}] Interpreting results..."
    mkdir -p "$RESULTS_DIR"

    "$CODEQL_BIN" database interpret-results \
        "$db_path" \
        --format=sarifv2.1.0 \
        --output="$sarif_path" \
        --sarif-add-query-help \
        2>&1 | while IFS= read -r line; do
            case "$line" in
                *"Interpreted"*|*"Exported"*)
                    info "[${lang}]   $line" ;;
            esac
        done

    ok "[${lang}] SARIF written to ${sarif_path}"
}

# ── Step 5: Print summary ────────────────────────────────────────
print_summary() {
    local lang="$1"
    local sarif_path="${RESULTS_DIR}/${lang}.sarif"

    if [[ ! -f "$sarif_path" ]]; then
        warn "[${lang}] No SARIF file found"
        return
    fi

    # Count results using python (more reliable than jq for nested SARIF)
    local count
    count=$(python3 -c "
import json, sys
sarif = json.load(open('${sarif_path}'))
total = 0
for run in sarif.get('runs', []):
    total += len(run.get('results', []))
print(total)
" 2>/dev/null || echo "?")

    if [[ "$count" == "0" ]]; then
        ok "[${lang}] No alerts found"
    else
        warn "[${lang}] ${count} alert(s) found"
        # Print each alert on one line
        python3 -c "
import json
sarif = json.load(open('${sarif_path}'))
for run in sarif.get('runs', []):
    rules = {r['id']: r for r in run.get('tool', {}).get('driver', {}).get('rules', [])}
    for result in run.get('results', []):
        rule_id = result.get('ruleId', '?')
        rule = rules.get(rule_id, {})
        severity = rule.get('properties', {}).get('security-severity', rule.get('defaultConfiguration', {}).get('level', '?'))
        msg = result.get('message', {}).get('text', '').split('\n')[0][:80]
        locs = result.get('locations', [])
        if locs:
            phys = locs[0].get('physicalLocation', {})
            path = phys.get('artifactLocation', {}).get('uri', '?')
            line = phys.get('region', {}).get('startLine', '?')
            print(f'  {rule_id:45s} {severity:>5s}  {path}:{line}')
            print(f'    {msg}')
        else:
            print(f'  {rule_id:45s} {severity:>5s}  (no location)')
            print(f'    {msg}')
" 2>/dev/null || true
    fi
}

# ── Main ──────────────────────────────────────────────────────────
main() {
    info "CodeQL local analysis"
    info "  Repository: ${REPO_ROOT}"
    info "  Languages:  ${LANGUAGES[*]}"
    info "  Threads:    ${THREADS} (0=auto)"
    info "  RAM:        ${RAM} MB"
    echo

    install_codeql
    echo

    mkdir -p "$DB_DIR" "$RESULTS_DIR"

    for lang in "${LANGUAGES[@]}"; do
        info "=== Analyzing: ${lang} ==="
        create_database "$lang"
        run_queries "$lang"
        interpret_results "$lang"
        echo
    done

    info "=== Summary ==="
    for lang in "${LANGUAGES[@]}"; do
        print_summary "$lang"
    done
    echo

    ok "Full SARIF results in: ${RESULTS_DIR}/"
    info "To compare with GitHub, run:"
    info "  cat ${RESULTS_DIR}/python.sarif | python3 -m json.tool | grep ruleId"
}

main
