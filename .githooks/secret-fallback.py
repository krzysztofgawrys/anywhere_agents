#!/usr/bin/env python3
"""Fallback secret scanner used when gitleaks is not installed.

Scans ADDED lines (the '+' side of a diff) for provider-prefixed tokens,
private-key headers, and high-entropy values assigned to secret-looking keys.
Conservative by design - the primary scanner is gitleaks.

Usage:
    secret-fallback.py staged
    secret-fallback.py range "<git-log-range>"
"""
import math
import re
import subprocess
import sys
from collections import Counter

mode = sys.argv[1] if len(sys.argv) > 1 else "staged"
arg = sys.argv[2] if len(sys.argv) > 2 else ""

if mode == "staged":
    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color"],
        capture_output=True, text=True,
    ).stdout
else:
    diff = subprocess.run(
        ["git", "log", "-p", "--unified=0", "--no-color", *arg.split()],
        capture_output=True, text=True,
    ).stdout

PATTERNS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{30,}",
    r"sk-ant-[A-Za-z0-9_-]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"AIza[0-9A-Za-z_-]{30,}",
    r"glpat-[A-Za-z0-9_-]{20,}",
    r"xox[baprs]-[A-Za-z0-9-]{20,}",
    r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.",
]
ASSIGN = re.compile(
    r"(?i)(secret|token|password|passwd|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|private[_-]?key)\s*[:=]\s*[\"']?([A-Za-z0-9/+_\-.]{16,})"
)
PLACEHOLDER = re.compile(r"(\$\{[^}]+\}|<[^>\n]{1,60}>|changeme|x{3,}|\.\.\.)", re.I)


def entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


findings = []
for raw in diff.splitlines():
    if not raw.startswith("+") or raw.startswith("+++"):
        continue
    line = raw[1:]
    matched = False
    for p in PATTERNS:
        if re.search(p, line):
            findings.append((p, line.strip()[:120]))
            matched = True
            break
    if matched:
        continue
    m = ASSIGN.search(line)
    if m:
        val = m.group(2)
        if PLACEHOLDER.search(val):
            continue
        if re.fullmatch(r"[0-9a-fA-F]{32,}", val) or entropy(val) >= 4.0:
            findings.append(("high-entropy-assignment", line.strip()[:120]))

if findings:
    print("Fallback skaner sekretow - trafienia:", file=sys.stderr)
    for rule, ln in findings[:20]:
        print(f"  [{rule}] {ln}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
