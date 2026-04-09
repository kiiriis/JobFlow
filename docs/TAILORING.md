# Resume Tailoring System

## Overview

JobFlow tailors LaTeX resumes to specific job descriptions using Claude AI. It supports three resume variants, iterative refinement, and auto-condensing to ensure a single-page output.

## Resume Variants

| Variant | Focus | File |
|---------|-------|------|
| `se` | Software Engineering (default) | `resumes/base/KrishMakadiaSE.tex` |
| `ml` | Machine Learning / AI | `resumes/base/KrishMakadiaML.tex` |
| `appdev` | Full-Stack / App Development | `resumes/base/KrishMakadiaAppDev.tex` |

Variant is auto-selected based on JD keywords (ML keywords → ml, React/Vue/frontend → appdev, else → se).

## Tailoring Flow

### Via Web Dashboard (`/tailor`)

1. User pastes JD text
2. Selects model (sonnet/opus/haiku) and effort (low/medium/high)
3. **Pre-filter check**:
   - Reject if JD contains visa disqualifiers
   - Reject if 3+ senior signals AND 0 entry signals
4. Session created with UUID
5. Background thread runs Claude CLI:
   ```
   claude --model sonnet --output-format text "prompt..."
   ```
6. Claude returns complete `.tex` file
7. META line parsed: `META: company=X | role=Y | location=Z`
8. LaTeX extracted from output (strips markdown fences)
9. Saved to `data/output/tailor_<session_id>/`
10. Compiled with pdflatex (runs twice for cross-refs)
11. **Page count check**: If PDF > 1 page, auto-condense triggered
12. PDF served via iframe preview

### Refinement

User can submit feedback (e.g., "emphasize AWS experience"):
1. Current `.tex` + feedback sent to Claude
2. New iteration generated
3. Re-compiled and page-checked
4. Iteration counter incremented

### Cancellation

- Sessions can be cancelled mid-generation
- Claude subprocess killed via `SIGKILL`
- Session marked as cancelled

### Via CLI

```bash
# Process a job
jobflow apply "https://..." --paste -t "SWE" -c "Stripe" -l "SF"

# Save tailored resume
jobflow save --dir data/output/Stripe_SWE_2026-04-08
```

## Key Functions

### `tailor.py`

- `load_base_resume(variant, config)` — Load `.tex` template
- `load_master_prompt(config)` — Load `resumes/prompt.md`
- `extract_preamble_and_education(tex)` — Get header + education section
- `merge_resume(preamble, tailored_sections)` — Combine preamble + Claude output
- `build_tailor_prompt(job, base_tex, master_prompt)` — Assemble full Claude prompt
- `save_tailored_resume(tex_content, output_dir, company, role)` — Write `.tex` file

### `latex.py`

- `compile_pdf(tex_path, final_name)` — Run pdflatex, clean artifacts
- `get_page_count(pdf_path)` — Count pages via PDF bytes regex

### `web/__init__.py` (tailor routes)

- `_run_tailor(session_id, config)` — Main tailoring thread
- `_run_tailor_refine(session_id, config, feedback)` — Refinement thread
- `_auto_condense(session_id, config)` — Page reduction thread
- `_run_claude(session, prompt)` — Subprocess wrapper for Claude CLI
- `_extract_tex_from_output(output)` — Parse LaTeX from Claude response
- `_parse_meta_line(output)` — Extract company/role/location from META line

## Session Management

- Stored in-memory: `tailor_sessions` dict
- Max 20 sessions (oldest completed evicted via `_evict_old_sessions()`)
- Session fields: id, status, jd_text, variant, company, role, location, pdf_path, current_tex, iteration, model, effort, feedback_history, output_dir, _process, _cancelled, _created_at

## Dependencies

- **Claude CLI** — must be installed and authenticated (`claude` command)
- **pdflatex** — from MacTeX/TexLive (`pdflatex` command)
- Only works locally, not on Render deployment
