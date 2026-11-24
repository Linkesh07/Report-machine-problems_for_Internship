"""Small helpers that create, merge, split and delete issues and reports."""
import os

from flask import current_app

from . import db
from .models import Issue, LoginLog, Report


def new_issue_for(report):
    """Give a (flushed) report its own brand-new issue."""
    issue = Issue(
        title=report.title,
        status=report.status or "Pending",
        priority=report.severity,
        main_report_id=report.id,
    )
    db.session.add(issue)
    db.session.flush()
    report.issue = issue
    db.session.flush()
    issue.recount()
    return issue


def move_report_to_issue(report, target):
    """Merge a report into another (original) issue. Returns True if it moved."""
    old = report.issue
    report.match_status = "duplicate"
    report.manager_reviewed = True
    if report.duplicate_of is None or report.duplicate_of.issue_id != target.id:
        report.duplicate_of_id = target.main_report_id
    if old is not None and old.id == target.id:
        return False

    report.issue = target
    report.status = target.status
    db.session.flush()

    if old is not None:
        left = Report.query.filter_by(issue_id=old.id).count()
        if left == 0:
            # The old issue is empty now: keep its notes, then remove it.
            for note in list(old.notes):
                note.issue = target
            db.session.flush()
            db.session.delete(old)
        else:
            old.recount()
    target.recount()
    return True


def split_report_from_issue(report):
    """Manager says: this is NOT a duplicate. Make it a separate issue."""
    old = report.issue
    report.match_status = "new"
    report.duplicate_of_id = None
    report.manager_reviewed = True
    if old is None:
        return new_issue_for(report)

    alone = Report.query.filter_by(issue_id=old.id).count() == 1
    if alone:
        return old  # it already has its own issue

    issue = Issue(
        title=report.title,
        status=old.status,
        priority=report.severity,
        main_report_id=report.id,
    )
    db.session.add(issue)
    db.session.flush()
    report.issue = issue
    db.session.flush()
    issue.recount()
    old.recount()
    return issue


def delete_report(report):
    """Delete one report and keep its issue tidy.

    - If it was the main report of an issue that has other reports, the oldest remaining
      report becomes the new main report.
    - If it was the only report, the issue (and its notes) is deleted too.
    Returns the photo file names so the caller can remove them after the commit.
    """
    rid = report.id
    photos = [report.photo_path] if report.photo_path else []
    issue = report.issue
    remaining = []
    if issue is not None:
        remaining = (
            Report.query.filter(Report.issue_id == issue.id, Report.id != rid)
            .order_by(Report.created_at, Report.id)
            .all()
        )
    was_main = issue is not None and issue.main_report_id == rid
    new_main = None
    if remaining:
        new_main = remaining[0] if was_main else next(
            (x for x in remaining if x.id == issue.main_report_id), remaining[0]
        )

    # Reports that pointed at the deleted one must point somewhere valid.
    for dep in Report.query.filter(Report.duplicate_of_id == rid).all():
        if new_main is not None and dep.issue_id == issue.id and dep.id != new_main.id:
            dep.duplicate_of_id = new_main.id
        else:
            dep.duplicate_of_id = None
            dep.match_status = "new"
            dep.similarity_score = None

    if was_main and new_main is not None:
        new_main.match_status = "new"
        new_main.duplicate_of_id = None
        new_main.similarity_score = None
        issue.main_report_id = new_main.id
        issue.title = new_main.title

    db.session.flush()
    db.session.delete(report)
    db.session.flush()

    if issue is not None:
        if remaining:
            db.session.expire(issue, ["reports"])
            issue.recount()
        else:
            db.session.delete(issue)  # its notes are deleted with it
    return photos


def delete_worker(worker):
    """Delete a worker together with all of their reports. Returns photo names to remove."""
    photos = []
    for report in Report.query.filter_by(worker_id=worker.id).order_by(Report.id).all():
        photos += delete_report(report)
    LoginLog.query.filter_by(user_id=worker.id).update({"user_id": None})
    db.session.delete(worker)
    return photos


def remove_photo_files(names):
    """Delete photo files from disk (call after the database commit succeeded)."""
    folder = current_app.config["UPLOAD_FOLDER"]
    for name in names:
        try:
            os.remove(os.path.join(folder, os.path.basename(name)))
        except OSError:
            pass
