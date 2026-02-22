#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

echo "[1/5] Inspect status"
git status --short

echo "[2/5] Exclude known noisy artifacts from staging"
git restore --staged codex-notification.txt 2>/dev/null || true

echo "[3/5] Stage intended files"
git add src/workflows/multi_agent_workflow.py tests/test_multi_agent_workflow.py

echo "[4/5] Run focused verification"
PYTHONPATH=src python3 -m unittest -q tests.test_multi_agent_workflow

echo "[5/5] Commit"
git commit -m "feat(workflows): orchestrate role-based multi-agent execution"

echo "Commit complete."
