---
name: job-search-daily-digest
schedule: daily, 8:00
tools: one shell command
---

Run `./run_digest.sh` from the project folder, exactly once, and nothing else.

It starts the search in the background and returns immediately. The script builds and sends the digest itself about 15 minutes later, and emails a failure notice if anything crashes. Do not wait for it, re-run it, or send any email yourself.
