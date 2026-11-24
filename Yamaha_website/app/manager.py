"""Pages for the manager: dashboard, reports, issues + notes, worker accounts, alerts."""
import re
from collections import Counter
from datetime import date, datetime, time, timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import case, func, or_

from . import db, login_manager
from .auth import password_problems
from .duplicate_detection import shared_keywords
from .models import SEVERITIES, STATUSES, Issue, LoginLog, Machine, ManagerNote, Report, User
from .services import (
    delete_report, delete_worker, move_report_to_issue, remove_photo_files, split_report_from_issue,
)

bp = Blueprint("manager", __name__, url_prefix="/manager")


@bp.before_request
def guard():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if current_user.role != "manager":
        abort(403)


# ---------------------------------------------------------------- helpers
def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _back(default_url):
    target = request.form.get("next", "")
    if target.startswith("/manager") and not target.startswith("//"):
        return redirect(target)
    return redirect(default_url)


def _get_report(rid):
    return db.session.get(Report, rid) or abort(404)


def _get_issue(iid):
    return db.session.get(Issue, iid) or abort(404)


def _bars(pairs):
    """[(label, count)] -> [(label, count, percent_of_max)] for the simple CSS charts."""
    biggest = max([count for _, count in pairs] + [1])
    return [(label, count, round(count * 100 / biggest)) for label, count in pairs]


# ---------------------------------------------------------------- dashboard
@bp.route("/")
def dashboard():
    stats = {
        "total": Report.query.count(),
        "new": Report.query.filter_by(match_status="new").count(),
        "duplicate": Report.query.filter_by(match_status="duplicate").count(),
        "review": Report.query.filter_by(match_status="possible").count(),
        "open_issues": Issue.query.filter(Issue.status != "Resolved").count(),
        "resolved": Issue.query.filter_by(status="Resolved").count(),
        "critical": Issue.query.filter(Issue.status != "Resolved", Issue.priority == "Critical").count(),
    }

    today = date.today()
    week = []
    for back in range(6, -1, -1):
        day = today - timedelta(days=back)
        start = datetime.combine(day, time.min)
        count = Report.query.filter(Report.created_at >= start, Report.created_at < start + timedelta(days=1)).count()
        week.append((day.strftime("%a"), count))

    by_machine = (
        db.session.query(Machine.name, func.count(Report.id))
        .join(Report, Report.machine_id == Machine.id)
        .group_by(Machine.id)
        .order_by(func.count(Report.id).desc())
        .limit(5)
        .all()
    )
    by_priority = dict(
        db.session.query(Issue.priority, func.count(Issue.id))
        .filter(Issue.status != "Resolved")
        .group_by(Issue.priority)
        .all()
    )
    priority_bars = _bars([(p, by_priority.get(p, 0)) for p in reversed(SEVERITIES)])

    needs_review = Report.query.filter_by(match_status="possible").order_by(Report.created_at.desc()).limit(5).all()
    recent = Report.query.order_by(Report.created_at.desc()).limit(8).all()
    return render_template(
        "manager/dashboard.html",
        stats=stats,
        week=_bars(week),
        machine_bars=_bars([(name, count) for name, count in by_machine]),
        priority_bars=priority_bars,
        needs_review=needs_review,
        recent=recent,
    )


# ---------------------------------------------------------------- reports list
@bp.route("/reports")
def reports():
    keys = ("q", "machine", "status", "severity", "match", "worker", "date_from", "date_to", "unseen")
    f = {k: request.args.get(k, "").strip() for k in keys}

    query = Report.query.join(User, Report.worker_id == User.id).join(Machine, Report.machine_id == Machine.id)
    if f["q"]:
        like = f"%{f['q']}%"
        query = query.filter(
            or_(
                Report.title.ilike(like), Report.message.ilike(like), User.name.ilike(like),
                User.employee_id.ilike(like), Machine.name.ilike(like),
            )
        )
    if _to_int(f["machine"]):
        query = query.filter(Report.machine_id == int(f["machine"]))
    if f["status"] in STATUSES:
        query = query.filter(Report.status == f["status"])
    if f["severity"] in SEVERITIES:
        query = query.filter(Report.severity == f["severity"])
    if f["match"] in ("new", "duplicate", "possible"):
        query = query.filter(Report.match_status == f["match"])
    if _to_int(f["worker"]):
        query = query.filter(Report.worker_id == int(f["worker"]))
    for key, op in (("date_from", "ge"), ("date_to", "lt")):
        if f[key]:
            try:
                day = datetime.strptime(f[key], "%Y-%m-%d")
            except ValueError:
                continue
            if op == "ge":
                query = query.filter(Report.created_at >= day)
            else:
                query = query.filter(Report.created_at < day + timedelta(days=1))
    if f["unseen"] == "1":
        query = query.filter(Report.seen_by_manager.is_(False))

    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Report.created_at.desc()).paginate(page=page, per_page=12, error_out=False)
    return render_template(
        "manager/reports.html",
        pagination=pagination,
        f=f,
        args={k: v for k, v in f.items() if v},
        machines=Machine.query.order_by(Machine.name).all(),
        workers=User.query.filter_by(role="worker").order_by(User.name).all(),
    )


@bp.post("/reports/mark-seen")
def mark_seen():
    Report.query.filter_by(seen_by_manager=False).update({"seen_by_manager": True})
    db.session.commit()
    flash("All new reports are marked as seen.", "success")
    return _back(url_for("manager.reports"))


# ---------------------------------------------------------------- report details + actions
@bp.route("/reports/<int:rid>")
def report_detail(rid):
    report = _get_report(rid)
    if not report.seen_by_manager:
        report.seen_by_manager = True
        db.session.commit()
    match = report.duplicate_of
    issue = report.issue
    merge_targets = (
        Issue.query.filter(Issue.status != "Resolved", Issue.id != issue.id)
        .order_by(Issue.updated_at.desc()).limit(40).all()
    )
    return render_template(
        "manager/report_detail.html",
        r=report,
        match=match,
        issue=issue,
        shared=shared_keywords(report.text, match.text) if match else [],
        siblings=[x for x in issue.reports if x.id != report.id],
        merge_targets=merge_targets,
    )


@bp.post("/reports/<int:rid>/confirm-duplicate")
def confirm_duplicate(rid):
    report = _get_report(rid)
    match = report.duplicate_of
    if match is None or match.issue is None:
        flash("There is no similar report to merge with.", "error")
    else:
        move_report_to_issue(report, match.issue)
        db.session.commit()
        flash(f"Confirmed. Report #{report.id} now belongs to issue #{match.issue.id}.", "success")
    return _back(url_for("manager.report_detail", rid=rid))


@bp.post("/reports/<int:rid>/not-duplicate")
def not_duplicate(rid):
    report = _get_report(rid)
    split_report_from_issue(report)
    db.session.commit()
    flash(f"Report #{report.id} is now a separate new issue.", "success")
    return _back(url_for("manager.report_detail", rid=rid))


@bp.post("/reports/<int:rid>/merge")
def merge_report(rid):
    report = _get_report(rid)
    target = db.session.get(Issue, _to_int(request.form.get("target_issue_id")) or 0)
    if target is None or target.id == report.issue_id:
        flash("Please choose a different issue to merge into.", "error")
    else:
        move_report_to_issue(report, target)
        db.session.commit()
        flash(f"Report #{report.id} was merged into issue #{target.id}.", "success")
        return redirect(url_for("manager.report_detail", rid=rid))
    return _back(url_for("manager.report_detail", rid=rid))


@bp.post("/reports/<int:rid>/delete")
def report_delete(rid):
    """Delete a report. Only reports the manager has already checked (seen) can be deleted."""
    report = _get_report(rid)
    if not report.seen_by_manager:
        flash("Open the report (or mark it as seen) before deleting it.", "error")
        return _back(url_for("manager.reports"))
    number = report.id
    photos = delete_report(report)
    db.session.commit()
    remove_photo_files(photos)
    flash(f"Report #{number} was deleted.", "success")
    return _back(url_for("manager.reports"))


# ---------------------------------------------------------------- issues + notes
@bp.route("/issues")
def issues():
    status = request.args.get("status", "")
    priority = request.args.get("priority", "")
    query = Issue.query
    if status in STATUSES:
        query = query.filter(Issue.status == status)
    if priority in SEVERITIES:
        query = query.filter(Issue.priority == priority)
    order_open = case((Issue.status == "Resolved", 1), else_=0)
    order_priority = case({"Critical": 0, "High": 1, "Medium": 2, "Low": 3}, value=Issue.priority)
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(order_open, order_priority, Issue.updated_at.desc()).paginate(
        page=page, per_page=12, error_out=False
    )
    args = {k: v for k, v in (("status", status), ("priority", priority)) if v}
    return render_template("manager/issues.html", pagination=pagination, status=status, priority=priority, args=args)


@bp.route("/issues/<int:iid>")
def issue_detail(iid):
    return render_template("manager/issue_detail.html", issue=_get_issue(iid))


@bp.post("/issues/<int:iid>/status")
def issue_status(iid):
    issue = _get_issue(iid)
    status = request.form.get("status", "")
    if status not in STATUSES:
        flash("Please choose a valid status.", "error")
    else:
        issue.set_status(status)
        db.session.commit()
        flash(f"Status changed to {status} for all {len(issue.reports)} report(s) in this issue.", "success")
    return _back(url_for("manager.issue_detail", iid=iid))


@bp.post("/issues/<int:iid>/priority")
def issue_priority(iid):
    issue = _get_issue(iid)
    value = request.form.get("priority", "")
    if value == "auto":
        issue.priority_locked = False
        issue.recount()
        flash(f"Priority is automatic again ({issue.priority}).", "success")
    elif value in SEVERITIES:
        issue.priority = value
        issue.priority_locked = True
        flash(f"Priority set to {value}.", "success")
    else:
        flash("Please choose a valid priority.", "error")
    db.session.commit()
    return _back(url_for("manager.issue_detail", iid=iid))


@bp.post("/issues/<int:iid>/note")
def issue_note(iid):
    issue = _get_issue(iid)
    text = request.form.get("note_text", "").strip()
    if not text:
        flash("Please write something in the note.", "error")
    elif len(text) > 2000:
        flash("The note is too long (maximum 2000 characters).", "error")
    else:
        db.session.add(ManagerNote(issue_id=issue.id, manager_id=current_user.id, note_text=text))
        issue.updated_at = datetime.now()
        db.session.commit()
        flash("Note saved.", "success")
    return _back(url_for("manager.issue_detail", iid=iid))


# ---------------------------------------------------------------- worker accounts
def _get_worker(uid):
    user = db.session.get(User, uid)
    if user is None or user.role != "worker":
        abort(404)
    return user


def _id_parts(employee_id):
    """'WRK0010' -> ('WRK', 10, 4): letters, number, number of digits. Used for natural order."""
    match = re.fullmatch(r"(.*?)(\d+)", employee_id or "")
    if not match:
        return employee_id or "", -1, 0
    return match.group(1), int(match.group(2)), len(match.group(2))


def _next_worker_id():
    """Suggest the next free Employee ID, e.g. WRK020 -> WRK021 (keeps the same prefix and digits)."""
    ids = [u.employee_id for u in User.query.filter_by(role="worker").all()]
    parts = [_id_parts(i) for i in ids if _id_parts(i)[1] >= 0]
    if not parts:
        return "WRK001"
    prefix = Counter(p[0] for p in parts).most_common(1)[0][0]
    numbers = [p[1] for p in parts if p[0] == prefix]
    width = max(3, min(p[2] for p in parts if p[0] == prefix))
    return f"{prefix}{max(numbers) + 1:0{width}d}"


@bp.route("/workers")
def workers():
    rows = sorted(User.query.filter_by(role="worker").all(), key=lambda u: _id_parts(u.employee_id)[:2])
    counts = dict(db.session.query(Report.worker_id, func.count(Report.id)).group_by(Report.worker_id).all())
    return render_template("manager/workers.html", rows=rows, counts=counts)


@bp.route("/workers/new", methods=["GET", "POST"])
def worker_new():
    form = {}
    if request.method == "POST":
        form = request.form
        employee_id = form.get("employee_id", "").strip().upper()
        name = form.get("name", "").strip()
        password = form.get("password", "")
        errors = []
        if not re.fullmatch(r"[A-Z0-9_-]{3,20}", employee_id):
            errors.append("Employee ID must be 3 to 20 characters: letters, numbers, - or _.")
        elif User.query.filter_by(employee_id=employee_id).first():
            errors.append("That Employee ID is already used.")
        if not 2 <= len(name) <= 80:
            errors.append("Please enter the worker's name (2 to 80 characters).")
        problems = password_problems(password)
        if problems:
            errors.append("The temporary password needs " + ", ".join(problems) + ".")
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            user = User(employee_id=employee_id, name=name, role="worker", active=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Worker {name} ({employee_id}) was added.", "success")
            return redirect(url_for("manager.workers"))
    return render_template("manager/worker_form.html", mode="new", form=form, worker=None, suggested=_next_worker_id())


@bp.route("/workers/<int:uid>/edit", methods=["GET", "POST"])
def worker_edit(uid):
    worker = _get_worker(uid)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        errors = []
        if not 2 <= len(name) <= 80:
            errors.append("Please enter the worker's name (2 to 80 characters).")
        if password:
            problems = password_problems(password)
            if problems:
                errors.append("The new password needs " + ", ".join(problems) + ".")
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            worker.name = name
            worker.active = request.form.get("active") == "on"
            if password:
                worker.set_password(password)
            if request.form.get("unlock") == "on":
                worker.reset_failures()
            db.session.commit()
            flash("Worker details saved.", "success")
            return redirect(url_for("manager.workers"))
    return render_template("manager/worker_form.html", mode="edit", form={}, worker=worker)


@bp.post("/workers/<int:uid>/toggle")
def worker_toggle(uid):
    worker = _get_worker(uid)
    worker.active = not worker.is_active
    db.session.commit()
    flash(f"{worker.name} is now {'active' if worker.active else 'disabled'}.", "success")
    return redirect(url_for("manager.workers"))


@bp.post("/workers/<int:uid>/delete")
def worker_delete(uid):
    worker = _get_worker(uid)
    name, count = worker.name, worker.reports.count()
    photos = delete_worker(worker)  # also deletes the worker's reports
    db.session.commit()
    remove_photo_files(photos)
    extra = f" together with {count} report(s)" if count else ""
    flash(f"Worker {name} was deleted{extra}.", "success")
    return redirect(url_for("manager.workers"))


# ---------------------------------------------------------------- live alerts (polled by the browser)
@bp.get("/api/notifications")
def notifications():
    unseen = Report.query.filter_by(seen_by_manager=False)
    latest = [
        {
            "id": r.id,
            "title": r.title,
            "machine": r.machine.name,
            "worker": r.worker.name,
            "match": r.match_status,
            "url": url_for("manager.report_detail", rid=r.id),
        }
        for r in unseen.order_by(Report.created_at.desc()).limit(3)
    ]
    return jsonify(count=unseen.count(), latest=latest)


@bp.post("/api/notifications/seen")
def notifications_seen():
    Report.query.filter_by(seen_by_manager=False).update({"seen_by_manager": True})
    db.session.commit()
    return jsonify(ok=True)
