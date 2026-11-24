"""Login, logout and password change. Workers and the manager use separate login pages."""
import re

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .models import LOCK_MINUTES, LoginLog, User

bp = Blueprint("auth", __name__)

# Used so that a wrong Employee ID takes the same time as a wrong password.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def dashboard_for(user):
    return url_for("manager.dashboard" if user.role == "manager" else "worker.dashboard")


def password_problems(password):
    """Return a list of things missing from a password (empty list = strong enough)."""
    problems = []
    if len(password) < 8:
        problems.append("at least 8 characters")
    if not re.search(r"[a-z]", password):
        problems.append("a lowercase letter")
    if not re.search(r"[A-Z]", password):
        problems.append("a capital letter")
    if not re.search(r"\d", password):
        problems.append("a number")
    return problems


def _safe_next(role):
    target = request.args.get("next") or request.form.get("next") or ""
    if target == f"/{role}" or target.startswith(f"/{role}/"):
        return target
    return None


def _log(user, employee_id, role, success):
    db.session.add(
        LoginLog(
            user_id=user.id if user else None,
            employee_id=employee_id[:30],
            role=role,
            success=success,
            ip_address=request.remote_addr,
        )
    )


def _login(role):
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    employee_id = ""
    if request.method == "POST":
        employee_id = request.form.get("employee_id", "").strip().upper()
        password = request.form.get("password", "")
        user = User.query.filter_by(employee_id=employee_id).first()
        generic = f"Wrong Employee ID or password for the {role} login."

        if user is None:
            check_password_hash(_DUMMY_HASH, password)
            _log(None, employee_id, role, False)
            flash(generic, "error")
        elif user.role != role:
            # Right person, wrong door (e.g. a worker on the manager page).
            check_password_hash(_DUMMY_HASH, password)
            _log(user, employee_id, role, False)
            flash(generic, "error")
        elif user.is_locked():
            _log(user, employee_id, role, False)
            flash(
                f"Too many wrong attempts. This account is locked for about "
                f"{user.minutes_locked()} more minute(s).",
                "error",
            )
        elif not user.check_password(password):
            user.register_failure()
            _log(user, employee_id, role, False)
            if user.is_locked():
                flash(f"Too many wrong attempts. The account is locked for {LOCK_MINUTES} minutes.", "error")
            else:
                flash(generic, "error")
        elif not user.is_active:
            _log(user, employee_id, role, False)
            flash("This account is disabled. Please contact the manager.", "error")
        else:
            user.reset_failures()
            user.last_login = db.func.now()
            _log(user, employee_id, role, True)
            db.session.commit()
            login_user(user)
            session.permanent = True  # enables the 30 minute idle time-out
            return redirect(_safe_next(role) or dashboard_for(user))
        db.session.commit()

    return render_template(
        "login.html", role=role, employee_id=employee_id, next=request.args.get("next", "")
    )


@bp.route("/worker-login", methods=["GET", "POST"])
def worker_login():
    return _login("worker")


@bp.route("/manager-login", methods=["GET", "POST"])
def manager_login():
    return _login("manager")


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "GET":
        return redirect(dashboard_for(current_user) if current_user.is_authenticated else url_for("main.home"))
    logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("main.home"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        problems = password_problems(new)
        if not current_user.check_password(current):
            flash("Your current password is not correct.", "error")
        elif new != confirm:
            flash("The two new passwords do not match.", "error")
        elif problems:
            flash("The new password needs " + ", ".join(problems) + ".", "error")
        elif new == current:
            flash("Please choose a password different from the old one.", "error")
        else:
            current_user.set_password(new)
            db.session.commit()
            flash("Password changed successfully.", "success")
            return redirect(dashboard_for(current_user))
    return render_template("change_password.html")
