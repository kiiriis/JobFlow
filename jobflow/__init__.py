"""JobFlow — Automated job scanner and resume tailoring system.

Scans LinkedIn and ATS job boards (Lever, Greenhouse, Ashby) on an hourly
cron via GitHub Actions, scores each posting against a Python/ML/Backend
tech stack, and surfaces results through a live Flask dashboard.

Architecture overview:
    GitHub Actions (hourly)  ->  scanner.py  ->  filter.py  ->  linkedin_store.py
         |                                                            |
    data/ci/scan_results.json                              data/ci/linkedin_jobs.json
                                                                      |
                                                               web/__init__.py
                                                            (Flask dashboard on Render)

Modules:
    scanner.py       - Multi-platform job aggregator (LinkedIn, Lever, Greenhouse, Ashby, GitHub)
    filter.py        - Multi-signal scoring engine with hard-reject pipeline (0-100%)
    linkedin_store.py - Persistent job store with merge, dedup, prune, and filtering
    cli.py           - Typer CLI (scan, apply, save, process, list, status, init, web)
    web/__init__.py  - Flask app factory with HTMX-powered dashboard
    ai_scorer.py     - Optional Llama 4 Scout relevance scoring via Groq (0-10 scale)
    tailor.py        - LaTeX resume manipulation (merge preamble + tailored sections)
    latex.py         - pdflatex compilation wrapper
    tracker.py       - CSV-based application tracking
    scraper.py       - Job description text parser
    config.py        - YAML config loader with path resolution
    models.py        - Core dataclasses (JobPosting, FilterResult)
"""

__version__ = "0.1.0"
