"""JobFlow Web Dashboard — Flask app factory with HTMX-powered routes.

This is the main web interface deployed on Render.com. It provides:

Pages:
    /           → Redirects to /linkedin
    /linkedin   → Main job feed with filtering, sorting, time buckets, status management
    /scan       → Trigger job scans from the browser
    /tailor     → Paste a JD → Claude tailors your resume → download PDF
    /boards     → ATS platform scanner (placeholder)
    /health     → JSON health check for uptime monitoring

Architecture:
    - HTMX for dynamic updates (no full-page reloads)
    - Partial templates in _partials/ return HTML fragments
    - Background threads for long-running operations (scan, tailor)
    - In-memory session store for tailor sessions (max 20, auto-evict)

Data flow:
    1. /api/scan/trigger → starts background scan thread
    2. _run_scan() calls scan_all_api_boards() + filter + dedup
    3. Results saved to scan_results.json and merged into linkedin_jobs.json
    4. /api/linkedin/jobs reads linkedin_jobs.json and returns filtered HTML table
    5. Response headers carry count metadata (X-Counts, X-Level-Counts, etc.)
       so the JS can update sidebar chips without a separate request

Auto-pull (local only):
    A background thread runs `git pull --rebase` every hour to pick up CI scan
    results, then re-merges into the store. Disabled on Render (data updates
    via redeploy triggered by CI push).

Tailor sessions:
    Each tailoring request creates an in-memory session with a UUID. The session
    tracks: JD text, variant, Claude output, PDF path, feedback history, and
    cancellation state. Sessions auto-evict when exceeding MAX_SESSIONS (20).
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, make_response

from ..config import load_config
from ..filter import (
    select_variant,
    has_match,
    count_matches,
    _has_phrase,
    DISQUALIFYING_PHRASES,
    SENIOR_DESC_SIGNALS,
    ENTRY_LEVEL_SIGNALS,
)
from ..latex import compile_pdf, get_page_count
from ..tailor import load_base_resume, load_master_prompt, save_tailored_resume
from ..tracker import list_jobs, append_job, STATUSES
from ..linkedin_store import (
    load_store, save_store, merge_scan_results, prune_old_jobs,
    update_job_status, get_filtered_jobs, get_status_counts,
    get_level_counts, get_filtered_counts, get_search_terms,
    get_time_counts,
    backfill_job,
    LINKEDIN_STATUSES,
    USE_DB,
)


# Shared scan state for the background scan thread.
# Since Flask runs in a single process (gunicorn -w 1), this dict is safe
# for thread communication without locks — only one scan runs at a time.
scan_state = {
    "running": False,
    "results": None,
    "error": None,
    "total": 0,
    "relevant": 0,
    "skipped": 0,
}

# In-memory store for tailor sessions.
# Keyed by UUID session_id. Each session tracks the full state of a resume
# tailoring request, including the Claude subprocess, output .tex, and PDF.
tailor_sessions = {}

MAX_SESSIONS = 20  # Evict oldest sessions beyond this


def _evict_old_sessions():
    """Remove oldest completed sessions when we exceed MAX_SESSIONS."""
    if len(tailor_sessions) <= MAX_SESSIONS:
        return
    # Sort by creation time (embedded in UUID v4 isn't ordered, so use _created_at)
    completed = [
        (sid, s) for sid, s in tailor_sessions.items()
        if s["status"] in ("done", "error")
    ]
    completed.sort(key=lambda x: x[1].get("_created_at", 0))
    # Remove oldest completed until we're at limit
    to_remove = len(tailor_sessions) - MAX_SESSIONS
    for sid, _ in completed[:to_remove]:
        tailor_sessions.pop(sid, None)


def _cancel_session(session):
    """Cancel a running session and kill its subprocess if possible."""
    session["_cancelled"] = True
    proc = session.get("_process")
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _is_cancelled(session):
    """Check if session has been cancelled."""
    return session.get("_cancelled", False)


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
    def index():
        return redirect("/linkedin")

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.route("/scan")
    def scan_page():
        return render_template("scan.html")

    # ── API Routes ───────────────────────────────────────────────

    @app.route("/api/scan/trigger", methods=["POST"])
    def api_trigger_scan():
        if scan_state["running"]:
            return jsonify({"error": "Scan already running"}), 409

        data = request.form or request.get_json(silent=True) or {}
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
        data = request.form or request.get_json(silent=True) or {}
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
        if _has_phrase(jd_lower, DISQUALIFYING_PHRASES):
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error="This role does not sponsor visas or requires U.S. citizenship / security clearance. Skipping.",
            )

        senior_count = count_matches(jd_lower, SENIOR_DESC_SIGNALS)
        entry_count = count_matches(jd_lower, ENTRY_LEVEL_SIGNALS)
        if senior_count > 0 and entry_count == 0 and senior_count >= 3:
            return render_template(
                "_partials/tailor_status.html",
                status="error",
                error="This role requires senior-level experience and has no entry-level signals. Skipping.",
            )

        # Cancel any currently running sessions to avoid orphaned Claude processes
        for sid, s in list(tailor_sessions.items()):
            if s["status"] == "running":
                _cancel_session(s)
                s["status"] = "error"
                s["error"] = "Cancelled — new tailor request started."

        _evict_old_sessions()

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
            "_created_at": time.time(),
            "_cancelled": False,
            "_process": None,
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

        if session["status"] == "running":
            return render_template(
                "_partials/tailor_status.html",
                status="running",
                session_id=session_id,
                iteration=session["iteration"],
            )

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

    @app.route("/api/tailor/cancel/<session_id>", methods=["POST"])
    def api_tailor_cancel(session_id):
        session = tailor_sessions.get(session_id)
        if session and session["status"] == "running":
            _cancel_session(session)
            session["status"] = "error"
            session["error"] = "Cancelled by user."
        return render_template(
            "_partials/tailor_status.html",
            status="error",
            error="Cancelled by user.",
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

    # ── LinkedIn Scanner Routes ──────────────────────────────────

    linkedin_store_path = config["_root"] / "data" / "ci" / "linkedin_jobs.json"
    scan_results_path = config["_root"] / "data" / "ci" / "scan_results.json"

    # Initialize DB if available
    if USE_DB:
        from ..db import init_db as _init_db
        from .. import db as _db
        _init_db()

    def _do_linkedin_merge():
        """Merge scan_results.json into linkedin_jobs.json (JSON backend only)."""
        if USE_DB:
            return  # DB backend handles merges directly
        if not scan_results_path.exists():
            return
        try:
            scan_data = json.loads(scan_results_path.read_text())
        except (json.JSONDecodeError, ValueError):
            return
        store = load_store(linkedin_store_path)
        store = merge_scan_results(store, scan_data)
        store = prune_old_jobs(store)
        for key in store.get("jobs", {}):
            store["jobs"][key] = backfill_job(store["jobs"][key])
        save_store(linkedin_store_path, store)

    # Merge once on startup (JSON backend only)
    if not USE_DB:
        _do_linkedin_merge()

    # Auto-pull thread (JSON backend, local dev only)
    def _auto_pull_loop():
        while True:
            time.sleep(3600)
            try:
                subprocess.run(
                    ["git", "pull", "--rebase", "origin", "main"],
                    cwd=str(config["_root"]),
                    capture_output=True,
                    timeout=30,
                )
                _do_linkedin_merge()
            except Exception:
                pass

    if not USE_DB and not os.environ.get("RENDER"):
        pull_thread = threading.Thread(target=_auto_pull_loop, daemon=True)
        pull_thread.start()

    @app.route("/boards")
    def boards_page():
        return render_template("boards.html")

    @app.route("/linkedin")
    def linkedin_page():
        if USE_DB:
            counts = _db.get_status_counts()
            level_counts = _db.get_level_counts()
            search_terms = _db.get_search_terms()
            time_counts = _db.get_time_counts()
            last_updated = _db.get_last_updated()
        else:
            store = load_store(linkedin_store_path)
            counts = get_status_counts(store)
            level_counts = get_level_counts(store)
            search_terms = get_search_terms(store)
            time_counts = get_time_counts(store)
            last_updated = store.get("last_updated", "")
        return render_template(
            "linkedin.html",
            statuses=LINKEDIN_STATUSES,
            counts=counts,
            level_counts=level_counts,
            search_terms=search_terms,
            time_counts=time_counts,
            last_updated=last_updated,
        )

    @app.route("/api/linkedin/jobs")
    def api_linkedin_jobs():
        status_filter = request.args.get("status", "")
        level_filter = request.args.get("level", "")
        query = request.args.get("q", "")
        search_term = request.args.get("search_term", "")
        time_range = request.args.get("time", "")
        bucket_filter = request.args.get("bucket", "")
        sort_col = request.args.get("sort", "last_seen")
        sort_dir = request.args.get("dir", "desc")
        tz_offset = int(request.args.get("tz", "0") or "0")  # minutes from UTC

        if USE_DB:
            jobs = _db.get_filtered_jobs(
                status=status_filter, level=level_filter, query=query,
                search_term=search_term, time_range=time_range,
                bucket_filter=bucket_filter, sort_col=sort_col,
                sort_dir=sort_dir, tz_offset=tz_offset,
            )
            fc = _db.get_filtered_counts(
                time_range=time_range, bucket_filter=bucket_filter,
                tz_offset=tz_offset, query=query, search_term=search_term,
            )
            time_counts = _db.get_time_counts(tz_offset=tz_offset)
        else:
            store = load_store(linkedin_store_path)
            jobs = get_filtered_jobs(
                store, status=status_filter, level=level_filter, query=query,
                search_term=search_term, time_range=time_range,
                bucket_filter=bucket_filter, sort_col=sort_col,
                sort_dir=sort_dir, tz_offset=tz_offset,
            )
            fc = get_filtered_counts(
                store, time_range=time_range, bucket_filter=bucket_filter,
                tz_offset=tz_offset, query=query, search_term=search_term,
            )
            time_counts = get_time_counts(store, tz_offset=tz_offset)

        counts = fc["status"]
        level_counts = fc["level"]
        resp = make_response(render_template(
            "_partials/linkedin_tbody.html",
            jobs=jobs,
            statuses=LINKEDIN_STATUSES,
            counts=counts,
        ))
        resp.headers["X-Counts"] = json.dumps(counts)
        resp.headers["X-Level-Counts"] = json.dumps(level_counts)
        resp.headers["X-Time-Counts"] = json.dumps({
            "this_hour": time_counts["this_hour"],
            "today": time_counts["today"],
            "yesterday": time_counts["yesterday"],
        })
        resp.headers["X-Buckets"] = json.dumps(time_counts.get("buckets", []))
        resp.headers["X-Total"] = str(len(jobs))
        return resp

    @app.route("/api/linkedin/jobs/<path:key>/status", methods=["PATCH"])
    def api_linkedin_status(key):
        data = request.form or request.get_json(silent=True) or {}
        new_status = data.get("status", "")
        if USE_DB:
            _db.update_job_status(key, new_status)
            job = _db.get_job(key) or {"_key": key}
        else:
            store = load_store(linkedin_store_path)
            if update_job_status(store, key, new_status):
                save_store(linkedin_store_path, store)
            job = store.get("jobs", {}).get(key, {})
            job["_key"] = key
        return render_template(
            "_partials/linkedin_row.html",
            job=job,
            statuses=LINKEDIN_STATUSES,
        )

    @app.route("/api/linkedin/refresh", methods=["POST"])
    def api_linkedin_refresh():
        if USE_DB:
            # DB is always fresh — just prune expired jobs
            try:
                _db.prune_expired_jobs()
                return '<span style="color:#5cb85c;">Refreshed!</span>'
            except Exception as e:
                return f'<span style="color:#d9534f;">Error: {e}</span>'
        try:
            subprocess.run(
                ["git", "pull", "--rebase", "origin", "main"],
                cwd=str(config["_root"]),
                capture_output=True,
                timeout=30,
            )
            _do_linkedin_merge()
            return '<span style="color:#5cb85c;">Refreshed!</span>'
        except Exception as e:
            return f'<span style="color:#d9534f;">Error: {e}</span>'

    return app


def _run_scan(config, platforms, hours, new_only):
    """Run scan in background thread.

    Called when the user clicks "Start Scan" on the /scan page. Runs the full
    scan pipeline: fetch jobs → score → dedup → save → merge into linkedin store.

    All jobs passing hard rejects (company blocklist, senior titles, non-US)
    are saved to scan_results.json and merged into linkedin_jobs.json.
    AI scoring is the real quality gate — algo score is informational only.
    """
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
                "score_pct": filt.score_pct,
                "level": filt.level,
                "min_exp": filt.min_exp,
                "max_exp": filt.max_exp,
                "competition": filt.competition,
                "variant": filt.resume_variant,
                "reason": filt.reason,
                "description_preview": job.description[:2000] if job.description else "",
                "date_posted": getattr(job, "date_posted", ""),
            })

        results_path = config["output_dir"] / "scan_results.json"
        config["output_dir"].mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(output, indent=2))
        scan_state["results"] = output

        # Merge into store so new jobs appear in the feed
        try:
            if USE_DB:
                from ..db import merge_scan_results as db_merge
                scan_state["new_jobs"] = db_merge(output)
            else:
                store_path = config["_root"] / "data" / "ci" / "linkedin_jobs.json"
                store = load_store(store_path)
                store = merge_scan_results(store, output)
                store = prune_old_jobs(store)
                for key in store.get("jobs", {}):
                    store["jobs"][key] = backfill_job(store["jobs"][key])
                save_store(store_path, store)
                scan_state["new_jobs"] = len(output)
        except Exception:
            pass

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
    """Build prompt that asks Claude to output a COMPLETE tailored .tex file.

    Unlike the CLI's build_tailor_prompt() (which asks for sections only),
    this prompt asks Claude to output the entire file from \\documentclass to
    \\end{document}. This avoids the preamble merge step and gives Claude
    full control over the output.

    The META line requirement (first line of output) lets us extract company/role
    metadata without a separate API call.
    """
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
        f"5. The output must be a valid, compilable LaTeX file\n"
        f"6. The resume MUST fit on exactly ONE PAGE. Keep bullets concise (1-2 lines max). Do NOT modify margins or font sizes.\n\n"
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


def _run_claude(session, prompt):
    """Run Claude CLI and return (stdout, stderr, returncode). Handles cancellation."""
    proc = subprocess.Popen(
        ["claude", "-p", prompt, "--model", session["model"], "--effort", session["effort"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    session["_process"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        session["_process"] = None

    if _is_cancelled(session):
        return None, None, -1

    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        proc.returncode,
    )


def _run_tailor(session_id, config):
    """Run initial resume tailoring in background thread.

    Pipeline: load base .tex → build prompt → run Claude CLI → parse META line
    → extract .tex → save → compile PDF → auto-condense if >1 page.

    The Claude CLI is run as a subprocess (`claude -p <prompt> --model <model>`),
    not via API, because the web server may not have an API key but the user
    has Claude Code installed.
    """
    session = tailor_sessions[session_id]
    try:
        jd_text = session["jd_text"]
        variant = session["variant"]

        base_tex = load_base_resume(variant, config)
        master_prompt = load_master_prompt(config)

        prompt = _build_tailor_prompt(jd_text, base_tex, master_prompt)

        stdout, stderr, returncode = _run_claude(session, prompt)

        if _is_cancelled(session):
            return

        if returncode != 0:
            session["status"] = "error"
            session["error"] = stderr or f"Claude CLI exited with code {returncode}"
            return

        output = (stdout or "").strip()
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

        # Auto-fix: if PDF exceeds 1 page, ask Claude to condense
        if pdf_path and get_page_count(Path(pdf_path)) > 1:
            _auto_condense(session_id, config)
            return

        session["status"] = "done"

    except subprocess.TimeoutExpired:
        session["status"] = "error"
        session["error"] = "Claude CLI timed out (>180s). Try again."
    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)


def _auto_condense(session_id, config):
    """Automatically condense a resume that spilled to 2+ pages.

    Called when _run_tailor() detects the compiled PDF has >1 page. Sends
    the current .tex back to Claude with instructions to shorten bullet points
    and reduce skill counts while preserving all sections and entries.
    """
    session = tailor_sessions[session_id]
    session["iteration"] += 1
    try:
        if _is_cancelled(session):
            return

        previous_tex = session["current_tex"]
        master_prompt = load_master_prompt(config)

        prompt = (
            f"{master_prompt}\n\n"
            f"---\n\n"
            f"## CRITICAL: The resume below compiled to MORE THAN ONE PAGE. Fix it.\n\n"
            f"You MUST condense it to fit on exactly ONE page. Strategies:\n"
            f"- Shorten bullet points — cut filler words, merge clauses, aim for 1 line each\n"
            f"- Reduce skills per category to 4-5 items max\n"
            f"- Shorten project descriptions\n"
            f"- Do NOT remove sections, experience entries, or projects\n"
            f"- Do NOT change margins, font size, or spacing commands\n\n"
            f"Output the COMPLETE condensed .tex file from \\documentclass to \\end{{document}}.\n"
            f"No markdown fences. No commentary.\n\n"
            f"{previous_tex}"
        )

        stdout, stderr, returncode = _run_claude(session, prompt)

        if _is_cancelled(session):
            return

        if returncode != 0:
            session["status"] = "error"
            session["error"] = f"Auto-condense failed: {stderr or 'unknown error'}"
            return

        output = (stdout or "").strip()
        if not output:
            session["status"] = "error"
            session["error"] = "Auto-condense returned empty output."
            return

        full_tex = _extract_tex_from_output(output)
        session["current_tex"] = full_tex
        session["conversation"].append({"role": "assistant", "content": full_tex})

        output_dir = session["output_dir"]
        tex_path = save_tailored_resume(full_tex, output_dir, session["company"], session["role"])
        pdf_path = compile_pdf(tex_path)
        session["pdf_path"] = str(pdf_path) if pdf_path else None
        session["status"] = "done"

    except subprocess.TimeoutExpired:
        session["status"] = "error"
        session["error"] = "Auto-condense timed out (>180s)."
    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)


def _run_tailor_refine(session_id, config, feedback):
    """Run resume refinement with user feedback in background thread.

    Called when the user submits feedback on a completed tailor session.
    Uses the PREVIOUS .tex output (not the original base) as the starting
    point, so edits are cumulative across iterations. The user can refine
    as many times as needed.
    """
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
            f"5. The resume MUST fit on exactly ONE PAGE. Keep bullets concise (1-2 lines max). Do NOT modify margins or font sizes.\n"
        )

        stdout, stderr, returncode = _run_claude(session, prompt)

        if _is_cancelled(session):
            return

        if returncode != 0:
            session["status"] = "error"
            session["error"] = stderr or f"Claude CLI exited with code {returncode}"
            return

        output = (stdout or "").strip()
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

        # Auto-fix: if PDF exceeds 1 page, ask Claude to condense
        if pdf_path and get_page_count(Path(pdf_path)) > 1:
            _auto_condense(session_id, config)
            return

        session["status"] = "done"

    except subprocess.TimeoutExpired:
        session["status"] = "error"
        session["error"] = "Claude CLI timed out (>180s). Try again."
    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)
