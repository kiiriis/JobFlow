"""JobFlow Web Dashboard — Flask app factory and routes."""

import json
import re
import shutil
import subprocess
import threading
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file, abort, redirect

from ..config import load_config
from ..filter import (
    select_variant,
    has_match,
    DISQUALIFYING_PHRASES,
    SENIOR_PHRASES,
    ENTRY_LEVEL_SIGNALS,
)
from ..latex import compile_pdf
from ..tailor import load_base_resume, load_master_prompt, save_tailored_resume
from ..tracker import list_jobs, update_status, append_job, STATUSES, HEADERS


# Shared scan state for background thread
scan_state = {
    "running": False,
    "results": None,
    "error": None,
    "total": 0,
    "relevant": 0,
    "skipped": 0,
}

# In-memory store for tailor sessions
tailor_sessions = {}


def create_app():
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )

    config = load_config()
    app.config["JOBFLOW"] = config

    # ── Page Routes ──────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        return redirect("/applications")

    @app.route("/applications")
    def applications():
        return render_template("applications.html", statuses=STATUSES)

    @app.route("/application/<int:index>")
    def application_detail(index):
        jobs = list_jobs(config["csv_path"])
        if index < 1 or index > len(jobs):
            abort(404)
        job = jobs[index - 1]

        # Find output folder and files
        resume_path = job.get("resume_path", "")
        output_base = config["output_dir"].resolve()
        job_dir = None
        files = []
        file_infos = []
        jd_text = ""
        if resume_path:
            job_dir = (Path(config["_root"]) / Path(resume_path).parent).resolve()
            if job_dir.exists():
                files = sorted(job_dir.iterdir())
                for f in files:
                    try:
                        rel = f.resolve().relative_to(output_base)
                    except ValueError:
                        rel = f.name
                    file_infos.append({
                        "name": f.name,
                        "path": str(rel),
                        "size_kb": round(f.stat().st_size / 1024, 1),
                        "suffix": f.suffix,
                    })
            jd_path = job_dir / "job_description.txt" if job_dir else None
            if jd_path and jd_path.exists():
                jd_text = jd_path.read_text()

        return render_template(
            "application_detail.html",
            job=job,
            index=index,
            file_infos=file_infos,
            jd_text=jd_text,
            statuses=STATUSES,
        )

    @app.route("/scan")
    def scan_page():
        return render_template("scan.html")

    # ── API Routes ───────────────────────────────────────────────

    @app.route("/api/applications")
    def api_applications():
        jobs = list_jobs(config["csv_path"])
        status_filter = request.args.get("status", "")
        query = request.args.get("q", "").lower()
        sort_by = request.args.get("sort", "date_found")
        sort_dir = request.args.get("dir", "desc")

        # Add original index before filtering
        indexed = [{"_index": i + 1, **j} for i, j in enumerate(jobs)]

        if status_filter:
            indexed = [j for j in indexed if j.get("status") == status_filter]
        if query:
            indexed = [j for j in indexed if
                       query in j.get("company", "").lower() or
                       query in j.get("role", "").lower() or
                       query in j.get("notes", "").lower()]

        # Sort
        reverse = sort_dir == "desc"
        if sort_by == "score":
            indexed.sort(key=lambda j: int(j.get("score", 0) or 0), reverse=reverse)
        elif sort_by == "company":
            indexed.sort(key=lambda j: j.get("company", "").lower(), reverse=reverse)
        else:
            indexed.sort(key=lambda j: j.get("date_found", ""), reverse=reverse)

        return render_template(
            "_partials/applications_tbody.html",
            jobs=indexed,
            statuses=STATUSES,
        )

    @app.route("/api/applications/<int:index>/status", methods=["PATCH"])
    def api_update_status(index):
        data = request.form or request.json or {}
        new_status = data.get("status", "")
        notes = data.get("notes", "")
        if not new_status:
            return "Missing status", 400

        update_status(config["csv_path"], index, new_status, notes)

        # Return updated row
        jobs = list_jobs(config["csv_path"])
        if index < 1 or index > len(jobs):
            return "Not found", 404
        job = {"_index": index, **jobs[index - 1]}
        return render_template("_partials/application_row.html", job=job, statuses=STATUSES)

    @app.route("/api/applications/<int:index>/notes", methods=["PATCH"])
    def api_update_notes(index):
        data = request.form or request.json or {}
        notes = data.get("notes", "")

        jobs = list_jobs(config["csv_path"])
        if index < 1 or index > len(jobs):
            return "Not found", 404

        # Direct CSV update for notes
        import csv
        rows = jobs
        rows[index - 1]["notes"] = notes
        with open(config["csv_path"], "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        return f'<span class="notes-text" hx-get="/api/applications/{index}/notes/edit" hx-trigger="click" hx-swap="outerHTML">{notes or "Click to add..."}</span>'

    @app.route("/api/applications/<int:index>/notes/edit")
    def api_edit_notes(index):
        jobs = list_jobs(config["csv_path"])
        if index < 1 or index > len(jobs):
            return "Not found", 404
        current = jobs[index - 1].get("notes", "")
        return (
            f'<input type="text" name="notes" value="{current}" '
            f'hx-patch="/api/applications/{index}/notes" '
            f'hx-trigger="blur, keydown[key==\'Enter\']" '
            f'hx-swap="outerHTML" '
            f'style="margin:0;padding:2px 6px;font-size:0.85em" autofocus>'
        )

    @app.route("/api/scan/trigger", methods=["POST"])
    def api_trigger_scan():
        if scan_state["running"]:
            return jsonify({"error": "Scan already running"}), 409

        data = request.form or request.json or {}
        platform = data.get("platform", "") or None
        hours = int(data.get("hours", 0) or 0)
        new_only = data.get("new_only") in ("true", "on", True, "1")

        platforms = [platform] if platform else None

        thread = threading.Thread(
            target=_run_scan,
            args=(config, platforms, hours, new_only),
            daemon=True,
        )
        thread.start()
        return render_template("_partials/scan_status.html", running=True)

    @app.route("/api/scan/status")
    def api_scan_status():
        if scan_state["running"]:
            return render_template("_partials/scan_status.html", running=True)

        results = scan_state.get("results")
        error = scan_state.get("error")
        if error:
            return render_template("_partials/scan_status.html", running=False, error=error)

        # Load scan results from file
        results_path = config["output_dir"] / "scan_results.json"
        scan_results = []
        if results_path.exists():
            scan_results = json.loads(results_path.read_text())

        return render_template(
            "_partials/scan_status.html",
            running=False,
            results=scan_results,
            total=scan_state.get("total", 0),
            relevant=scan_state.get("relevant", 0),
            skipped=scan_state.get("skipped", 0),
        )

    @app.route("/api/scan/track", methods=["POST"])
    def api_track_job():
        data = request.form or request.json or {}
        added = append_job(
            config["csv_path"],
            company=data.get("company", ""),
            role=data.get("role", ""),
            link=data.get("url", ""),
            score=int(data.get("score", 0) or 0),
            status="Pending",
            variant=data.get("variant", "se"),
            source=data.get("source", "scan"),
        )
        if added:
            return '<span class="badge-tracked">Tracked ✓</span>'
        return '<span class="badge-duplicate">Already tracked</span>'

    @app.route("/api/stats")
    def api_stats():
        jobs = list_jobs(config["csv_path"])
        counts = Counter(j.get("status", "Unknown") for j in jobs)

        # Weekly activity (last 8 weeks)
        weeks = {}
        today = date.today()
        for j in jobs:
            d = j.get("date_found", "")
            if d:
                try:
                    job_date = date.fromisoformat(d)
                    week_start = job_date - timedelta(days=job_date.weekday())
                    key = week_start.isoformat()
                    weeks[key] = weeks.get(key, 0) + 1
                except ValueError:
                    pass

        # Last 8 weeks
        week_labels = []
        week_values = []
        for i in range(7, -1, -1):
            ws = today - timedelta(weeks=i, days=today.weekday())
            key = ws.isoformat()
            week_labels.append(ws.strftime("%b %d"))
            week_values.append(weeks.get(key, 0))

        return jsonify({
            "status_counts": dict(counts),
            "week_labels": week_labels,
            "week_values": week_values,
            "total": len(jobs),
        })

    @app.route("/api/file/<path:filepath>")
    def api_serve_file(filepath):
        output_dir = config["output_dir"].resolve()
        requested = (output_dir / filepath).resolve()
        if not str(requested).startswith(str(output_dir)):
            abort(403)
        if not requested.exists():
            abort(404)
        # Set appropriate MIME type
        suffix = requested.suffix.lower()
        mimetype = {
            ".pdf": "application/pdf",
            ".tex": "text/plain",
            ".txt": "text/plain",
            ".json": "application/json",
        }.get(suffix, "application/octet-stream")
        return send_file(requested, mimetype=mimetype)

    # ── Tailor Routes ────────────────────────────────────────────

    @app.route("/tailor")
    def tailor_page():
        return render_template("tailor.html")

    @app.route("/api/tailor/generate", methods=["POST"])
    def api_tailor_generate():
        jd_text = (request.form.get("jd_text") or "").strip()
        if not jd_text:
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error="Please paste a job description.",
            )

        if not shutil.which("claude"):
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error="Claude CLI not found. Make sure 'claude' is installed and on your PATH.",
            )

        # Fast pre-filter: reject non-new-grad / no-sponsorship JDs instantly
        jd_lower = jd_text.lower()
        if has_match(jd_lower, DISQUALIFYING_PHRASES):
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error="This role does not sponsor visas or requires U.S. citizenship / security clearance. Skipping.",
            )

        if has_match(jd_lower, SENIOR_PHRASES) and not has_match(jd_lower, ENTRY_LEVEL_SIGNALS):
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error="This role requires senior-level experience (3+ years) and has no new-grad / entry-level signals. Skipping.",
            )

        session_id = str(uuid.uuid4())
        output_dir = config["output_dir"] / f"tailor_{session_id[:8]}"
        variant = select_variant(jd_text)
        model = request.form.get("model", "sonnet")
        effort = request.form.get("effort", "low")

        tailor_sessions[session_id] = {
            "id": session_id,
            "status": "running",
            "jd_text": jd_text,
            "variant": variant,
            "company": "Unknown",
            "role": "Unknown",
            "location": "",
            "conversation": [],
            "feedback_history": [],
            "current_tex": "",
            "pdf_path": None,
            "error": None,
            "output_dir": output_dir,
            "iteration": 1,
            "model": model,
            "effort": effort,
        }

        thread = threading.Thread(
            target=_run_tailor,
            args=(session_id, config),
            daemon=True,
        )
        thread.start()

        return render_template(
            "_partials/tailor_status.html",
            status="running",
            session_id=session_id,
            iteration=1,
        )

    @app.route("/api/tailor/status/<session_id>")
    def api_tailor_status(session_id):
        session = tailor_sessions.get(session_id)
        if not session:
            abort(404)

        if session["status"] == "running":
            return render_template(
                "_partials/tailor_status.html",
                status="running",
                session_id=session_id,
                iteration=session["iteration"],
            )

        if session["status"] == "error":
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error=session["error"],
            )

        # Done
        return render_template(
            "_partials/tailor_status.html",
            status="done",
            session_id=session_id,
            company=session["company"],
            role=session["role"],
            location=session["location"],
            variant=session["variant"],
            iteration=session["iteration"],
            pdf_path=session["pdf_path"],
            tex_content=session["current_tex"],
            model=session.get("model", "sonnet"),
            effort=session.get("effort", "low"),
        )

    @app.route("/api/tailor/refine/<session_id>", methods=["POST"])
    def api_tailor_refine(session_id):
        session = tailor_sessions.get(session_id)
        if not session:
            abort(404)

        feedback = (request.form.get("feedback") or "").strip()
        if not feedback:
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error="Please provide feedback for regeneration.",
            )

        session["feedback_history"].append(feedback)
        session["iteration"] += 1
        session["status"] = "running"
        session["pdf_path"] = None
        session["error"] = None

        thread = threading.Thread(
            target=_run_tailor_refine,
            args=(session_id, config, feedback),
            daemon=True,
        )
        thread.start()

        return render_template(
            "_partials/tailor_status.html",
            status="running",
            session_id=session_id,
            iteration=session["iteration"],
        )

    @app.route("/api/tailor/pdf/<session_id>")
    def api_tailor_pdf(session_id):
        session = tailor_sessions.get(session_id)
        if not session or not session.get("pdf_path"):
            abort(404)
        pdf = Path(session["pdf_path"]).resolve()
        if not pdf.exists():
            abort(404)
        return send_file(pdf, mimetype="application/pdf")

    @app.route("/api/tailor/download/<session_id>")
    def api_tailor_download(session_id):
        session = tailor_sessions.get(session_id)
        if not session or not session.get("pdf_path"):
            abort(404)
        pdf = Path(session["pdf_path"]).resolve()
        if not pdf.exists():
            abort(404)
        download_name = f"{session['company']}_{session['role']}.pdf".replace(" ", "_")
        return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=download_name)

    return app


def _run_scan(config, platforms, hours, new_only):
    """Run scan in background thread."""
    from ..scanner import scan_all_api_boards, load_seen_jobs, save_seen_jobs, deduplicate_results

    scan_state["running"] = True
    scan_state["error"] = None
    scan_state["results"] = None

    try:
        results = scan_all_api_boards(config, platforms, max_age_hours=hours)

        if new_only:
            seen = load_seen_jobs(config)
            results, seen = deduplicate_results(results, seen)
            save_seen_jobs(config, seen)

        apply_jobs = [(j, r) for j, r in results if r.should_apply]
        skip_jobs = [(j, r) for j, r in results if not r.should_apply]

        scan_state["total"] = len(results)
        scan_state["relevant"] = len(apply_jobs)
        scan_state["skipped"] = len(skip_jobs)

        # Save results
        output = []
        for i, (job, filt) in enumerate(sorted(apply_jobs, key=lambda x: x[1].score, reverse=True), 1):
            output.append({
                "index": i,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "url": job.url,
                "score": filt.score,
                "variant": filt.resume_variant,
                "reason": filt.reason,
            })

        results_path = config["output_dir"] / "scan_results.json"
        config["output_dir"].mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(output, indent=2))
        scan_state["results"] = output

    except Exception as e:
        scan_state["error"] = str(e)
    finally:
        scan_state["running"] = False


def _parse_meta_line(output: str) -> tuple[str, str, str, str]:
    """Parse META line from Claude output. Returns (company, role, location, cleaned_output)."""
    lines = output.strip().split("\n")
    company, role, location = "Unknown", "Unknown", ""

    for i, line in enumerate(lines):
        meta_match = re.match(
            r"META:\s*company=(.+?)\s*\|\s*role=(.+?)\s*\|\s*location=(.+)",
            line.strip(),
            re.IGNORECASE,
        )
        if meta_match:
            company = meta_match.group(1).strip()
            role = meta_match.group(2).strip()
            location = meta_match.group(3).strip()
            # Remove the META line from output
            cleaned = "\n".join(lines[:i] + lines[i + 1:]).strip()
            return company, role, location, cleaned

    return company, role, location, output


def _build_tailor_prompt(jd_text, base_tex, master_prompt):
    """Build prompt that asks Claude to edit the .tex file in-place."""
    return (
        f"{master_prompt}\n\n"
        f"---\n\n"
        f"## Job Description\n\n"
        f"{jd_text}\n\n"
        f"---\n\n"
        f"## My Current Resume (LaTeX)\n\n"
        f"Below is my complete LaTeX resume file. Edit the CONTENT (bullet points, skills, "
        f"project descriptions) to tailor it for the job description above. "
        f"Keep ALL LaTeX commands, structure, formatting, and macros EXACTLY as they are. "
        f"Only change the text inside the commands.\n\n"
        f"IMPORTANT OUTPUT RULES:\n"
        f"1. On the VERY FIRST LINE, output metadata: META: company=<company> | role=<role> | location=<location>\n"
        f"2. Then output the COMPLETE modified .tex file — from \\documentclass to \\end{{document}}\n"
        f"3. Do NOT wrap the output in markdown code fences (no ```)\n"
        f"4. Do NOT add any commentary or explanation\n"
        f"5. The output must be a valid, compilable LaTeX file\n\n"
        f"{base_tex}"
    )


def _extract_tex_from_output(output):
    """Extract the complete .tex content from Claude's output, stripping any markdown."""
    # Remove markdown code fences if present
    cleaned = re.sub(r"```(?:latex)?\s*\n?", "", output)
    cleaned = cleaned.strip()

    # Find \documentclass to \end{document}
    doc_start = cleaned.find(r"\documentclass")
    doc_end = cleaned.rfind(r"\end{document}")

    if doc_start != -1 and doc_end != -1:
        return cleaned[doc_start:doc_end + len(r"\end{document}")] + "\n"

    # Fallback: return everything after META line
    lines = cleaned.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith(r"\documentclass"):
            return "\n".join(lines[i:]).strip() + "\n"

    return cleaned


def _run_tailor(session_id, config):
    """Run initial resume tailoring in background thread."""
    session = tailor_sessions[session_id]
    try:
        jd_text = session["jd_text"]
        variant = session["variant"]

        base_tex = load_base_resume(variant, config)
        master_prompt = load_master_prompt(config)

        prompt = _build_tailor_prompt(jd_text, base_tex, master_prompt)

        # Call Claude CLI
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", session["model"], "--effort", session["effort"]],
            capture_output=True,
            timeout=180,
        )

        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            session["status"] = "error"
            session["error"] = stderr or f"Claude CLI exited with code {result.returncode}"
            return

        output = stdout.strip()
        if not output:
            session["status"] = "error"
            session["error"] = "Claude returned empty output."
            return

        # Parse META line
        company, role, location, cleaned_output = _parse_meta_line(output)
        session["company"] = company
        session["role"] = role
        session["location"] = location

        # Extract the complete .tex file from output
        full_tex = _extract_tex_from_output(cleaned_output)
        session["current_tex"] = full_tex

        # Store conversation
        session["conversation"].append({"role": "assistant", "content": full_tex})

        # Save and compile
        output_dir = session["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        tex_path = save_tailored_resume(full_tex, output_dir, company, role)
        pdf_path = compile_pdf(tex_path)
        session["pdf_path"] = str(pdf_path) if pdf_path else None

        session["status"] = "done"

    except subprocess.TimeoutExpired:
        session["status"] = "error"
        session["error"] = "Claude CLI timed out (>180s). Try again."
    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)


def _run_tailor_refine(session_id, config, feedback):
    """Run resume refinement with user feedback in background thread."""
    session = tailor_sessions[session_id]
    try:
        jd_text = session["jd_text"]
        master_prompt = load_master_prompt(config)

        # Use the PREVIOUS .tex output as the base (not the original)
        previous_tex = session["current_tex"]

        prompt = (
            f"{master_prompt}\n\n"
            f"---\n\n"
            f"## Job Description\n\n"
            f"{jd_text}\n\n"
            f"---\n\n"
            f"## Current Resume (LaTeX) — needs revision\n\n"
            f"Below is the current tailored resume. The user wants changes.\n\n"
            f"{previous_tex}\n\n"
            f"---\n\n"
            f"## User Feedback\n\n"
            f"{feedback}\n\n"
            f"---\n\n"
            f"Apply the user's feedback to the resume above. "
            f"Keep ALL LaTeX commands, structure, formatting, and macros EXACTLY as they are. "
            f"Only change the text content as requested.\n\n"
            f"IMPORTANT OUTPUT RULES:\n"
            f"1. Output the COMPLETE modified .tex file — from \\documentclass to \\end{{document}}\n"
            f"2. Do NOT wrap the output in markdown code fences (no ```)\n"
            f"3. Do NOT add any commentary or explanation\n"
            f"4. The output must be a valid, compilable LaTeX file\n"
        )

        result = subprocess.run(
            ["claude", "-p", prompt, "--model", session["model"], "--effort", session["effort"]],
            capture_output=True,
            timeout=180,
        )

        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            session["status"] = "error"
            session["error"] = stderr or f"Claude CLI exited with code {result.returncode}"
            return

        output = stdout.strip()
        if not output:
            session["status"] = "error"
            session["error"] = "Claude returned empty output."
            return

        # Extract the complete .tex file
        full_tex = _extract_tex_from_output(output)
        session["current_tex"] = full_tex

        # Store in conversation
        session["conversation"].append({"role": "assistant", "content": full_tex})

        # Save and compile (overwrite previous)
        output_dir = session["output_dir"]
        tex_path = save_tailored_resume(full_tex, output_dir, session["company"], session["role"])
        pdf_path = compile_pdf(tex_path)
        session["pdf_path"] = str(pdf_path) if pdf_path else None

        session["status"] = "done"

    except subprocess.TimeoutExpired:
        session["status"] = "error"
        session["error"] = "Claude CLI timed out (>180s). Try again."
    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)
