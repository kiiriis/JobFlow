import re
from pathlib import Path

from .models import JobPosting


def load_base_resume(variant: str, config: dict) -> str:
    """Load the base resume .tex for the given variant."""
    path = config["resumes"].get(variant)
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"Base resume not found for variant '{variant}': {path}")
    return Path(path).read_text()


def load_master_prompt(config: dict) -> str:
    """Load the master resume editing prompt."""
    path = config["resume_prompt"]
    if not Path(path).exists():
        raise FileNotFoundError(f"Resume prompt not found: {path}")
    return Path(path).read_text()


def extract_preamble_and_education(tex: str) -> str:
    """Extract everything from \\documentclass through end of Education section."""
    # Find the end of Education section (before Experience starts)
    match = re.search(r"(%-+EXPERIENCE-+|\\section\{Experience\})", tex)
    if match:
        return tex[:match.start()].rstrip() + "\n\n"
    # Fallback: return everything before \section{Experience}
    idx = tex.find(r"\section{Experience}")
    if idx != -1:
        return tex[:idx].rstrip() + "\n\n"
    raise ValueError("Could not find Experience section boundary in base resume")


def merge_resume(preamble: str, tailored_sections: str) -> str:
    """Merge preamble+education with tailored Experience/Projects/Skills sections."""
    # Clean up the tailored sections - remove any markdown fencing
    cleaned = tailored_sections.strip()
    cleaned = re.sub(r"^```(?:latex)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    # Remove any "Company Name + Role" header line if present
    # (the master prompt outputs this before the LaTeX)
    lines = cleaned.split("\n")
    start_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("%-") or stripped.startswith("\\section{") or stripped.startswith("\\resumeSubHeadingListStart"):
            start_idx = i
            break
        # Skip lines that look like headers (e.g., "Stripe — SDE 1, New Grad")
        if stripped and not stripped.startswith("\\") and not stripped.startswith("%") and "—" in stripped:
            continue
        if stripped.startswith("## ") or stripped.startswith("# "):
            continue
        if stripped.startswith("\\"):
            start_idx = i
            break

    cleaned = "\n".join(lines[start_idx:]).strip()

    # Ensure \end{document} is present
    if r"\end{document}" not in cleaned:
        cleaned += "\n\n\n\\end{document}\n"

    return preamble + cleaned


def build_tailor_prompt(job: JobPosting, base_tex: str, master_prompt: str) -> str:
    """Assemble the full tailoring prompt for Claude."""
    return (
        f"{master_prompt}\n\n"
        f"---\n\n"
        f"## Job Description\n\n"
        f"**Company:** {job.company}\n"
        f"**Role:** {job.title}\n"
        f"**Location:** {job.location}\n\n"
        f"{job.description}\n\n"
        f"---\n\n"
        f"## My Current Resume (LaTeX)\n\n"
        f"```latex\n{base_tex}\n```"
    )


def save_tailored_resume(tex_content: str, output_dir: Path) -> Path:
    """Save the tailored .tex file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    tex_path = output_dir / "tailored_resume.tex"
    tex_path.write_text(tex_content)
    return tex_path


def make_output_dirname(company: str, role: str, date_str: str) -> str:
    """Create a sanitized output directory name."""
    name = f"{company}_{role}_{date_str}"
    # Replace spaces and special chars
    name = re.sub(r"[/\\:*?\"<>|]", "", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")
