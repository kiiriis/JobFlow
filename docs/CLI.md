# CLI Reference

Entry point: `jobflow` (installed via `pip install -e .`)

## Commands

### jobflow scan

Scan job boards for relevant positions.

```bash
# Scan all platforms
jobflow scan

# LinkedIn only, last 4 hours
jobflow scan --platform linkedin --hours 4

# Only new jobs (dedup against seen_jobs.json)
jobflow scan --platform linkedin --new --hours 1

# Don't save results
jobflow scan --no-save
```

**Options:**
- `--platform`: lever, greenhouse, ashby, linkedin, github (default: all)
- `--hours`: Max age in hours (default: 72)
- `--new / --no-new`: Deduplicate against previously seen jobs
- `--save / --no-save`: Save to scan_results.json (default: save)

**Platforms & Sources:**
- **Lever**: 11 companies via JSON API
- **Greenhouse**: 40 companies via JSON API
- **Ashby**: 31 companies via JSON API
- **LinkedIn**: 6 search terms via python-jobspy (200 results each)
- **GitHub**: SimplifyJobs + Jobright markdown repos

### jobflow apply

Process a single job posting.

```bash
# From URL (scrapes JD)
jobflow apply "https://example.com/job" -t "SWE" -c "Stripe" -l "SF, CA"

# From clipboard/stdin
jobflow apply "https://example.com/job" --paste -t "SWE" -c "Stripe"

# Skip filter check
jobflow apply "https://example.com/job" --no-filter -t "SWE" -c "Stripe"
```

### jobflow save

Merge tailored resume sections and compile PDF.

```bash
jobflow save --dir data/output/Stripe_SWE_2026-04-08
jobflow save --dir <path> --variant ml
jobflow save --dir <path> --sections custom_sections.tex
```

### jobflow process

Process jobs from scan results.

```bash
# Show available jobs
jobflow process

# Process job #3
jobflow process 3

# Process all relevant jobs
jobflow process --all
```

### jobflow list

View tracked applications.

```bash
jobflow list
jobflow list --status Applied
jobflow list --status Interview
```

### jobflow status

Update application status.

```bash
jobflow status 3 Applied
jobflow status 3 Interview --notes "Phone screen scheduled"
```

### jobflow web

Launch the web dashboard.

```bash
jobflow web              # Default port 8080
jobflow web --port 3000  # Custom port
```

### jobflow init

First-time setup — creates config.yaml and directories.

```bash
jobflow init
```
