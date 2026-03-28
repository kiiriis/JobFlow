# JobFlow

Automated job scanner and resume tailoring CLI for new grad / entry-level SWE positions.

## Project Structure

```
JobFlow/
├── config/
│   ├── config.yaml          # Main config (resume paths, output dirs)
│   └── job_boards.json      # Job board API endpoints + career pages
├── resumes/
│   ├── base/                # Base LaTeX resumes (SE, ML, AppDev variants)
│   └── prompt.md            # Master resume tailoring prompt
├── data/
│   ├── applications.csv     # Tracked job applications
│   └── output/              # Generated resumes + scan results
├── jobflow/                 # Python package
│   ├── cli.py               # Typer CLI commands
│   ├── config.py            # Config loader
│   ├── scanner.py           # Job board scanners (API + LinkedIn + GitHub)
│   ├── filter.py            # Job relevance filter
│   ├── tailor.py            # Resume section merging
│   ├── latex.py             # PDF compilation
│   ├── tracker.py           # CSV tracking
│   ├── scraper.py           # Job posting parser
│   └── models.py            # Data models
├── pyproject.toml
└── CLAUDE.md
```

## Commands
- `jobflow scan` — Scan all sources (82 API companies + LinkedIn + GitHub repos)
- `jobflow scan --hours 24` — Only jobs posted in last 24 hours
- `jobflow scan --new` — Only jobs not seen in previous scans
- `jobflow scan --platform <name>` — One source: lever, greenhouse, ashby, linkedin, github
- `jobflow apply <url> --paste -t "Title" -c "Company" -l "Location"` — Process a job
- `jobflow save --dir <path>` — Merge tailored sections + compile PDF
- `jobflow process <#>` — Process a job from scan results
- `jobflow list` — View tracked applications
- `jobflow init` — First-time setup

## Filter Criteria
- New grad / entry-level / SDE 1 roles only
- USA-based positions
- Must NOT deny visa sponsorship
- OPT/F1 friendly
- Software engineering roles only
