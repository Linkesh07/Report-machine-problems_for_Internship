"""Database tables (SQLAlchemy models)."""
from datetime import datetime, timedelta

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, login_manager

SEVERITIES = ["Low", "Medium", "High", "Critical"]
STATUSES = ["Pending", "In Progress", "Resolved"]
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 5


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(30), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    role = db.Column(db.String(10), nullable=False, index=True)  # "manager" or "worker"
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column("is_active", db.Boolean, nullable=False, default=True)
    failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    last_login = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def is_active(self):  # used by Flask-Login
        return bool(self.active)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_locked(self):
        return bool(self.locked_until and self.locked_until > datetime.now())

    def minutes_locked(self):
        if not self.is_locked():
            return 0
        return max(1, int((self.locked_until - datetime.now()).total_seconds() // 60) + 1)

    def register_failure(self):
        self.failed_attempts = (self.failed_attempts or 0) + 1
        if self.failed_attempts >= MAX_FAILED_LOGINS:
            self.locked_until = datetime.now() + timedelta(minutes=LOCK_MINUTES)
            self.failed_attempts = 0

    def reset_failures(self):
        self.failed_attempts = 0
        self.locked_until = None


@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(User, int(user_id))
    return user if user and user.is_active else None


class LoginLog(db.Model):
    __tablename__ = "login_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    employee_id = db.Column(db.String(30))
    role = db.Column(db.String(10))
    success = db.Column(db.Boolean, nullable=False)
    ip_address = db.Column(db.String(64))
    time = db.Column(db.DateTime, default=datetime.now)


class Machine(db.Model):
    __tablename__ = "machines"

    id = db.Column(db.Integer, primary_key=True)
    machine_code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    department = db.Column(db.String(60), nullable=False)

    @property
    def label(self):
        return f"{self.name} ({self.machine_code})"


class Issue(db.Model):
    """One real-world problem. Many reports (from many workers) can belong to it."""

    __tablename__ = "issues"

    id = db.Column(db.Integer, primary_key=True)
    main_report_id = db.Column(db.Integer, index=True)  # the first / original report
    title = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(15), nullable=False, default="Pending")
    priority = db.Column(db.String(10), nullable=False, default="Medium")
    priority_locked = db.Column(db.Boolean, nullable=False, default=False)  # manager set it by hand
    reporter_count = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now)

    reports = db.relationship(
        "Report", backref="issue", foreign_keys="Report.issue_id", order_by="Report.created_at"
    )
    notes = db.relationship(
        "ManagerNote", backref="issue", order_by="ManagerNote.created_at.desc()",
        cascade="all, delete-orphan",
    )

    @property
    def main_report(self):
        return db.session.get(Report, self.main_report_id) if self.main_report_id else None

    @property
    def machine(self):
        main = self.main_report
        return main.machine if main else None

    def recount(self):
        """Refresh the worker counter, the main report and the automatic priority."""
        reports = list(self.reports)
        if not reports:
            return
        self.reporter_count = len({r.worker_id for r in reports})
        if self.main_report_id not in {r.id for r in reports}:
            first = min(reports, key=lambda r: (r.created_at or datetime.now(), r.id))
            self.main_report_id = first.id
            self.title = first.title
        if not self.priority_locked:
            level = max(SEVERITY_RANK.get(r.severity, 1) for r in reports)
            if self.reporter_count >= 3:
                level += 1  # many workers complain -> more urgent
            elif self.reporter_count == 2:
                level = max(level, 1)
            self.priority = SEVERITIES[min(level, len(SEVERITIES) - 1)]
        self.updated_at = datetime.now()

    def set_status(self, status):
        self.status = status
        for report in self.reports:
            report.status = status
        self.updated_at = datetime.now()


class Report(db.Model):
    __tablename__ = "reports"

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(10), nullable=False, default="Medium")
    status = db.Column(db.String(15), nullable=False, default="Pending", index=True)
    photo_path = db.Column(db.String(80))

    # Duplicate detection result: "new", "duplicate" or "possible"
    match_status = db.Column(db.String(10), nullable=False, default="new", index=True)
    duplicate_of_id = db.Column(db.Integer, db.ForeignKey("reports.id"))
    similarity_score = db.Column(db.Float)
    manager_reviewed = db.Column(db.Boolean, nullable=False, default=False)
    seen_by_manager = db.Column(db.Boolean, nullable=False, default=False, index=True)

    issue_id = db.Column(db.Integer, db.ForeignKey("issues.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    worker = db.relationship("User", backref=db.backref("reports", lazy="dynamic"))
    machine = db.relationship("Machine", backref="reports")
    duplicate_of = db.relationship("Report", remote_side=[id], foreign_keys=[duplicate_of_id])

    @property
    def is_duplicate(self):
        return self.match_status == "duplicate"

    @property
    def text(self):
        return f"{self.title}. {self.message}"


class ManagerNote(db.Model):
    __tablename__ = "manager_notes"

    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issues.id"), nullable=False, index=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    note_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    manager = db.relationship("User")
