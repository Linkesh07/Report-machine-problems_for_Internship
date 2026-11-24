"""Pages for workers: dashboard, report a machine problem, my reports."""
import os
import uuid

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from PIL import Image, ImageOps, UnidentifiedImageError

from . import db, login_manager
from .duplicate_detection import assign_issue
from .models import SEVERITIES, STATUSES, Machine, Report

bp = Blueprint("worker", __name__, url_prefix="/worker")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


@bp.before_request
def guard():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if current_user.role != "worker":
        abort(403)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def save_photo(file_storage):
    """Check the photo is a real image, shrink it and save it under a random name."""
    if not file_storage or not file_storage.filename:
        return None
    name = file_storage.filename
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("The photo must be a PNG, JPG, GIF or WEBP picture.")
    try:
        image = Image.open(file_storage.stream)
        image.load()
        image = ImageOps.exif_transpose(image)
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((1600, 1600))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("That file is not a valid picture.")
    filename = f"{uuid.uuid4().hex}.jpg"
    image.save(os.path.join(current_app.config["UPLOAD_FOLDER"], filename), "JPEG", quality=85)
    return filename


@bp.route("/")
def dashboard():
    mine = Report.query.filter_by(worker_id=current_user.id)
    stats = {
        "total": mine.count(),
        "pending": mine.filter_by(status="Pending").count(),
        "progress": mine.filter_by(status="In Progress").count(),
        "resolved": mine.filter_by(status="Resolved").count(),
    }
    recent = mine.order_by(Report.created_at.desc()).limit(5).all()
    return render_template("worker/dashboard.html", stats=stats, recent=recent)


@bp.route("/report/new", methods=["GET", "POST"])
def new_report():
    machines = Machine.query.order_by(Machine.name).all()
    form = {}
    if request.method == "POST":
        form = request.form
        errors = []
        machine = db.session.get(Machine, _to_int(form.get("machine_id")) or 0)
        title = form.get("title", "").strip()
        message = form.get("message", "").strip()
        severity = form.get("severity", "")

        if machine is None:
            errors.append("Please choose the machine.")
        if not 5 <= len(title) <= 120:
            errors.append("The problem title must be 5 to 120 characters.")
        if not 15 <= len(message) <= 2000:
            errors.append("Please describe the problem in 15 to 2000 characters.")
        if severity not in SEVERITIES:
            errors.append("Please choose how serious the problem is.")

        photo_name = None
        if not errors:
            try:
                photo_name = save_photo(request.files.get("photo"))
            except ValueError as exc:
                errors.append(str(exc))

        if errors:
            for error in errors:
                flash(error, "error")
        else:
            report = Report(
                worker_id=current_user.id,
                machine_id=machine.id,
                title=title,
                message=message,
                severity=severity,
                photo_path=photo_name,
                status="Pending",
            )
            db.session.add(report)
            db.session.flush()
            assign_issue(report)  # compare with earlier reports
            db.session.commit()
            flash(f"Report #{report.id} was sent to the manager. Thank you!", "success")
            return redirect(url_for("worker.report_detail", rid=report.id))

    return render_template("worker/new_report.html", machines=machines, form=form)


@bp.route("/reports")
def reports():
    status = request.args.get("status", "")
    query = Report.query.filter_by(worker_id=current_user.id)
    if status in STATUSES:
        query = query.filter_by(status=status)
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Report.created_at.desc()).paginate(page=page, per_page=10, error_out=False)
    return render_template(
        "worker/reports.html", pagination=pagination, status=status,
        args={"status": status} if status in STATUSES else {},
    )


@bp.route("/reports/<int:rid>")
def report_detail(rid):
    report = Report.query.filter_by(id=rid, worker_id=current_user.id).first_or_404()
    return render_template("worker/report_detail.html", r=report)
