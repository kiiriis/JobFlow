# Resume Tailoring System

## Overview

JobFlow tailors LaTeX resumes to specific job descriptions using Claude AI. It supports three resume variants. Tailoring is CLI-only: `jobflow apply` builds a prompt you feed to Claude, then `jobflow save` merges the returned sections and compiles a PDF with pdflatex.

## Resume Variants

| Variant | Focus | File |
|---------|-------|------|
| `se` | Software Engineering (default) | `resumes/base/SE.tex` |
| `ml` | Machine Learning / AI | `resumes/base/ML.tex` |
| `appdev` | Full-Stack / App Development | `resumes/base/AppDev.tex` |

Variant is auto-selected based on JD keywords (ML keywords → ml, React/Vue/frontend → appdev, else → se).

## Tailoring Flow (CLI-only)

```bash
# 1. Structure the JD, score it, and write a tailoring prompt
jobflow apply "https://..." --paste -t "SWE" -c "Stripe" -l "SF"

# 2. Feed the generated prompt to Claude to get tailored LaTeX sections

# 3. Merge sections with the base preamble + education and compile the PDF
jobflow save --dir data/output/Stripe_SWE_2026-04-08
```

1. `jobflow apply` scrapes/structures the JD, auto-selects a resume variant, scores the role, and writes a tailoring prompt to the output directory.
2. You paste the prompt into Claude and copy the returned tailored `.tex` sections back into the output directory.
3. `jobflow save --dir <path>` extracts the base-resume preamble + education, merges in the tailored sections, and runs pdflatex (twice for cross-refs) to produce the final PDF.

### Refinement

To revise, edit the tailored sections in the output directory and re-run `jobflow save --dir <path>` to recompile.

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

## Dependencies

- **Claude CLI** — must be installed and authenticated (`claude` command)
- **pdflatex** — from MacTeX/TexLive (`pdflatex` command)
- Tailoring runs locally via the CLI, not on the Render deployment
