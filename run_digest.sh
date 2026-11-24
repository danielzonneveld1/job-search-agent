#!/bin/zsh
# Starts the daily digest in the background and returns immediately, so the
# scheduled task never hits its 10-minute command limit (the search alone
# now takes ~11 minutes). job_search.py sends the email itself, and emails a
# failure notice if it crashes.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
pidfile="logs/digest.pid"
if [[ -f "$pidfile" ]] && ps -p "$(cat "$pidfile")" -o command= 2>/dev/null | grep -q "job_search.py"; then
  echo "A job search run is already in progress (pid $(cat "$pidfile")); not starting another."
  exit 0
fi
log="logs/digest-$(date +%Y-%m-%d).log"
nohup .venv/bin/python3 job_search.py >> "$log" 2>&1 &
echo $! > "$pidfile"
echo "Started the job search (pid $!). It sends the digest itself in ~15 minutes; log: $log"
