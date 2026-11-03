#!/usr/bin/env bash
# FREE signal mode on Linux/Mac. Add to crontab (crontab -e), Mon-Fri 16:45 IST:
#   45 16 * * 1-5 /path/to/New-Projects-/scripts/run_signals.sh
cd "$(dirname "$0")/.." || exit 1
source .venv/bin/activate 2>/dev/null || true
mkdir -p state
TZ=Asia/Kolkata python -m swingbot signals >> state/signals.log 2>&1
