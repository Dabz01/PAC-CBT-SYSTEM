from __future__ import annotations

import os
import secrets
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort
from werkzeug.security import check_password_hash

from common import (
    SCHOOL_NAME,
    SCHOOL_ADDRESS,
    ALLOWED_CLASSES,
    ensure_db,
    get_db,
    get_student_profile,
    exam_is_active,
    parse_subjects,
    exam_window,
    now,
)

app = Flask(__name__, template_folder="templates_student", static_folder="static")
app.secret_key = os.environ.get("EXAMHUB_STUDENT_SECRET", secrets.token_hex(32))


def student_required(f):
    @wraps(f)
    def _w(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "student":
            flash("Student access required.", "danger")
            return redirect(url_for("logout"))
        return f(*args, **kwargs)

    return _w


@app.context_processor
def inject_brand():
    return {"SCHOOL_NAME": SCHOOL_NAME, "SCHOOL_ADDRESS": SCHOOL_ADDRESS, "PORTAL_NAME": "Student Portal"}


@app.route("/")
def home():
    return render_template("welcome.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("login"))
        if user["role"] != "student":
            flash("This login is for STUDENTS only.", "danger")
            return redirect(url_for("login"))
        session["user_id"] = user["id"]
        session["role"] = user["role"]

        student_profile = get_student_profile(user["id"])
        if student_profile and int(student_profile["is_archived"] or 0) == 1:
            session.clear()
            flash("Your account is archived. Please contact the admin.", "danger")
            return redirect(url_for("login"))

        return redirect(url_for("timetable"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/timetable")
@student_required
def timetable():
    db = get_db()
    student = get_student_profile(session["user_id"])
    if not student:
        flash("Student profile missing. Contact admin.", "danger")
        return redirect(url_for("logout"))

    exams_all = db.execute(
        "SELECT * FROM exams WHERE class=? AND is_active=1 ORDER BY start_at ASC",
        (student["class"],),
    ).fetchall()

    allowed_subjects = parse_subjects(student.get("subjects") if hasattr(student, "get") else (student["subjects"] if "subjects" in student.keys() else ""))
    # If student has no registered subjects, show all exams for their class.
    exams = []
    for e in exams_all:
        subj = (e["subject"] if "subject" in e.keys() else "General") or "General"
        if not allowed_subjects:
            exams.append(e)
        else:
            if subj.strip().lower() in allowed_subjects:
                exams.append(e)

    active, upcoming, past = [], [], []
    for e in exams:
        start_dt, end_dt = exam_window(e)
        if start_dt <= now() <= end_dt:
            active.append(e)
        elif now() < start_dt:
            upcoming.append(e)
        else:
            past.append(e)

    return render_template(
        "timetable.html",
        student=student,
        active=active,
        upcoming=upcoming,
        past=past,
        exam_window=exam_window,
        now=now,
    )


@app.route("/enter-code", methods=["GET", "POST"])
@student_required
def enter_code():
    if request.method == "POST":
        code = request.form.get("access_code", "").strip()
        if not code:
            flash("Enter an access code.", "warning")
            return redirect(url_for("enter_code"))

        db = get_db()
        student = get_student_profile(session["user_id"])
        if not student:
            flash("Student profile missing. Contact admin.", "danger")
            return redirect(url_for("logout"))

        exam = db.execute("SELECT * FROM exams WHERE access_code=?", (code,)).fetchone()
        if not exam:
            flash("Invalid access code.", "danger")
            return redirect(url_for("enter_code"))

        # Enforce class match (students can only take exams for their class)
        if (exam["class"] or "").strip() != (student["class"] or "").strip():
            flash(f"This exam is for {exam['class']}. Your class is {student['class']}.", "danger")
            return redirect(url_for("enter_code"))

        if not exam_is_active(exam):
            start_dt, end_dt = exam_window(exam)
            flash(f"Exam not active. Window: {start_dt} → {end_dt}", "warning")
            return redirect(url_for("enter_code"))

        attempted = db.execute(
            "SELECT id FROM attempts WHERE exam_id=? AND student_id=? LIMIT 1",
            (exam["id"], student["id"]),
        ).fetchone()
        if attempted:
            flash("You have already taken this exam.", "warning")
            return redirect(url_for("enter_code"))

        return redirect(url_for("start_exam", exam_id=exam["id"]))
    return render_template("enter_code.html")


@app.route("/exam/<int:exam_id>/start")
@student_required
def start_exam(exam_id: int):
    db = get_db()
    student = get_student_profile(session["user_id"])
    exam = db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam or not student:
        abort(404)


    allowed_subjects = parse_subjects(student.get('subjects') if hasattr(student,'get') else (student['subjects'] if 'subjects' in student.keys() else ''))
    subj = (exam['subject'] if 'subject' in exam.keys() else 'General') or 'General'
    if allowed_subjects and subj.strip().lower() not in allowed_subjects:
        flash('You are not registered for this subject.', 'danger')
        return redirect(url_for('timetable'))

    if not exam_is_active(exam):
        flash("Exam is not active.", "warning")
        return redirect(url_for("enter_code"))

    existing = db.execute(
        "SELECT * FROM attempts WHERE exam_id=? AND student_id=? LIMIT 1",
        (exam_id, student["id"]),
    ).fetchone()
    if existing:
        return redirect(url_for("take_exam", exam_id=exam_id))

    db.execute(
        "INSERT INTO attempts (exam_id, student_id, started_at) VALUES (?,?,?)",
        (exam_id, student["id"], now().isoformat(timespec="seconds")),
    )
    db.commit()
    return redirect(url_for("take_exam", exam_id=exam_id))


@app.route("/exam/<int:exam_id>/take")
@student_required
def take_exam(exam_id: int):
    db = get_db()
    student = get_student_profile(session["user_id"])
    exam = db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam or not student:
        abort(404)


    allowed_subjects = parse_subjects(student.get('subjects') if hasattr(student,'get') else (student['subjects'] if 'subjects' in student.keys() else ''))
    subj = (exam['subject'] if 'subject' in exam.keys() else 'General') or 'General'
    if allowed_subjects and subj.strip().lower() not in allowed_subjects:
        flash('You are not registered for this subject.', 'danger')
        return redirect(url_for('timetable'))

    attempt = db.execute(
        "SELECT * FROM attempts WHERE exam_id=? AND student_id=? LIMIT 1",
        (exam_id, student["id"]),
    ).fetchone()
    if not attempt:
        return redirect(url_for("start_exam", exam_id=exam_id))

    if int(attempt["submitted"]) == 1:
        flash("This attempt is already submitted.", "info")
        return redirect(url_for("enter_code"))

    _, end_dt = exam_window(exam)
    if now() > end_dt:
        return redirect(url_for("submit_exam", exam_id=exam_id))

    questions = db.execute("SELECT * FROM questions WHERE exam_id=? ORDER BY id ASC", (exam_id,)).fetchall()
    saved = db.execute(
        "SELECT question_id, selected_option FROM attempt_answers WHERE attempt_id=?",
        (attempt["id"],),
    ).fetchall()
    saved_map = {r["question_id"]: (r["selected_option"] or "") for r in saved}

    return render_template(
        "take_exam.html",
        exam=exam,
        attempt=attempt,
        questions=questions,
        saved_map=saved_map,
        # Send a numeric epoch (ms) to avoid browser ISO parsing / timezone quirks.
        end_at_ms=int(end_dt.timestamp() * 1000),
        server_now_ms=int(now().timestamp() * 1000),
    )


# -------------------------------
# URL aliases to match frontend calls (/student/exam/...)
# -------------------------------
@app.route("/student/exam/<int:exam_id>/save-answer", methods=["POST"])
@student_required
def save_answer_alias(exam_id: int):
    return save_answer(exam_id)


@app.route("/student/exam/<int:exam_id>/proctor-log", methods=["POST"])
@student_required
def proctor_log_alias(exam_id: int):
    return proctor_log(exam_id)


# ✅ FIX: add submit alias so /student/exam/<id>/submit works (prevents 404 on submit)
@app.route("/student/exam/<int:exam_id>/submit")
@student_required
def submit_exam_alias(exam_id: int):
    return submit_exam(exam_id)


@app.route("/exam/<int:exam_id>/save-answer", methods=["POST"])
@student_required
def save_answer(exam_id: int):
    db = get_db()
    student = get_student_profile(session["user_id"])
    exam = db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam or not student:
        return jsonify({"ok": False, "error": "Not found"}), 404

    attempt = db.execute(
        "SELECT * FROM attempts WHERE exam_id=? AND student_id=? LIMIT 1",
        (exam_id, student["id"]),
    ).fetchone()
    if not attempt or int(attempt["submitted"]) == 1:
        return jsonify({"ok": False, "error": "Attempt closed"}), 400

    _, end_dt = exam_window(exam)
    if now() > end_dt:
        return jsonify({"ok": False, "error": "Time up"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    qid = int(payload.get("question_id", 0))

    # Accept both keys: selected_option (backend) and selected (common frontend)
    selected = (payload.get("selected_option") or payload.get("selected") or "").upper().strip()
    if selected not in ("A", "B", "C", "D"):
        return jsonify({"ok": False, "error": "Invalid option"}), 400

    existing = db.execute(
        "SELECT id FROM attempt_answers WHERE attempt_id=? AND question_id=?",
        (attempt["id"], qid),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE attempt_answers SET selected_option=?, saved_at=CURRENT_TIMESTAMP WHERE id=?",
            (selected, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO attempt_answers (attempt_id, question_id, selected_option) VALUES (?,?,?)",
            (attempt["id"], qid, selected),
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/exam/<int:exam_id>/proctor-log", methods=["POST"])
@student_required
def proctor_log(exam_id: int):
    db = get_db()
    student = get_student_profile(session["user_id"])
    attempt = db.execute(
        "SELECT * FROM attempts WHERE exam_id=? AND student_id=? LIMIT 1",
        (exam_id, student["id"]),
    ).fetchone()
    if not attempt:
        return jsonify({"ok": False, "error": "No attempt"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    vtype = (payload.get("type") or "").strip()
    details = (payload.get("details") or "").strip()
    if not vtype:
        return jsonify({"ok": False, "error": "Missing type"}), 400

    db.execute(
        "INSERT INTO proctor_logs (attempt_id, exam_id, student_id, violation_type, details) VALUES (?,?,?,?,?)",
        (attempt["id"], exam_id, student["id"], vtype, details[:500]),
    )
    db.execute("UPDATE attempts SET violations = COALESCE(violations,0) + 1 WHERE id=?", (attempt["id"],))
    db.commit()
    updated = db.execute("SELECT violations FROM attempts WHERE id=?", (attempt["id"],)).fetchone()
    return jsonify({"ok": True, "violations": int(updated["violations"] or 0)})


@app.route("/exam/<int:exam_id>/submit")
@student_required
def submit_exam(exam_id: int):
    db = get_db()
    student = get_student_profile(session["user_id"])
    exam = db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam or not student:
        abort(404)


    allowed_subjects = parse_subjects(student.get('subjects') if hasattr(student,'get') else (student['subjects'] if 'subjects' in student.keys() else ''))
    subj = (exam['subject'] if 'subject' in exam.keys() else 'General') or 'General'
    if allowed_subjects and subj.strip().lower() not in allowed_subjects:
        flash('You are not registered for this subject.', 'danger')
        return redirect(url_for('timetable'))

    attempt = db.execute(
        "SELECT * FROM attempts WHERE exam_id=? AND student_id=? LIMIT 1",
        (exam_id, student["id"]),
    ).fetchone()
    if not attempt:
        abort(404)
    if int(attempt["submitted"]) == 1:
        flash("Already submitted.", "info")
        return redirect(url_for("enter_code"))

    questions = db.execute("SELECT id, correct_option, marks FROM questions WHERE exam_id=?", (exam_id,)).fetchall()
    ans = db.execute(
        "SELECT question_id, selected_option FROM attempt_answers WHERE attempt_id=?",
        (attempt["id"],),
    ).fetchall()
    amap = {r["question_id"]: (r["selected_option"] or "") for r in ans}

    score = 0.0
    total = 0.0
    for q in questions:
        total += float(q["marks"])
        if amap.get(q["id"], "").upper() == (q["correct_option"] or "").upper():
            score += float(q["marks"])

    db.execute(
        "UPDATE attempts SET score=?, submitted=1, submitted_at=? WHERE id=?",
        (score, now().isoformat(timespec="seconds"), attempt["id"]),
    )
    db.commit()

    flash("Submitted successfully.", "success")
    return redirect(url_for("thank_you", attempt_id=attempt["id"]))


@app.route("/thank-you/<int:attempt_id>")
@student_required
def thank_you(attempt_id: int):
    db = get_db()
    attempt = db.execute(
        """
        SELECT a.id, a.score, a.submitted_at, a.violations, e.title, e.class, e.id as exam_id
        FROM attempts a JOIN exams e ON e.id=a.exam_id
        WHERE a.id=? AND a.submitted=1
        """,
        (attempt_id,),
    ).fetchone()
    if not attempt:
        abort(404)

    total_row = db.execute(
        "SELECT COALESCE(SUM(marks),0) AS total FROM questions WHERE exam_id=?",
        (attempt["exam_id"],),
    ).fetchone()
    total = float(total_row["total"] or 0)
    return render_template("thank_you.html", attempt=attempt, total=total)


if __name__ == "__main__":
    ensure_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("STUDENT_PORT", "5000")), debug=True)
