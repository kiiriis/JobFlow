import shutil
import subprocess
from pathlib import Path


def check_pdflatex() -> bool:
    """Check if pdflatex is available on PATH."""
    return shutil.which("pdflatex") is not None


def compile_pdf(tex_path: Path) -> Path | None:
    """Compile a .tex file to PDF using pdflatex. Returns PDF path or None."""
    if not check_pdflatex():
        return None

    output_dir = tex_path.parent

    # Run pdflatex twice for proper cross-references
    for _ in range(2):
        result = subprocess.run(
            [
                "pdflatex",
                "-interaction=nonstopmode",
                "-output-directory",
                str(output_dir),
                str(tex_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(output_dir),
        )

    pdf_path = tex_path.with_suffix(".pdf")
    if pdf_path.exists():
        # Clean up auxiliary files
        for ext in [".aux", ".log", ".out"]:
            aux = tex_path.with_suffix(ext)
            if aux.exists():
                aux.unlink()
        return pdf_path

    # If compilation failed, print the error
    if result.returncode != 0:
        log_path = tex_path.with_suffix(".log")
        if log_path.exists():
            log_content = log_path.read_text()
            # Find error lines
            errors = [l for l in log_content.split("\n") if l.startswith("!")]
            if errors:
                print(f"LaTeX errors:\n" + "\n".join(errors[:5]))

    return None
