---
name: funding-watchlist-check
schedule: weekly
tools: Gmail read, file read/write (watched_companies.json only)
---

Keep a watch-list of recently funded companies that the daily job search treats as extra targets.

Steps:
1. Find the last week's issues of a VC funding newsletter in Gmail. If there are none, stop.
2. Read the funding sections (skip new VC funds themselves). For each funded operating company, judge whether it is a plausible hiring target for the roles in the search profile: a meaningful round, a visible commercial team, and a product with a real customer base.
3. Read watched_companies.json and append each qualifying company that isn't already there (by name or alias) as `{name, aliases, sector, added_date, source, note}`, where `note` is a one-line funding summary.
4. Write the file back, keeping every existing entry. Do not run the search or send email. The next daily run picks up the new companies automatically.

When unsure, lean towards including: a wrongly added company costs one extra lookup a day, while a wrongly skipped one could mean a missed role.
