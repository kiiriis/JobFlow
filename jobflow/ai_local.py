"""Local signed-in CLI scoring — drives the `claude` or `codex` CLI over a
subprocess (no API key). Shared by the batch script (scripts/ai_score_local.py)
and the `jobflow score` client so a friend's local machine scores identically to
the operator's.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .ai_prompt import BATCH_PROMPT, build_jobs_block, parse_scores

CLI_TIMEOUT = 300


def score_batch_with_claude(batch, profile: str):
    """Score a batch using one signed-in `claude -p` (headless) call."""
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=build_jobs_block(batch))
    if not shutil.which("claude"):
        print("  Batch error: claude CLI not found on PATH")
        return None
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--allowedTools", ""],
            input=prompt, capture_output=True, text=True, timeout=CLI_TIMEOUT,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"  Batch error: claude exited with code {result.returncode}: {err[:500]}")
            return None
        return parse_scores(result.stdout)
    except Exception as e:
        print(f"  Batch error: {e}")
        return None


def score_batch_with_codex(batch, profile: str, cwd: str | None = None):
    """Score a batch using one signed-in `codex exec` call."""
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=build_jobs_block(batch))
    if not shutil.which("codex"):
        print("  Batch error: codex CLI not found on PATH")
        return None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="jobflow_codex_score_", suffix=".txt", delete=False,
        ) as tmp:
            output_path = Path(tmp.name)
        result = subprocess.run(
            [
                "codex", "exec",
                "--cd", str(cwd or os.getcwd()),
                "--sandbox", "read-only",
                "--ignore-rules",
                "--output-last-message", str(output_path),
                "-",
            ],
            input=prompt, capture_output=True, text=True, timeout=CLI_TIMEOUT,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"  Batch error: codex exited with code {result.returncode}: {err[:500]}")
            return None
        text = ""
        if output_path and output_path.exists():
            text = output_path.read_text(errors="replace").strip()
        if not text:
            text = result.stdout
        return parse_scores(text)
    except Exception as e:
        print(f"  Batch error: {e}")
        return None
    finally:
        if output_path:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass


def score_batch(batch, profile: str, engine: str, cwd: str | None = None):
    """Dispatch a batch to the selected local CLI engine."""
    if engine == "codex":
        return score_batch_with_codex(batch, profile, cwd=cwd)
    return score_batch_with_claude(batch, profile)
