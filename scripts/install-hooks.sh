#!/usr/bin/env bash
# Enable the committed secret-scanning git hooks for this clone.
# Run once after cloning:  ./scripts/install-hooks.sh
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
git -C "$ROOT" config core.hooksPath .githooks
chmod +x "$ROOT"/.githooks/pre-commit "$ROOT"/.githooks/pre-push "$ROOT"/.githooks/scan-secrets.sh 2>/dev/null || true

echo "Git hooks enabled (core.hooksPath=.githooks)."
if command -v gitleaks >/dev/null 2>&1 || [ -x "$HOME/.local/bin/gitleaks" ]; then
  echo "gitleaks detected -> primary scanner active."
else
  echo "gitleaks NOT found -> hooks use the bundled fallback scanner (.githooks/secret-fallback.py)."
  echo "For stronger coverage install gitleaks: https://github.com/gitleaks/gitleaks"
fi
