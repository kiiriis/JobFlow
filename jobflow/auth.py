"""Google OAuth (OpenID Connect) login for JobFlow's multi-user mode.

Auth is OPTIONAL and config-gated:

  * When GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset, the app runs in
    single-user mode — every request is the operator (db.DEFAULT_USER_ID) and no
    login is required. This is the local-dev / JSON-store path; nothing changes
    for a self-hosted single user.
  * When configured, users sign in with Google. Only allow-listed emails
    (ALLOWED_EMAILS, plus JOBFLOW_OPERATOR_EMAIL) may enter. The operator's email
    is bound to the migrated single-user data (user_id=1); everyone else gets a
    fresh account, backfilled from the shared pool in the background.

Environment variables:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  — OAuth client (enables auth)
  OAUTH_REDIRECT_URI                      — optional explicit callback URL
  JOBFLOW_OPERATOR_EMAIL                  — your email -> bound to user_id=1
  ALLOWED_EMAILS                          — comma-separated allowlist
  JOBFLOW_SECRET_KEY                      — Flask session signing key
"""

import os
import threading

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint, g, jsonify, redirect, render_template, request, session, url_for,
)

from .db import DEFAULT_USER_ID

oauth = OAuth()
bp = Blueprint("auth", __name__, url_prefix="/auth")

# Endpoints reachable without a session (login flow, health, static assets).
_PUBLIC_PREFIXES = ("/auth/", "/static/")
_PUBLIC_PATHS = {"/health"}


def auth_enabled() -> bool:
    """True when Google OAuth is configured (multi-user mode)."""
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def operator_email() -> str:
    return os.environ.get("JOBFLOW_OPERATOR_EMAIL", "").strip().lower()


def allowed_emails() -> set[str]:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    op = operator_email()
    if op:
        emails.add(op)
    return emails


def is_email_allowed(email: str) -> bool:
    """Allowlist check. An empty allowlist means open (documented; set
    ALLOWED_EMAILS to restrict)."""
    allow = allowed_emails()
    return (not allow) or (email.lower() in allow)


def init_oauth(app) -> None:
    """Register the Flask session key and the Google OAuth client (if enabled)."""
    app.secret_key = (
        os.environ.get("JOBFLOW_SECRET_KEY")
        or os.environ.get("SECRET_KEY")
        or "dev-insecure-key-set-JOBFLOW_SECRET_KEY"
    )
    oauth.init_app(app)
    if auth_enabled():
        oauth.register(
            name="google",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )


_db_ready = False


def _ensure_db():
    """Lazily init the DB the first time auth needs it (login is rare)."""
    global _db_ready
    from . import db
    if not _db_ready:
        db.init_db()
        _db_ready = True
    return db


def current_user():
    return {
        "id": session.get("user_id"),
        "email": session.get("email", ""),
        "name": session.get("name", ""),
        "picture": session.get("picture", ""),
    }


def resolve_request_user():
    """before_request hook: set g.user_id / g.user and enforce login.

    Returns a response to short-circuit (redirect/401) or None to proceed.
    """
    if not auth_enabled():
        g.user_id = DEFAULT_USER_ID
        g.user = {"id": DEFAULT_USER_ID, "email": "", "name": "Operator", "picture": ""}
        return None

    path = request.path
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        g.user_id = session.get("user_id")
        g.user = current_user()
        return None

    uid = session.get("user_id")
    if not uid:
        if path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("auth.login"))
    g.user_id = uid
    g.user = current_user()
    return None


# ── Routes ──────────────────────────────────────────────────────────────────

@bp.route("/login")
def login():
    if not auth_enabled():
        return redirect("/")
    if session.get("user_id"):
        return redirect("/")
    return render_template(
        "login.html",
        error=request.args.get("error", ""),
        email=request.args.get("email", ""),
    )


@bp.route("/google")
def google_login():
    if not auth_enabled():
        return redirect("/")
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI") or url_for("auth.callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route("/google/callback")
def callback():
    if not auth_enabled():
        return redirect("/")
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        return redirect(url_for("auth.login", error="oauth_failed"))

    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    if not email:
        return redirect(url_for("auth.login", error="no_email"))
    if info.get("email_verified") is False:
        return redirect(url_for("auth.login", error="unverified"))
    if not is_email_allowed(email):
        return render_template("login.html", error="not_allowed", email=email), 403

    db = _ensure_db()
    sub = info["sub"]
    name = info.get("name", "")
    picture = info.get("picture", "")

    if operator_email() and email == operator_email():
        user = db.bind_operator(sub, email, name, picture)
    else:
        user = db.get_or_create_user(sub, email, name, picture)
        if user.get("created"):
            # New user: backfill their feed from the shared pool off the request.
            threading.Thread(target=db.backfill_user, args=(user["id"],), daemon=True).start()

    session.permanent = True
    session["user_id"] = user["id"]
    session["email"] = email
    session["name"] = name
    session["picture"] = picture
    return redirect("/")


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login") if auth_enabled() else "/")
