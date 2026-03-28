import csv
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

HEADERS = ["company", "role", "link", "score", "status", "resume_path", "date"]


def init_csv(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)


def append_job(
    path: Path,
    company: str,
    role: str,
    link: str,
    score: int,
    status: str = "Pending",
    resume_path: str = "",
) -> None:
    init_csv(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([company, role, link, score, status, resume_path, date.today().isoformat()])


def list_jobs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def print_jobs(path: Path) -> None:
    jobs = list_jobs(path)
    if not jobs:
        Console().print("[yellow]No jobs tracked yet.[/yellow]")
        return

    table = Table(title="Tracked Applications")
    for header in HEADERS:
        table.add_column(header.capitalize(), style="cyan" if header == "company" else None)

    for job in jobs:
        status = job.get("status", "")
        style = "green" if status == "Applied" else "red" if status == "Skipped" else None
        table.add_row(*[job.get(h, "") for h in HEADERS], style=style)

    Console().print(table)
