# JobFlow

Milan-specific automated job scanner and dashboard for new grad / entry-level semiconductor hardware roles.

## Target Roles

- ASIC Design Engineer
- ASIC Verification Engineer
- ASIC Physical Design Engineer
- SoC Design Engineer
- SoC Verification Engineer
- SoC Physical Design Engineer
- FPGA Design Engineer
- RTL / VLSI / Silicon Design or Verification Engineer
- GPU ASIC Engineer

## Constraints

- US-based roles only
- Visa-sponsorship friendly for an international student on OPT/H1B/CPT path
- Reject no-sponsorship, citizenship-only, green-card-only, and clearance-required jobs
- Reject embedded-only, firmware-only, generic SWE, backend, frontend, data, ML/AI, DevOps/SRE, IT/support, product, sales, software QA/testing, management, senior/staff/principal/lead roles

## Important Files

- `docs/MILAN_SETUP.md` — Milan handoff guide for GitHub Actions, Neon, and local dashboard use
- `jobflow/scanner.py` — LinkedIn search terms and target title prefilter
- `jobflow/filter.py` — hardware scoring and hard-reject logic
- `jobflow/ai_scorer.py` — optional Groq AI scoring prompt
- `config/profile.txt` — Milan candidate profile
- `.github/workflows/scan-jobs.yml` — scheduled scanner and Neon merge
- `render.yaml` — optional Render web service config

## Commands

- `pytest` — run the full test suite
- `DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow web` — launch local dashboard against Neon
- `DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow scan --platform linkedin --save --hours 24` — manual LinkedIn scan

## Deployment

Use Neon Postgres via `DATABASE_URL` in GitHub Actions and local dashboard runs. Render is optional; the JSON files in `data/ci/` are fallback storage only and should start empty for Milan.
