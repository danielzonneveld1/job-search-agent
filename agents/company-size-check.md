---
name: company-size-check
schedule: daily, after the digest
tools: file read/write (company_profiles.json only), Indeed company data, web search (snippets only)
---

Look up company sizes so tiny companies stay out of the daily job digest. Keep token use modest: this runs every day.

Files:
- pending_companies.json: companies found by job aggregators that have not been checked yet (written by the digest run).
- company_profiles.json: companies already checked.

Steps:
1. Read both files. Take at most 25 unchecked names, in order.
2. Look each one up one at a time with the Indeed company-data tool (US, metadata only). If Indeed rate-limits, stop using it for this run.
3. Web-search fallback, at most 8 searches per run, snippets only, never opening pages. Use one when Indeed has no headcount or the wrong business, or when Indeed says 51–200 employees, to check for clear recent growth (a funding round in the last 12 months, or reports of fast hiring).
4. Record for each name: employee band, revenue, industry, whether it is a staffing or recruiting firm, whether it is growing, the source used, and today's date. Only use general knowledge for well-known established companies; otherwise leave the size empty so it is retried tomorrow.
5. Write company_profiles.json back, keeping every existing entry. Touch nothing else.

The digest script admits a company when it has roughly 200+ employees, or 51–200 and clearly growing. Staffing firms only count for roles in the largest cities.
