"""Home page and protected photo downloads."""
from flask import Blueprint, abort, current_app, redirect, render_template, send_from_directory
from flask_login import current_user, login_required

from .auth import dashboard_for
from .models import Report

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))
    return render_template("home.html")


@bp.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    """Photos are private: only the manager and the worker who sent it can open them."""
    report = Report.query.filter_by(photo_path=filename).first_or_404()
    if current_user.role != "manager" and report.worker_id != current_user.id:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)
