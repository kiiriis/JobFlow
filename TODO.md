# TODO

## Task 1 — Fix location filtering (still getting non-US jobs)

**Priority:** High

The LinkedIn scanner is set to `location="United States"` but some jobs from Canada and other countries still leak through. Need to investigate:

- Is python-jobspy returning non-US results despite the location filter?
- Should there be a post-scan location filter that rejects jobs with non-US locations?
- Check if the hard-reject pipeline in `filter.py` needs a location validation step (currently it trusts the search parameter and gives a flat `loc_score = 10`)

**Goal:** Zero non-US jobs in the feed.

## ~~Task 2 — Deleted jobs reappear after next scan~~ ✅ DONE

Fixed: `dismissed_jobs` table tracks deleted URLs permanently. `merge_scan_results()` skips them.

## Task 3 — Address remaining security concerns from DB migration audit

**Priority:** Low

See `DB_MIGRATION_AUDIT.md` for full details. Key open items:

- **Issue #8:** Race condition in scan trigger (low risk, single-user app)
- General: review f-string SQL construction patterns for long-term safety
