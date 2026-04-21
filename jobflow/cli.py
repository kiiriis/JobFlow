"""Typer CLI — the command-line interface for all JobFlow operations.

Commands:
    jobflow scan      — Scan job boards (LinkedIn, Lever, Greenhouse, Ashby, GitHub)
    jobflow apply     — Process a single job posting (scrape → filter → tailor prompt)
    jobflow save      — Merge tailored LaTeX sections + compile PDF
    jobflow process   — Process jobs from scan results interactively
    jobflow list      — View tracked applications
    jobflow status    — Update application status
    jobflow init      — First-time setup (create config, directories, CSV)
    jobflow web       — Launch the Flask dashboard

The scan command is the most-used: it's called by GitHub Actions hourly
(`jobflow scan --platform linkedin --new --save --hours 4`) and by the
web dashboard's "Scan Now" button.

The apply → save workflow is for manual job processing:
    1. `jobflow apply <url> --paste` → scrape JD, score, build tailoring prompt
    2. User feeds the prompt to Claude to get tailored LaTeX
    3. `jobflow save --dir <path>` → merge preamble + tailored sections, compile PDF
"""

import os
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
from .scanner import (
    deduplicate_results,
    load_seen_jobs,
    print_scan_results,
    save_seen_jobs,
    scan_all_api_boards,
)
from .tracker import append_job, init_csv, print_jobs, update_status, STATUSES

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
                variant=result.resume_variant, source="manual",
                notes=result.reason,
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

    # Save metadata (score, variant, etc.) for the save command
    import json
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "company": company,
        "role": title,
        "url": url,
        "location": location,
        "score": score,
        "variant": variant,
        "date": today,
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

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
    variant: str = typer.Option("", "--variant", "-v", help="Resume variant override (reads from metadata.json if empty)"),
    sections: str = typer.Option("", "--sections", "-s", help="Path to file with tailored LaTeX sections"),
):
    """Save tailored resume sections into the final .tex and compile to PDF."""
    import json
    config = load_config()

    # Read metadata from apply step
    meta_path = Path(dir) / "metadata.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())

    # Use metadata values, with CLI overrides
    if not variant:
        variant = meta.get("variant", "se")
    company_name = meta.get("company", "")
    role_name = meta.get("role", "")
    job_url = meta.get("url", "")
    score = meta.get("score", -1)

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
    tex_path = save_tailored_resume(full_tex, Path(dir), company_name, role_name)
    console.print(f"[green]Saved:[/green] {tex_path}")

    # Compile PDF with Company_Role.pdf naming
    from .tailor import _sanitize_filename
    pdf_name = ""
    if company_name and role_name:
        pdf_name = f"{_sanitize_filename(company_name)}_{_sanitize_filename(role_name)}.pdf"

    if check_pdflatex():
        console.print("\n[bold cyan]Compiling PDF...[/bold cyan]")
        pdf_path = compile_pdf(tex_path, final_name=pdf_name)
        if pdf_path:
            console.print(f"[green]PDF:[/green] {pdf_path}")
        else:
            console.print("[yellow]PDF compilation failed. Check the .tex file.[/yellow]")
    else:
        console.print("[yellow]pdflatex not found. Install MacTeX to compile PDFs.[/yellow]")
        pdf_path = None

    added = append_job(
        config["csv_path"],
        company_name, role_name, job_url,
        score=score, status="Pending",
        resume_path=str(tex_path),
        variant=variant, source="manual",
    )

    console.print(Panel(
        f"Company: {company_name}\n"
        f"Role: {role_name}\n"
        f"Resume: {tex_path}\n"
        f"PDF: {pdf_path or 'N/A'}\n"
        f"Tracked: {'Yes' if added else 'Already exists (duplicate)'}",
        title="Application Saved",
        border_style="green",
    ))


@app.command()
def scan(
    platform: Optional[str] = typer.Option(None, "--platform", "-p", help="Scan specific platform: lever, greenhouse, ashby, linkedin, github"),
    hours: int = typer.Option(0, "--hours", "-h", help="Only show jobs posted within this many hours (0 = all)"),
    new_only: bool = typer.Option(False, "--new", "-n", help="Only show jobs not seen in previous scans"),
    save_results: bool = typer.Option(True, "--save/--no-save", help="Save relevant jobs to scan_results.json"),
):
    """Scan all job boards for new grad positions (APIs + LinkedIn + GitHub repos)."""
    config = load_config()
    platforms = [platform] if platform else None

    console.print("[bold]Scanning job boards for new grad / entry-level positions...[/bold]")
    results = scan_all_api_boards(config, platforms, max_age_hours=hours)

    # Deduplication
    if new_only:
        seen = load_seen_jobs(config)
        before = len(results)
        results, seen = deduplicate_results(results, seen)
        save_seen_jobs(config, seen)
        skipped_dupes = before - len(results)
        if skipped_dupes:
            console.print(f"\n[dim]Skipped {skipped_dupes} previously seen jobs[/dim]")

    print_scan_results(results)

    # Save relevant jobs to a JSON file for easy processing
    apply_jobs = [(j, r) for j, r in results if r.should_apply]
    if apply_jobs and save_results:
        import json
        new_entries = []
        for job, filt in sorted(apply_jobs, key=lambda x: x[1].score, reverse=True):
            new_entries.append({
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "url": job.url,
                "score": filt.score,
                "score_pct": filt.score_pct,
                "level": filt.level,
                "min_exp": filt.min_exp,
                "max_exp": filt.max_exp,
                "competition": filt.competition,
                "variant": filt.resume_variant,
                "reason": filt.reason,
                "reject_reason": filt.reject_reason,
                "description_preview": job.description,
                "date_posted": getattr(job, "date_posted", ""),
                "source": getattr(job, "source", "linkedin"),
            })

        results_path = config["output_dir"] / "scan_results.json"
        config["output_dir"].mkdir(parents=True, exist_ok=True)

        # Merge with existing results (append, dedup by URL)
        existing = []
        if results_path.exists():
            try:
                existing = json.loads(results_path.read_text())
            except (json.JSONDecodeError, ValueError):
                existing = []

        seen_urls = {e.get("url") for e in existing if e.get("url")}
        for entry in new_entries:
            if entry["url"] and entry["url"] not in seen_urls:
                existing.append(entry)
                seen_urls.add(entry["url"])

        # Cap at 500 entries, keep highest-scored
        existing.sort(key=lambda e: int(e.get("score", 0) or 0), reverse=True)
        existing = existing[:500]

        # Re-index
        for i, entry in enumerate(existing, 1):
            entry["index"] = i

        results_path.write_text(json.dumps(existing, indent=2))
        console.print(f"\n[green]Results saved to:[/green] {results_path} ({len(existing)} total jobs)")

        # AI scoring with Llama 4 Scout via Groq (only if GROQ_API_KEY is set)
        if os.environ.get("GROQ_API_KEY"):
            from .ai_scorer import ai_score_jobs
            console.print("\n[bold cyan]Running AI relevance scoring (Llama 4 Scout via Groq)...[/bold cyan]")
            ai_score_jobs(new_entries, config.get("_root"))
            # Update the entries in the saved results
            url_to_ai = {e["url"]: e for e in new_entries if e.get("ai_score")}
            for entry in existing:
                ai = url_to_ai.get(entry.get("url"))
                if ai:
                    entry["ai_score"] = ai["ai_score"]
                    entry["ai_reason"] = ai["ai_reason"]
            results_path.write_text(json.dumps(existing, indent=2))
            scored_count = len(url_to_ai)
            console.print(f"[green]AI scored {scored_count} new jobs[/green]")
        else:
            console.print("\n[dim]AI scoring skipped (GROQ_API_KEY not set)[/dim]")

        console.print(
            "\n[yellow]Next steps:[/yellow]\n"
            "  1. Review the results above\n"
            "  2. Pick a job by number and run:\n"
            "     jobflow process <number>\n"
            "  Or process all relevant jobs:\n"
            "     jobflow process --all"
        )


@app.command()
def process(
    index: int = typer.Argument(0, help="Job index from scan results (0 = show list)"),
    all_jobs: bool = typer.Option(False, "--all", "-a", help="Process all relevant jobs"),
):
    """Process a job from scan results: filter, tailor, and track."""
    import json
    config = load_config()
    results_path = config["output_dir"] / "scan_results.json"

    if not results_path.exists():
        console.print("[red]No scan results found. Run 'jobflow scan' first.[/red]")
        raise typer.Exit(1)

    with open(results_path) as f:
        jobs = json.load(f)

    if not jobs:
        console.print("[yellow]No relevant jobs in scan results.[/yellow]")
        return

    if index == 0 and not all_jobs:
        # Print the list
        from rich.table import Table
        table = Table(title="Scan Results")
        table.add_column("#", style="dim", width=4)
        table.add_column("Company", style="cyan")
        table.add_column("Role")
        table.add_column("Score", justify="right")
        table.add_column("Variant")
        for j in jobs:
            table.add_row(str(j["index"]), j["company"], j["title"], str(j["score"]), j["variant"])
        console.print(table)
        console.print("\n[yellow]Run: jobflow process <number>[/yellow]")
        return

    to_process = jobs if all_jobs else [j for j in jobs if j["index"] == index]
    if not to_process:
        console.print(f"[red]Job #{index} not found in scan results.[/red]")
        raise typer.Exit(1)

    for entry in to_process:
        console.print(f"\n[bold cyan]Processing: {entry['company']} — {entry['title']}[/bold cyan]")
        console.print(f"  URL: {entry['url']}")
        console.print(f"  Score: {entry['score']} | Variant: {entry['variant']}")

        # We need the full job description — the scan only stored a preview
        # Claude needs to scrape the full JD via Playwright MCP
        console.print(
            f"\n[yellow]To process this job, Claude should:[/yellow]\n"
            f"  1. Use Playwright MCP to open: {entry['url']}\n"
            f"  2. Extract the full job description\n"
            f"  3. Run:\n"
            f"     jobflow apply \"{entry['url']}\" "
            f"--title \"{entry['title']}\" "
            f"--company \"{entry['company']}\" "
            f"--location \"{entry['location']}\" "
            f"--variant {entry['variant']} --paste"
        )


@app.command(name="list")
def list_jobs(
    status: Optional[str] = typer.Option("", "--status", "-s", help=f"Filter by status: {', '.join(STATUSES)}"),
):
    """Show all tracked job applications."""
    config = load_config()
    print_jobs(config["csv_path"], status_filter=status)


@app.command()
def status(
    index: int = typer.Argument(..., help="Job row number (from 'jobflow list')"),
    new_status: str = typer.Argument(..., help=f"New status: {', '.join(STATUSES)}"),
    notes: str = typer.Option("", "--notes", "-n", help="Add a note (e.g., 'Phone screen scheduled')"),
):
    """Update the status of a tracked application."""
    config = load_config()
    update_status(config["csv_path"], index, new_status, notes)


@app.command()
def init():
    """Initialize JobFlow: create config, directories, and CSV if needed."""
    config_dir = Path("config")
    config_path = config_dir / "config.yaml"
    config_dir.mkdir(exist_ok=True)

    if not config_path.exists():
        config_path.write_text(
            "resumes:\n"
            "  se: resumes/base/SE.tex\n"
            "  ml: resumes/base/ML.tex\n"
            "  appdev: resumes/base/AppDev.tex\n"
            "\n"
            "output_dir: data/output\n"
            "csv_path: data/applications.csv\n"
            "job_boards: config/job_boards.json\n"
            "resume_prompt: resumes/prompt.md\n"
        )
        console.print(f"[green]Created:[/green] {config_path}")
    else:
        console.print(f"[yellow]Config already exists:[/yellow] {config_path}")

    # Create directories
    for d in ["resumes/base", "data/output", "config"]:
        Path(d).mkdir(parents=True, exist_ok=True)

    config = load_config()
    init_csv(config["csv_path"])
    console.print(f"[green]CSV ready:[/green] {config['csv_path']}")
    console.print(f"[green]Output dir:[/green] {config['output_dir']}")

    if not check_pdflatex():
        console.print("[yellow]Warning: pdflatex not found. Install MacTeX for PDF compilation.[/yellow]")

    console.print("\n[bold green]JobFlow initialized![/bold green]")


@app.command()
def web(
    port: int = typer.Option(8080, "--port", "-p", help="Port to run the web dashboard on"),
):
    """Launch the JobFlow web dashboard."""
    from .web import create_app
    console.print(f"\n[bold green]Starting JobFlow Dashboard at http://localhost:{port}[/bold green]\n")
    webapp = create_app()
    webapp.run(host="0.0.0.0", port=port, debug=True)


if __name__ == "__main__":
    app()
