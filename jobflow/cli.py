from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from .config import load_config
from .filter import evaluate_job
from .latex import check_pdflatex, compile_pdf
from .models import JobPosting
from .scraper import parse_job_text, save_job_description
from .tailor import (
    build_tailor_prompt,
    extract_preamble_and_education,
    load_base_resume,
    load_master_prompt,
    make_output_dirname,
    merge_resume,
    save_tailored_resume,
)
from .tracker import append_job, init_csv, print_jobs

app = typer.Typer(name="jobflow", help="Tailor your resume for job postings.")
console = Console()


@app.command()
def apply(
    url: str = typer.Argument("", help="Job posting URL"),
    paste: bool = typer.Option(False, "--paste", "-p", help="Paste job description manually"),
    title: str = typer.Option("", "--title", "-t", help="Job title"),
    company: str = typer.Option("", "--company", "-c", help="Company name"),
    location: str = typer.Option("", "--location", "-l", help="Job location"),
    variant: Optional[str] = typer.Option(None, "--variant", "-v", help="Resume variant: se, ml, appdev"),
    skip_filter: bool = typer.Option(False, "--no-filter", help="Skip the filter step"),
):
    """Process a job posting: scrape, filter, tailor resume, and track."""
    config = load_config()

    # Get job description
    if paste:
        console.print("[bold]Paste the job description below (press Ctrl+D when done):[/bold]")
        import sys
        description = sys.stdin.read()
        if not url:
            url = "manual-paste"
    elif url:
        console.print(f"\n[bold cyan]Step 1: Scrape job posting[/bold cyan]")
        console.print(f"URL: {url}")
        console.print(
            "\n[yellow]Use Playwright MCP to scrape this URL, then call:[/yellow]\n"
            f"  jobflow apply \"{url}\" --title \"<title>\" --company \"<company>\" --location \"<location>\"\n"
            "\n[yellow]Or provide the scraped text with --paste[/yellow]"
        )
        return
    else:
        console.print("[red]Provide a URL or use --paste[/red]")
        raise typer.Exit(1)

    if not title or not company:
        console.print("[red]--title and --company are required[/red]")
        raise typer.Exit(1)

    job = parse_job_text(description if paste else "", url, title, company, location)

    # Filter step
    if not skip_filter:
        console.print(f"\n[bold cyan]Step 2: Filter[/bold cyan]")
        result = evaluate_job(job)
        console.print(f"  Score: {result.score}/100")
        console.print(f"  Should apply: {'Yes' if result.should_apply else 'No'}")
        console.print(f"  Reason: {result.reason}")
        console.print(f"  Recommended variant: {result.resume_variant}")

        if not result.should_apply:
            append_job(
                config["csv_path"], company, title, url,
                result.score, "Skipped",
            )
            console.print("\n[red]Job filtered out. Added to tracker as 'Skipped'.[/red]")
            return

        if variant is None:
            variant = result.resume_variant
        score = result.score
    else:
        score = -1
        if variant is None:
            variant = "se"

    # Prepare output directory
    today = date.today().isoformat()
    dirname = make_output_dirname(company, title, today)
    output_dir = config["output_dir"] / dirname

    # Save job description
    save_job_description(job, output_dir)

    # Load base resume and master prompt
    console.print(f"\n[bold cyan]Step 3: Tailor resume[/bold cyan]")
    console.print(f"  Using variant: {variant}")
    base_tex = load_base_resume(variant, config)
    master_prompt = load_master_prompt(config)

    # Build the tailoring prompt
    prompt = build_tailor_prompt(job, base_tex, master_prompt)
    prompt_path = output_dir / "tailor_prompt.txt"
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt)

    console.print(f"\n[bold green]Tailoring prompt saved to:[/bold green] {prompt_path}")
    console.print(
        "\n[yellow]Now Claude should:[/yellow]\n"
        "  1. Read the prompt file above\n"
        "  2. Generate the tailored Experience, Projects, and Skills sections\n"
        "  3. Run: jobflow save --dir \"" + str(output_dir) + "\" --variant " + variant
    )


@app.command()
def save(
    dir: Path = typer.Option(..., "--dir", "-d", help="Output directory with tailor_prompt.txt"),
    variant: str = typer.Option("se", "--variant", "-v", help="Resume variant used"),
    sections: str = typer.Option("", "--sections", "-s", help="Path to file with tailored LaTeX sections"),
):
    """Save tailored resume sections into the final .tex and compile to PDF."""
    config = load_config()

    if sections:
        tailored = Path(sections).read_text()
    else:
        console.print("[bold]Paste the tailored LaTeX sections below (Ctrl+D when done):[/bold]")
        import sys
        tailored = sys.stdin.read()

    # Load base resume to get preamble + education
    base_tex = load_base_resume(variant, config)
    preamble = extract_preamble_and_education(base_tex)

    # Merge
    full_tex = merge_resume(preamble, tailored)
    tex_path = save_tailored_resume(full_tex, Path(dir))
    console.print(f"[green]Saved:[/green] {tex_path}")

    # Compile PDF
    if check_pdflatex():
        console.print("\n[bold cyan]Compiling PDF...[/bold cyan]")
        pdf_path = compile_pdf(tex_path)
        if pdf_path:
            console.print(f"[green]PDF:[/green] {pdf_path}")
        else:
            console.print("[yellow]PDF compilation failed. Check the .tex file.[/yellow]")
    else:
        console.print("[yellow]pdflatex not found. Install MacTeX to compile PDFs.[/yellow]")
        pdf_path = None

    # Track in CSV
    # Parse company/role/date from directory name
    parts = Path(dir).name.rsplit("_", 1)
    date_str = parts[-1] if len(parts) > 1 else ""
    name_parts = parts[0].rsplit("_", 1) if len(parts) > 1 else [Path(dir).name, ""]

    # Try to read job_description.txt for metadata
    jd_path = Path(dir) / "job_description.txt"
    company_name = ""
    role_name = ""
    job_url = ""
    if jd_path.exists():
        for line in jd_path.read_text().split("\n")[:4]:
            if line.startswith("Company: "):
                company_name = line[9:].strip()
            elif line.startswith("Role: "):
                role_name = line[6:].strip()
            elif line.startswith("URL: "):
                job_url = line[5:].strip()

    append_job(
        config["csv_path"],
        company_name, role_name, job_url,
        score=-1, status="Pending",
        resume_path=str(tex_path),
    )

    console.print(Panel(
        f"Company: {company_name}\n"
        f"Role: {role_name}\n"
        f"Resume: {tex_path}\n"
        f"PDF: {pdf_path or 'N/A'}\n"
        f"Tracked in: {config['csv_path']}",
        title="Application Saved",
        border_style="green",
    ))


@app.command(name="list")
def list_jobs():
    """Show all tracked job applications."""
    config = load_config()
    print_jobs(config["csv_path"])


@app.command()
def init():
    """Initialize JobFlow: create config and CSV if needed."""
    config_path = Path("config.yaml")
    if not config_path.exists():
        config_path.write_text(
            "resumes:\n"
            "  se: CurrentResume/KrishMakadiaSE.tex\n"
            "  ml: CurrentResume/KrishMakadiaML.tex\n"
            "  appdev: CurrentResume/KrishMakadiaAppDev.tex\n"
            "\n"
            "output_dir: output\n"
            "csv_path: jobLists.csv\n"
            "job_boards: JobBoards_Links.json\n"
            "resume_prompt: ResumeEditingPrompt.md\n"
        )
        console.print(f"[green]Created:[/green] {config_path}")
    else:
        console.print(f"[yellow]Config already exists:[/yellow] {config_path}")

    config = load_config()
    config["output_dir"].mkdir(parents=True, exist_ok=True)
    init_csv(config["csv_path"])
    console.print(f"[green]CSV ready:[/green] {config['csv_path']}")
    console.print(f"[green]Output dir:[/green] {config['output_dir']}")

    if not check_pdflatex():
        console.print("[yellow]Warning: pdflatex not found. Install MacTeX for PDF compilation.[/yellow]")

    console.print("\n[bold green]JobFlow initialized![/bold green]")


if __name__ == "__main__":
    app()
