"""Bike Factory Machine Issue Portal - application factory."""
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def create_app(test_config=None):
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///yamaha_portal.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        UPLOAD_FOLDER=os.path.join(BASE_DIR, "uploads"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # 5 MB per request (photo limit)
        # The user is logged out after this many idle minutes.
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=int(os.environ.get("SESSION_MINUTES", "30"))),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from . import models  # noqa: F401  (registers the tables)
    from .auth import bp as auth_bp
    from .main import bp as main_bp
    from .manager import bp as manager_bp
    from .worker import bp as worker_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(worker_bp)
    app.register_blueprint(manager_bp)

    with app.app_context():
        db.create_all()

    _register_hooks(app)
    return app


def _register_hooks(app):
    from .models import Report, SEVERITIES, STATUSES

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/manager/api"):
            return jsonify(error="login required"), 401
        flash("Please log in to continue.", "info")
        target = "auth.manager_login" if request.path.startswith("/manager") else "auth.worker_login"
        return redirect(url_for(target, next=request.full_path.rstrip("?")))

    @app.context_processor
    def inject_globals():
        unseen = 0
        if current_user.is_authenticated and current_user.role == "manager":
            unseen = Report.query.filter_by(seen_by_manager=False).count()
        return dict(
            unseen_count=unseen,
            SEVERITIES=SEVERITIES,
            STATUSES=STATUSES,
            app_name="Bike Factory Machine Issue Portal",
        )

    @app.template_filter("dt")
    def fmt_dt(value):
        return value.strftime("%d %b %Y, %I:%M %p") if value else "—"

    @app.template_filter("ago")
    def fmt_ago(value):
        if not value:
            return "—"
        seconds = int((datetime.now() - value).total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60} min ago"
        if seconds < 86400:
            return f"{seconds // 3600} hr ago"
        if seconds < 86400 * 7:
            return f"{seconds // 86400} day(s) ago"
        return value.strftime("%d %b %Y")

    @app.template_filter("pct")
    def fmt_pct(value):
        return f"{round((value or 0) * 100)}%"

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        # Stop the browser "Back" button from showing private pages after logout.
        if current_user.is_authenticated and not request.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def error_page(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(403)
    def forbidden(_e):
        return error_page(403, "Access Denied", "You do not have permission to open this page.")

    @app.errorhandler(404)
    def not_found(_e):
        return error_page(404, "Page not found", "The page you are looking for does not exist.")

    @app.errorhandler(405)
    def not_allowed(_e):
        return error_page(405, "Not allowed", "That action is not allowed on this page.")

    @app.errorhandler(413)
    def too_large(_e):
        return error_page(413, "File too large", "The photo must be smaller than 5 MB.")

    @app.errorhandler(500)
    def server_error(_e):
        return error_page(500, "Something went wrong", "An unexpected error happened. Please try again.")

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        flash("Your page expired. Please try again.", "error")
        ref = request.referrer
        if ref and urlparse(ref).netloc == request.host:
            return redirect(ref)
        return redirect(url_for("main.home"))
