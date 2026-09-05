#!/bin/bash
# Check the Record-of-Rights crawl and alert when it is not healthy.
#
# Run from cron every two hours. It compares against the last run's numbers,
# because most of the ways this crawl fails do not stop the process: macOS
# throttles it to near zero on battery, and a DNS dropout leaves it spinning
# while the record count sits still. A liveness check alone would call all of
# that healthy.
#
# Alerts go to a desktop notification and to logs/monitor.log. The log is the
# reliable half -- notifications depend on a logged-in GUI session.
#
#   crontab:  0 */2 * * * /Users/you/Documents/GitHub/odisha-ror/monitor.sh

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HERE/logs/crawl.out"
STATE="$HERE/logs/monitor_state"
ALERTS="$HERE/logs/monitor.log"
STALL_MINUTES=20
SLOW_PER_MIN=60
FAIL_SHARE_PCT=25
MIN_WINDOW_MINUTES=5

now=$(date "+%Y-%m-%d %H:%M")
problems=()
note=""

# --- is it running at all ---
if ! pgrep -f "fetch_ror.py --workers" >/dev/null; then
    problems+=("crawler is NOT running")
fi

# --- has the log moved recently ---
if [[ -f "$LOG" ]]; then
    age=$(( ($(date +%s) - $(stat -f %m "$LOG")) / 60 ))
    (( age > STALL_MINUTES )) && problems+=("log has not been written for ${age}m")
else
    problems+=("no log at $LOG")
fi

# --- has the record count moved since the last check ---
last_line=$(grep INFO "$LOG" 2>/dev/null | tail -1)
# The log writes "27173 failed," with a trailing comma, so matching the bare
# word leaves the count empty and every later sum silently reads as zero.
records=$(echo "$last_line" | sed -n 's/.*INFO \([0-9]*\) records.*/\1/p')
failed=$(echo  "$last_line" | sed -n 's/.*cells, \([0-9]*\) failed.*/\1/p')
left=$(echo    "$last_line" | sed -n 's/.* \([0-9]*\) villages left.*/\1/p')
if [[ -z "$records" || -z "$failed" ]]; then
    problems+=("cannot read the progress line -- log format may have changed")
    records=${records:-0}; failed=${failed:-0}
fi

if [[ -f "$STATE" ]]; then
    read -r prev_records prev_failed prev_epoch < "$STATE"
    mins=$(( ($(date +%s) - prev_epoch) / 60 ))
    # The crawl writes one progress line a minute, so a check run moments after
    # the last one sees no movement and would cry stall. Only judge the rate
    # once enough time has passed for there to be something to judge.
    if (( mins < MIN_WINDOW_MINUTES )); then
        note="last check ${mins}m ago -- too soon to judge the rate"
        mins=0
    fi
    gained=$(( records - prev_records ))
    fgained=$(( failed - prev_failed ))
    if (( mins > 0 )); then
      rate=$(( gained / mins ))
      note="+${gained} records in ${mins}m (${rate}/min), +${fgained} failed"
      if (( gained <= 0 )); then
        problems+=("no new records in ${mins}m -- stalled")
      elif (( rate < SLOW_PER_MIN )); then
        problems+=("only ${rate} records/min over ${mins}m")
      fi
      if (( gained + fgained > 0 )); then
        share=$(( 100 * fgained / (gained + fgained) ))
        (( share > FAIL_SHARE_PCT )) && problems+=("${share}% of fetches failing")
      fi
    fi
fi
echo "$records $failed $(date +%s)" > "$STATE"

# --- power: the single commonest cause of a slowdown here ---
if pmset -g batt | grep -q "Battery Power"; then
    pct=$(pmset -g batt | grep -o "[0-9]*%" | head -1)
    problems+=("on battery ($pct) -- macOS will throttle the crawl")
fi

if (( ${#problems[@]} == 0 )); then
    printf '%s  OK    %s villages left. %s\n' "$now" "${left:-?}" "$note" >> "$ALERTS"
    exit 0
fi

summary=$(printf '%s; ' "${problems[@]}"); summary=${summary%; }
printf '%s  ALERT %s | %s\n' "$now" "$summary" "$note" >> "$ALERTS"
osascript -e "display notification \"$summary\" with title \"Odisha RoR crawl\" sound name \"Basso\"" 2>/dev/null
exit 1
