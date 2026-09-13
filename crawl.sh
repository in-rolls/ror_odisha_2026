#!/bin/bash
set -eu
cd "$(dirname "$0")"
mkdir -p logs
if [[ -L raw && ! -d raw ]]; then
    printf '%s data volume unavailable; retrying after five minutes\n' "$(date -Iseconds)" >> logs/crawl.out
    sleep 300
    exit 1
fi
printf '%s start\n' "$(date -Iseconds)" >> logs/crawl.out
priority_args=()
if [[ -f raw/repair-villages.json ]]; then
    priority_args=(--priority-file raw/repair-villages.json)
fi
if /usr/bin/caffeinate -dims .venv/bin/python fetch_ror.py --workers 256 "${priority_args[@]}" >> logs/crawl.out 2>&1; then
    exit 0
fi
printf '%s incomplete pass; retrying after five minutes\n' "$(date -Iseconds)" >> logs/crawl.out
sleep 300
exit 1
