---
name: digest-feedback-learning
schedule: daily, 30 minutes before the digest
tools: file read/write (learned_rules.json only), Google Sheets read
---

Turn the user's newest job-digest feedback notes into filter rules for the next digest. Keep token use small: this runs every day, and most days there is little or nothing new.

Background: a Python script emails a daily job digest. Each role has "Applied" and "Not for me" buttons. "Not for me" records a reason and an optional free-text note in the "Digest Feedback" tab of the tracker Sheet. The script reads extra rules from learned_rules.json, which is the ONLY file you may change.

The script ALREADY handles these, so never add rules for them: roles needing more experience than the profile allows; internships requiring current enrollment; postings requiring another language; locations outside the profile; sales-development roles under broader titles; small companies; roles already applied to or clicked; closed listings.

Steps:
1. Read learned_rules.json.
2. Read the "Digest Feedback" tab (columns: Date, Verdict, Reason, Title, Company, Sector, Location, Source, Link, Role ID, Note).
3. Keep only "Not for me" rows dated after `last_reviewed`. If none, stop without writing anything.
4. For each, decide whether the note shows a clear, lasting preference. Be conservative; when unsure, add nothing. Ignore notes about a single listing ("not available", "skills don't match"). Rule types:
   - `exclude_title_phrases`: a specific phrase (two or more words, or one unambiguous role word) for a role type the user never wants. Never a broad word like analyst, associate, coordinator, specialist, manager.
   - `exclude_companies`: a company the user says they never want.
   - `exclude_description_phrases`: an exact phrase that only appears when a requirement the user lacks is mandatory, and only once it has come up in at least two notes.
   At most 3 new rules per run; skip duplicates.
5. Write the file back: append new values, add a `rules_log` entry per rule (`added`, `type`, `value`, a short plain-English `rule` shown in the next email, and `because`), and move `last_reviewed` forward.
6. Touch nothing else.
