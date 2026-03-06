# admin_app.py (FULLY CORRECTED)
from __future__ import annotations

import os
import secrets
import csv
import io
import string
import re
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    abort,
    Response,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from common import (
    SCHOOL_NAME,
    SCHOOL_ADDRESS,
    ALLOWED_CLASSES,
    is_allowed_class,
    ensure_db,
    get_db,
    get_subjects_offered,
    now,
)

app = Flask(__name__, template_folder="templates_admin", static_folder="static")
app.secret_key = os.environ.get("ADMIN_SECRET", secrets.token_hex(32))


# -----------------------------------
# ✅ SAFETY: ALWAYS ensure DB is ready
# -----------------------------------
def _ensure_admin_portal_tables():
    """
    Admin Portal depends on these tables. Older DBs may not have them.
    Safe to run repeatedly (IF NOT EXISTS) + safe ALTER migrations.
    """
    db = get_db()

    # teachers table
    db.execute(
        """CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );"""
    )

    # admins table
    db.execute(
        """CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            full_name TEXT,
            admin_type TEXT NOT NULL DEFAULT 'super',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );"""
    )

    # subjects table
    db.execute(
        """CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""
    )

    # audit log used by dashboard
    db.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            actor_id INTEGER,
            actor_username TEXT,
            exam_id INTEGER,
            exam_title TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""
    )

    # student credential export table
    db.execute(
        """CREATE TABLE IF NOT EXISTS student_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            password_plain TEXT NOT NULL,
            full_name TEXT,
            class TEXT,
            batch_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );"""
    )

    # result admins (separate credential gate for result-card module)
    db.execute(
        """CREATE TABLE IF NOT EXISTS result_admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""
    )

    # attempts extra columns for mixed Objective/Theory scoring (safe migrations)
    try:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(attempts)").fetchall()]
        if "objective_score" not in cols:
            db.execute("ALTER TABLE attempts ADD COLUMN objective_score REAL DEFAULT 0")
        if "theory_score" not in cols:
            db.execute("ALTER TABLE attempts ADD COLUMN theory_score REAL DEFAULT 0")
        if "total_score" not in cols:
            db.execute("ALTER TABLE attempts ADD COLUMN total_score REAL DEFAULT 0")

        # Backfill objective_score from legacy score column where applicable
        db.execute(
            """UPDATE attempts
                SET objective_score=COALESCE(NULLIF(objective_score,0), COALESCE(score,0))
                WHERE objective_score IS NULL OR objective_score=0"""
        )
        # Backfill total_score
        db.execute(
            """UPDATE attempts
                SET total_score=COALESCE(NULLIF(total_score,0), COALESCE(objective_score,0)+COALESCE(theory_score,0))
                WHERE total_score IS NULL OR total_score=0"""
        )
    except Exception:
        pass

    db.commit()

    # Seed default Result Admin accounts (idempotent)
    try:
        existing = db.execute("SELECT username FROM result_admins").fetchall()
        existing_usernames = {r["username"] for r in existing} if existing else set()

        defaults = [
            ("Mr. Kizito", generate_password_hash("KizitoGint"), "Mr. Kizito"),
            ("Mr. Chukwuma", generate_password_hash("ChukwumaGint"), "Mr. Chukwuma"),
        ]

        for uname, pwh, full in defaults:
            if uname not in existing_usernames:
                db.execute(
                    "INSERT OR IGNORE INTO result_admins (username, password_hash, full_name) VALUES (?,?,?)",
                    (uname, pwh, full),
                )
        db.commit()
    except Exception:
        pass

    # Backfill admins rows for existing admin users (default super)
    try:
        admin_users = db.execute("SELECT id, username FROM users WHERE role='admin'").fetchall()
        for u in admin_users:
            exists = db.execute("SELECT 1 FROM admins WHERE user_id=?", (u["id"],)).fetchone()
            if not exists:
                db.execute(
                    "INSERT OR IGNORE INTO admins (user_id, full_name, admin_type) VALUES (?,?,?)",
                    (u["id"], u["username"], "super"),
                )
        db.commit()
    except Exception:
        pass

@app.before_request
def _db_guard():
    """
    Ensures schema + migrations for EVERY request.
    This prevents 'no such table' errors on old DBs.
    """
    try:
        ensure_db()
    except Exception:
        pass
    try:
        _ensure_admin_portal_tables()
    except Exception:
        pass


# -------------------------
# AUTH / BRAND
# -------------------------
def admin_required(f):
    @wraps(f)
    def _w(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("logout"))
        return f(*args, **kwargs)

    return _w


def result_admin_required(f):
    """Secondary gate used by the Result module (within Admin Portal)."""

    @wraps(f)
    def _w(*args, **kwargs):
        if "user_id" not in session or session.get("role") != "admin":
            return redirect(url_for("login"))
        if not session.get("result_admin_id"):
            return redirect(url_for("result_admin_login"))
        return f(*args, **kwargs)

    return _w


@app.context_processor
def inject_brand():
    return {
        "SCHOOL_NAME": SCHOOL_NAME,
        "SCHOOL_ADDRESS": SCHOOL_ADDRESS,
        "PORTAL_NAME": "Admin Portal",
        "ALLOWED_CLASSES": ALLOWED_CLASSES,
        "SUBJECTS_OFFERED": get_subjects_offered(),  # ✅ dynamic subjects
        "ADMIN_TYPE": session.get("admin_type", "super"),
    }


def log_audit(event_type: str, actor_role: str, actor_id: int | None, detail: str, exam_id: int | None = None):
    """Best-effort audit logging (never break requests)."""
    try:
        db = get_db()
        actor_username = None
        if actor_id:
            u = db.execute("SELECT username FROM users WHERE id=?", (actor_id,)).fetchone()
            actor_username = u["username"] if u else None

        exam_title = None
        if exam_id:
            e = db.execute("SELECT title FROM exams WHERE id=?", (exam_id,)).fetchone()
            exam_title = e["title"] if e else None

        db.execute(
            """INSERT INTO audit_log (event_type, actor_role, actor_id, actor_username, exam_id, exam_title, detail)
               VALUES (?,?,?,?,?,?,?)""",
            (event_type, actor_role, actor_id, actor_username, exam_id, exam_title, detail),
        )
        db.commit()
    except Exception:
        pass


def _ensure_admin_row_and_get_type(db, user_id: int, username_fallback: str) -> str:
    """
    Ensures a row exists in admins for this user and returns admin_type.
    """
    admin_row = db.execute("SELECT admin_type FROM admins WHERE user_id=?", (user_id,)).fetchone()
    if not admin_row:
        db.execute(
            "INSERT OR IGNORE INTO admins (user_id, full_name, admin_type) VALUES (?,?,?)",
            (user_id, username_fallback, "super"),
        )
        db.commit()
        admin_row = db.execute("SELECT admin_type FROM admins WHERE user_id=?", (user_id,)).fetchone()

    admin_type = (admin_row["admin_type"] if admin_row else "super") or "super"
    if admin_type not in ("super", "sub"):
        admin_type = "super"
    return admin_type


# -------------------------
# HELPERS
# -------------------------
def _generate_student_code(db) -> str:
    prefix = "PACS-"
    for _ in range(1000):
        code = prefix + "".join(secrets.choice(string.digits) for _ in range(6))
        exists = db.execute("SELECT 1 FROM users WHERE username=? LIMIT 1", (code,)).fetchone()
        if not exists:
            return code
    raise RuntimeError("Could not generate a unique student code. Try again.")


def _generate_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _parse_students_csv(file_bytes: bytes):
    """Parse CSV headers: name/full_name,class,subjects"""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return []

    header = [c.strip().lower() for c in rows[0]]
    has_header = any(h in ("name", "full_name", "class", "subject", "subjects") for h in header)
    students = []

    if has_header:
        idx_name = None
        idx_class = None
        idx_subjects = None
        for i, h in enumerate(header):
            if h in ("name", "full_name"):
                idx_name = i
            if h == "class":
                idx_class = i
            if h in ("subject", "subjects"):
                idx_subjects = i

        for r in rows[1:]:
            name = (r[idx_name].strip() if idx_name is not None and idx_name < len(r) else "").strip()
            klass = (r[idx_class].strip() if idx_class is not None and idx_class < len(r) else "").strip()
            subjects = (r[idx_subjects].strip() if idx_subjects is not None and idx_subjects < len(r) else "").strip()
            if name:
                students.append({"full_name": name, "class": klass, "subjects": subjects})
    else:
        for r in rows:
            name = (r[0].strip() if len(r) >= 1 else "").strip()
            klass = (r[1].strip() if len(r) >= 2 else "").strip()
            subjects = (r[2].strip() if len(r) >= 3 else "").strip()
            if name:
                students.append({"full_name": name, "class": klass, "subjects": subjects})

    return students


def _delete_exam_cascade(db, exam_id: int):
    db.execute("DELETE FROM exams WHERE id=?", (exam_id,))


def _delete_user_cascade(db, user_id: int):
    role = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if role and role["role"] == "teacher":
        exam_ids = db.execute("SELECT id FROM exams WHERE created_by=?", (user_id,)).fetchall()
        for e in exam_ids:
            _delete_exam_cascade(db, int(e["id"]))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))


# -------------------------
# ROUTES
# -------------------------
@app.route("/")
def home():
    if session.get("user_id") and session.get("role") == "admin":
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


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
        if user["role"] != "admin":
            flash("This portal is for ADMINS only.", "danger")
            return redirect(url_for("login"))

        session["user_id"] = int(user["id"])
        session["role"] = "admin"

        # ✅ admin_type from admins table
        session["admin_type"] = _ensure_admin_row_and_get_type(
            db=db,
            user_id=int(user["id"]),
            username_fallback=user["username"],
        )

        log_audit("admin_login", "admin", int(user["id"]), "Admin logged in")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    if session.get("user_id"):
        log_audit("admin_logout", "admin", session.get("user_id"), "Admin logged out")
    session.clear()
    return redirect(url_for("login"))


# -------------------------
# RESULT MODULE (separate login)
# -------------------------
@app.route("/result/login", methods=["GET", "POST"], endpoint="result_admin_login")
@admin_required
def result_admin_login():
    """Result module login (only Result Admins)."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        db = get_db()
        ra = db.execute("SELECT * FROM result_admins WHERE username=?", (username,)).fetchone()
        if not ra or not check_password_hash(ra["password_hash"], password):
            flash("Invalid Result Admin username or password.", "danger")
            return redirect(url_for("result_admin_login"))

        session["result_admin_id"] = int(ra["id"])
        session["result_admin_name"] = ra["full_name"]
        session["result_admin_username"] = ra["username"]
        return redirect(url_for("result_home"))

    return render_template("result_admin_login.html")


@app.route("/result/logout", endpoint="result_admin_logout")
@admin_required
def result_admin_logout():
    session.pop("result_admin_id", None)
    session.pop("result_admin_name", None)
    session.pop("result_admin_username", None)
    flash("Logged out of Result module.", "info")
    return redirect(url_for("result_admin_login"))


@app.route("/result", endpoint="result_home")
@result_admin_required
def result_home():
    return render_template(
        "result_admin_home.html",
        classes=["GRADE 9 DIAMOND", "GRADE 9 EMERALD"],
        result_admin_name=session.get("result_admin_name") or "Result Admin",
    )


@app.route("/result/admins/create", methods=["GET", "POST"], endpoint="create_result_admin")
@result_admin_required
def create_result_admin():
    db = get_db()
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not full_name or not username or not password:
            flash("Full name, username and password are required.", "danger")
            return redirect(url_for("create_result_admin"))

        try:
            db.execute(
                "INSERT INTO result_admins (username, password_hash, full_name) VALUES (?,?,?)",
                (username, generate_password_hash(password), full_name),
            )
            db.commit()
            flash("Result Admin created successfully.", "success")
            return redirect(url_for("result_home"))
        except Exception:
            flash("Username already exists (or could not be created).", "danger")
            return redirect(url_for("create_result_admin"))

    return render_template("create_result_admin.html")


@app.route("/result/class/<path:klass>", endpoint="result_class")
@result_admin_required
def result_class(klass: str):
    klass = (klass or "").strip()
    if klass not in ("GRADE 9 DIAMOND", "GRADE 9 EMERALD"):
        flash("Invalid class selected.", "danger")
        return redirect(url_for("result_home"))

    db = get_db()
    rows = db.execute(
        """SELECT s.id as student_id, s.full_name, s.class, s.subjects, s.photo_path, u.username
           FROM students s
           JOIN users u ON u.id=s.user_id
           WHERE s.class=?
           ORDER BY s.full_name COLLATE NOCASE ASC""",
        (klass,),
    ).fetchall()

    students = [
        {
            "student_id": int(r["student_id"]),
            "full_name": r["full_name"],
            "class": r["class"],
            "subjects": r["subjects"] or "",
            "photo_path": r["photo_path"],
            "username": r["username"],
        }
        for r in rows
    ]

    return render_template("result_admin_class.html", klass=klass, students=students)


def _grade_code(score_0_100: float) -> str:
    s = float(score_0_100)
    if 0 <= s <= 39:
        return "F9 (FAIL)"
    if 40 <= s <= 44:
        return "E8 (PASS)"
    if 45 <= s <= 49:
        return "D7 (PASS)"
    if 50 <= s <= 54:
        return "C6 (CREDIT)"
    if 55 <= s <= 59:
        return "C5 (CREDIT)"
    if 60 <= s <= 64:
        return "C4 (CREDIT)"
    if 65 <= s <= 69:
        return "B3 (GOOD)"
    if 70 <= s <= 74:
        return "B2 (VERY GOOD)"
    return "A1 (EXCELLENT)"


@app.route("/result/student/<int:student_id>/report.pdf", endpoint="result_student_report_pdf")
@result_admin_required
def result_student_report_pdf(student_id: int):
    db = get_db()
    student = db.execute(
        """SELECT s.id as student_id, s.full_name, s.class, s.photo_path, u.username
           FROM students s
           JOIN users u ON u.id=s.user_id
           WHERE s.id=?""",
        (student_id,),
    ).fetchone()
    if not student:
        abort(404)

    # Pull all graded attempts for this student (objective + theory => total /100)
    attempts = db.execute(
        """SELECT e.subject, e.title, a.score, COALESCE(a.theory_score,0) as theory_score
           FROM attempts a
           JOIN exams e ON e.id=a.exam_id
           WHERE a.submitted=1 AND a.student_id=?
           ORDER BY COALESCE(e.subject, e.title) COLLATE NOCASE ASC""",
        (student_id,),
    ).fetchall()

    subjects_rows = []
    sn = 1
    for r in attempts:
        subj = (r["subject"] or r["title"] or "General").strip()
        obj = float(r["score"] or 0)
        thy = float(r["theory_score"] or 0)
        total = max(0.0, min(100.0, obj + thy))
        subjects_rows.append(
            {
                "sn": sn,
                "subject": subj,
                "score": total,
                "grade": _grade_code(total),
            }
        )
        sn += 1

    # PDF generation
    buf = io.BytesIO()
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except Exception:
        flash("ReportLab not installed. Run: py -m pip install reportlab", "danger")
        return redirect(url_for("result_class", klass=student["class"]))

    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    logo_path = os.path.join(app.static_folder, "pac_logo.png")
    logo_exists = os.path.exists(logo_path)

    # Header block
    top_y = height - 2.0 * cm
    if logo_exists:
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, (width - 2.2 * cm) / 2, top_y - 1.8 * cm, 2.2 * cm, 2.2 * cm, mask="auto")
        except Exception:
            pass

    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width / 2, top_y - 2.2 * cm, "PAC COLLEGE")
    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, top_y - 2.75 * cm, "FEDERAL HOUSING ESTATE EGBEADA")
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(width / 2, top_y - 3.35 * cm, "MOCK EXAMINATION REPORT CARD")

    # Student photo (right)
    photo_box_w = 3.2 * cm
    photo_box_h = 3.8 * cm
    photo_x = width - 2.0 * cm - photo_box_w
    photo_y = top_y - 7.4 * cm

    c.roundRect(photo_x, photo_y, photo_box_w, photo_box_h, 10, stroke=1, fill=0)
    photo_path_rel = student["photo_path"]
    if photo_path_rel:
        abs_photo = os.path.join(app.static_folder, photo_path_rel)
        if os.path.exists(abs_photo):
            try:
                pimg = ImageReader(abs_photo)
                c.drawImage(pimg, photo_x + 0.15 * cm, photo_y + 0.15 * cm, photo_box_w - 0.3 * cm, photo_box_h - 0.3 * cm, preserveAspectRatio=True, mask="auto")
            except Exception:
                pass

    # Student identity (left)
    left_x = 2.0 * cm
    info_y = top_y - 4.8 * cm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left_x, info_y, f"Name: {student['full_name']}")
    c.setFont("Helvetica", 11)
    c.drawString(left_x, info_y - 0.7 * cm, f"Class: {student['class']}")
    c.setFont("Helvetica", 10)
    c.drawString(left_x, info_y - 1.4 * cm, f"Username: {student['username']}")

    # Table
    table_top = top_y - 8.2 * cm
    c.setFont("Helvetica-Bold", 10)
    c.roundRect(1.7 * cm, table_top, width - 3.4 * cm, height - (table_top + 4.8 * cm), 10, stroke=1, fill=0)

    y = table_top + (height - (table_top + 4.8 * cm)) - 1.0 * cm
    c.drawString(2.2 * cm, y, "S/N")
    c.drawString(3.3 * cm, y, "SUBJECT")
    c.drawString(width - 7.0 * cm, y, "SCORE")
    c.drawString(width - 4.6 * cm, y, "GRADE")
    c.line(2.0 * cm, y - 0.25 * cm, width - 2.0 * cm, y - 0.25 * cm)

    c.setFont("Helvetica", 9)
    y -= 0.8 * cm
    for row in subjects_rows or [{"sn": "-", "subject": "-", "score": 0.0, "grade": "-"}]:
        if y < 4.8 * cm:
            c.showPage()
            y = height - 2.5 * cm

        c.drawString(2.25 * cm, y, str(row["sn"]))
        c.drawString(3.3 * cm, y, str(row["subject"])[:35])
        c.drawRightString(width - 6.2 * cm, y, f"{row['score']:.0f}")
        c.drawString(width - 4.6 * cm, y, str(row["grade"]))
        y -= 0.6 * cm

    # Principal signature line
    sig_y = 3.2 * cm
    c.setFont("Helvetica", 10)
    c.line(2.0 * cm, sig_y, 9.0 * cm, sig_y)
    c.drawString(2.0 * cm, sig_y - 0.45 * cm, "Principal's Signature")

    # Grading box (lower right)
    box_w = 7.2 * cm
    box_h = 3.1 * cm
    box_x = width - 2.0 * cm - box_w
    box_y = 2.2 * cm
    c.roundRect(box_x, box_y, box_w, box_h, 8, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(box_x + 0.4 * cm, box_y + box_h - 0.6 * cm, "GRADE")
    c.drawString(box_x + 3.2 * cm, box_y + box_h - 0.6 * cm, "COMMENT")
    c.setFont("Helvetica", 8)
    lines = [
        ("0-39", "FAIL"),
        ("40-44", "PASS"),
        ("45-49", "PASS"),
        ("50-64", "CREDIT"),
        ("65-69", "GOOD"),
        ("70-74", "VERY GOOD"),
        ("75-100", "EXCELLENT"),
    ]
    yy = box_y + box_h - 1.1 * cm
    for rng, comment in lines:
        c.drawString(box_x + 0.4 * cm, yy, rng)
        c.drawString(box_x + 3.2 * cm, yy, comment)
        yy -= 0.35 * cm

    c.save()

    pdf_bytes = buf.getvalue()
    buf.close()
    safe_name = (student["full_name"] or "student").replace(" ", "_")
    filename = f"PAC_ReportCard_{safe_name}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# -------------------------
# ✅ FIX: MISSING 'questions' ENDPOINT (template expects it)
# -------------------------
@app.route("/exams/<int:exam_id>/questions", endpoint="questions")
@admin_required
def questions(exam_id: int):
    """
    templates_admin/exams.html calls: url_for('questions', exam_id=e.id)
    Admin portal does not manage questions, so we redirect safely.
    """
    flash("Questions are managed in the Teacher Portal (not Admin Portal).", "info")
    return redirect(url_for("exams"))


# -------------------------
# DASHBOARD
# -------------------------
@app.route("/dashboard")
@admin_required
def dashboard():
    db = get_db()

    active_attempts = db.execute(
        """SELECT
                a.id as attempt_id,
                a.started_at,
                a.last_seen,
                s.full_name,
                s.class as student_class,
                e.title as exam_title,
                COALESCE(e.subject,'General') as exam_subject
            FROM attempts a
            JOIN students s ON s.id=a.student_id
            JOIN exams e ON e.id=a.exam_id
            WHERE a.submitted=0
              AND a.last_seen IS NOT NULL
              AND datetime(a.last_seen) >= datetime('now','-2 minutes')
            ORDER BY datetime(a.last_seen) DESC
            LIMIT 50"""
    ).fetchall()

    recent_uploads = db.execute(
        """SELECT id, detail, actor_username, exam_title, created_at
            FROM audit_log
            WHERE actor_role='teacher'
            ORDER BY id DESC
            LIMIT 20"""
    ).fetchall()

    student_events = db.execute(
        """SELECT id, detail, actor_username, exam_title, created_at
            FROM audit_log
            WHERE actor_role='student'
            ORDER BY id DESC
            LIMIT 30"""
    ).fetchall()

    return render_template(
        "dashboard.html",
        active_attempts=active_attempts,
        recent_uploads=recent_uploads,
        student_events=student_events,
    )


# -------------------------
# SUBJECTS (ADMIN add/remove)
# -------------------------
@app.route("/subjects", methods=["GET", "POST"])
@admin_required
def subjects():
    db = get_db()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        action = (request.form.get("action") or "add").strip().lower()

        if action == "add":
            if not name:
                flash("Enter a subject name.", "warning")
                return redirect(url_for("subjects"))
            try:
                db.execute("INSERT INTO subjects (name, is_active) VALUES (?,1)", (name,))
                db.commit()
                log_audit("subject_add", "admin", session.get("user_id"), f"Added subject: {name}")
                flash("Subject added.", "success")
            except Exception:
                flash("Subject already exists or could not be added.", "danger")
            return redirect(url_for("subjects"))

        if action == "remove":
            sid = (request.form.get("subject_id") or "").strip()
            if not sid:
                flash("Select a subject to remove.", "warning")
                return redirect(url_for("subjects"))
            row = db.execute("SELECT name FROM subjects WHERE id=?", (sid,)).fetchone()
            db.execute("DELETE FROM subjects WHERE id=?", (sid,))
            db.commit()
            if row:
                log_audit("subject_remove", "admin", session.get("user_id"), f"Removed subject: {row['name']}")
            flash("Subject removed.", "warning")
            return redirect(url_for("subjects"))

    rows = db.execute("SELECT id, name FROM subjects WHERE is_active=1 ORDER BY name ASC").fetchall()
    return render_template("subjects.html", subjects=rows)


# -------------------------
# USER MANAGEMENT
# -------------------------
@app.route("/users")
@admin_required
def users():
    db = get_db()
    class_filter = (request.args.get("class") or "").strip()

    rows = db.execute(
        """SELECT
            u.id, u.username, u.role, u.created_at,
            s.full_name AS student_name, s.class AS student_class, s.subjects AS student_subjects, s.photo_path AS student_photo,
            t.full_name AS teacher_name,
            a.full_name AS admin_name, a.admin_type AS admin_type
        FROM users u
        LEFT JOIN students s ON s.user_id = u.id
        LEFT JOIN teachers t ON t.user_id = u.id
        LEFT JOIN admins a ON a.user_id = u.id
        ORDER BY u.created_at DESC"""
    ).fetchall()

    users_out = []
    for r in rows:
        if r["role"] == "student" and class_filter:
            if (r["student_class"] or "").strip() != class_filter:
                continue

        display_name = r["student_name"] or r["teacher_name"] or r["admin_name"] or r["username"]
        users_out.append(
            {
                "id": r["id"],
                "username": r["username"],
                "role": r["role"],
                "created_at": r["created_at"],
                "display_name": display_name,
                "student_class": r["student_class"],
                "student_subjects": r["student_subjects"] or "",
                "student_photo": r["student_photo"],
                "admin_type": r["admin_type"] or "super",
            }
        )

    return render_template(
        "users.html",
        users=users_out,
        class_filter=class_filter,
        allowed_classes=ALLOWED_CLASSES,
    )


@app.route("/users/create")
@admin_required
def create_user():
    return render_template("create_user.html")


@app.route("/users/create/student", methods=["GET", "POST"])
@admin_required
def create_student():
    db = get_db()
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        student_class = (request.form.get("class") or "").strip()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        subjects = request.form.getlist("subjects")
        subjects_clean = ",".join([s.strip() for s in subjects if s.strip()])

        photo = request.files.get("photo")

        if not full_name or not student_class or not username or not password or not (photo and photo.filename):
            flash("Full name, class, username, password, and student picture are required.", "danger")
            return redirect(url_for("create_student"))

        if not is_allowed_class(student_class):
            flash("Invalid class. Please select one of the official classes.", "danger")
            return redirect(url_for("create_student"))

        # Create user
        try:
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                (username, generate_password_hash(password), "student"),
            )
            db.commit()
        except Exception:
            flash("Username already exists.", "danger")
            return redirect(url_for("create_student"))

        user_id = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]

        # Save photo
        try:
            uploads_dir = os.path.join(app.static_folder, "uploads", "students", student_class.replace(" ", "_"))
            os.makedirs(uploads_dir, exist_ok=True)

            fname = secure_filename(photo.filename)
            base, ext = os.path.splitext(fname)
            if not ext:
                ext = ".png"

            final = f"{base}_{user_id}{ext}"
            abs_path = os.path.join(uploads_dir, final)
            photo.save(abs_path)

            photo_path = f"uploads/students/{student_class.replace(' ', '_')}/{final}"
        except Exception:
            try:
                db.execute("DELETE FROM users WHERE id=?", (user_id,))
                db.commit()
            except Exception:
                pass
            flash("Failed to save student picture. Please try again with a valid image file.", "danger")
            return redirect(url_for("create_student"))

        # Insert student
        db.execute(
            "INSERT INTO students (user_id, full_name, class, subjects, photo_path) VALUES (?,?,?,?,?)",
            (user_id, full_name, student_class, subjects_clean, photo_path),
        )

        # ✅ ALSO store credential for PDF/CSV export (security note: plaintext stored)
        try:
            batch_id = "manual-" + now().strftime("%Y%m%d%H%M%S")
            db.execute(
                """INSERT INTO student_credentials (user_id, username, password_plain, full_name, class, batch_id)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, username, password, full_name, student_class, batch_id),
            )
        except Exception:
            pass

        db.commit()
        log_audit("student_create", "admin", session.get("user_id"), f"Created student: {full_name} ({student_class})")

        flash("Student created successfully.", "success")
        return redirect(url_for("users"))

    return render_template(
        "create_student.html",
        allowed_classes=ALLOWED_CLASSES,
        subjects_offered=get_subjects_offered(),
    )


@app.route("/users/create/teacher", methods=["GET", "POST"])
@admin_required
def create_teacher():
    db = get_db()
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not full_name or not username or not password:
            flash("Name, username and password are required.", "danger")
            return redirect(url_for("create_teacher"))

        try:
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                (username, generate_password_hash(password), "teacher"),
            )
            db.commit()
        except Exception:
            flash("Username already exists.", "danger")
            return redirect(url_for("create_teacher"))

        user_id = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
        db.execute(
            "INSERT OR REPLACE INTO teachers (user_id, full_name) VALUES (?,?)",
            (user_id, full_name),
        )
        db.commit()
        log_audit("teacher_create", "admin", session.get("user_id"), f"Created teacher: {full_name}")

        flash("Teacher created successfully.", "success")
        return redirect(url_for("users"))

    return render_template("create_teacher.html")


@app.route("/users/create/admin", methods=["GET", "POST"])
@admin_required
def create_admin():
    if session.get("admin_type", "super") != "super":
        flash("Only super admins can create admins.", "danger")
        return redirect(url_for("users"))

    db = get_db()
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        admin_type = (request.form.get("admin_type") or "sub").strip().lower()
        if admin_type not in ("super", "sub"):
            admin_type = "sub"

        if not full_name or not username or not password:
            flash("Name, username and password are required.", "danger")
            return redirect(url_for("create_admin"))

        try:
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                (username, generate_password_hash(password), "admin"),
            )
            db.commit()
        except Exception:
            flash("Username already exists.", "danger")
            return redirect(url_for("create_admin"))

        user_id = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
        db.execute(
            "INSERT OR REPLACE INTO admins (user_id, full_name, admin_type) VALUES (?,?,?)",
            (user_id, full_name, admin_type),
        )
        db.commit()
        log_audit("admin_create", "admin", session.get("user_id"), f"Created admin: {full_name} ({admin_type})")

        flash("Admin created successfully.", "success")
        return redirect(url_for("users"))

    return render_template("create_admin.html")


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    if user_id == session.get("user_id"):
        flash("You cannot delete your own account while logged in.", "danger")
        return redirect(url_for("users"))

    db = get_db()
    u = db.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        flash("User not found.", "warning")
        return redirect(url_for("users"))

    if u["role"] == "admin":
        admin_count = db.execute("SELECT COUNT(1) AS c FROM users WHERE role='admin'").fetchone()["c"]
        if int(admin_count) <= 1:
            flash("You cannot delete the last admin account.", "danger")
            return redirect(url_for("users"))

    _delete_user_cascade(db, user_id)
    db.commit()
    log_audit("user_delete", "admin", session.get("user_id"), f"Deleted {u['role']}: {u['username']}")
    flash(f"Deleted {u['role']}: {u['username']}", "success")
    return redirect(url_for("users"))


# -------------------------
# BULK STUDENTS (CSV)
# -------------------------
@app.route("/students/bulk-upload", methods=["POST"])
@admin_required
def bulk_upload_students():
    db = get_db()
    f = request.files.get("students_csv")
    if not f or f.filename.strip() == "":
        flash("Please choose a CSV file.", "warning")
        return redirect(url_for("users"))
    if not f.filename.lower().endswith(".csv"):
        flash("Invalid file type. Upload a .csv file.", "danger")
        return redirect(url_for("users"))

    students = _parse_students_csv(f.read())
    if not students:
        flash("CSV appears empty or invalid.", "danger")
        return redirect(url_for("users"))

    batch_id = now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)

    created = 0
    skipped = 0
    invalid_class = 0

    for s in students:
        full_name = (s.get("full_name") or "").strip()
        klass = (s.get("class") or "").strip()

        if not full_name:
            skipped += 1
            continue
        if not is_allowed_class(klass):
            invalid_class += 1
            continue

        username = _generate_student_code(db)
        password_plain = _generate_password(10)

        try:
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                (username, generate_password_hash(password_plain), "student"),
            )
            user_id = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            db.execute(
                "INSERT INTO students (user_id, full_name, class, subjects) VALUES (?,?,?,?)",
                (user_id, full_name, klass, (s.get("subjects") or "").strip()),
            )
            db.execute(
                """INSERT INTO student_credentials (user_id, username, password_plain, full_name, class, batch_id)
                    VALUES (?,?,?,?,?,?)""",
                (user_id, username, password_plain, full_name, klass, batch_id),
            )
            created += 1
        except Exception:
            skipped += 1
            continue

    db.commit()
    session["last_student_batch"] = batch_id

    if created:
        flash(f"Uploaded {created} student(s). Download login list now.", "success")
        log_audit("students_bulk_upload", "admin", session.get("user_id"), f"Bulk uploaded {created} student(s)")

    if skipped or invalid_class:
        msg_parts = []
        if skipped:
            msg_parts.append(f"{skipped} blank/duplicate row(s)")
        if invalid_class:
            msg_parts.append(f"{invalid_class} row(s) with invalid class")
        flash("Skipped " + ", ".join(msg_parts) + ".", "warning")

    return redirect(url_for("users"))


@app.route("/students/template.csv")
@admin_required
def download_student_csv_template():
    klass1 = ALLOWED_CLASSES[0] if ALLOWED_CLASSES else "GRADE 9 EMERALD"
    klass2 = ALLOWED_CLASSES[1] if len(ALLOWED_CLASSES) > 1 else klass1
    csv_text = (
        "name,class,subjects\n"
        f'Student One,{klass1},"Mathematics,English Language"\n'
        f"Student Two,{klass2},Biology\n"
    )
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=PAC_students_template.csv"},
    )


# ✅ Credentials CSV: now includes SUBJECTS too
@app.route("/students/credentials.csv")
@admin_required
def download_student_credentials():
    all_flag = (request.args.get("all", "").strip() == "1")
    batch_id = "" if all_flag else (request.args.get("batch_id", "").strip() or session.get("last_student_batch", ""))

    db = get_db()
    if batch_id:
        rows = db.execute(
            """
            SELECT sc.full_name, sc.class, sc.username, sc.password_plain,
                   COALESCE(s.subjects,'') AS subjects
            FROM student_credentials sc
            LEFT JOIN students s ON s.user_id = sc.user_id
            WHERE sc.batch_id=?
            ORDER BY sc.id ASC
            """,
            (batch_id,),
        ).fetchall()
        filename = f"PAC_students_login_{batch_id}.csv"
    else:
        rows = db.execute(
            """
            SELECT sc.full_name, sc.class, sc.username, sc.password_plain,
                   COALESCE(s.subjects,'') AS subjects
            FROM student_credentials sc
            LEFT JOIN students s ON s.user_id = sc.user_id
            ORDER BY sc.id DESC
            """
        ).fetchall()
        filename = "PAC_students_login_all.csv"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["full_name", "class", "username", "password", "subjects"])
    for r in rows:
        writer.writerow([r["full_name"], r["class"], r["username"], r["password_plain"], r["subjects"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ✅ Export FULL student list per class as PDF
# Includes: Full name, Username, Password (latest known), Subjects
# Uses: School logo + school name + class header + watermark on EVERY page
@app.route("/students/list.pdf")
@admin_required
def download_students_pdf():
    klass = (request.args.get("class") or "").strip()
    if not klass:
        flash("Select a class first, then download the PDF.", "warning")
        return redirect(url_for("users"))
    if not is_allowed_class(klass):
        flash("Invalid class selected.", "danger")
        return redirect(url_for("users"))

    db = get_db()

    rows = db.execute(
        """
        SELECT
            s.full_name,
            u.username,
            COALESCE(sc.password_plain, '') AS password_plain,
            COALESCE(s.subjects, '') AS subjects
        FROM students s
        JOIN users u ON u.id = s.user_id
        LEFT JOIN (
            SELECT user_id, password_plain
            FROM student_credentials
            WHERE id IN (SELECT MAX(id) FROM student_credentials GROUP BY user_id)
        ) sc ON sc.user_id = u.id
        WHERE s.class = ?
        ORDER BY s.full_name ASC
        """,
        (klass,),
    ).fetchall()

    buf = io.BytesIO()
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except Exception:
        flash("ReportLab not installed. Run: py -m pip install reportlab", "danger")
        return redirect(url_for("users"))

    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    logo_path = os.path.join(app.static_folder, "pac_logo.png")
    logo_exists = os.path.exists(logo_path)

    def wrap_text(text: str, max_chars: int):
        t = (text or "").strip()
        if not t:
            return ["-"]
        return [t[i : i + max_chars] for i in range(0, len(t), max_chars)]

    def draw_header_and_watermark():
        # Watermark on every page
        if logo_exists:
            try:
                c.saveState()
                if hasattr(c, "setFillAlpha"):
                    c.setFillAlpha(0.08)
                wm = ImageReader(logo_path)
                wm_w = 12 * cm
                wm_h = 12 * cm
                c.drawImage(
                    wm,
                    (width - wm_w) / 2,
                    (height - wm_h) / 2,
                    wm_w,
                    wm_h,
                    mask="auto",
                    preserveAspectRatio=True,
                )
                c.restoreState()
            except Exception:
                pass

        # Header
        y = height - 2.2 * cm
        if logo_exists:
            try:
                img = ImageReader(logo_path)
                c.drawImage(img, 2 * cm, y - 0.8 * cm, 1.6 * cm, 1.6 * cm, mask="auto", preserveAspectRatio=True)
            except Exception:
                pass

        c.setFont("Helvetica-Bold", 16)
        c.drawString(4 * cm, y, SCHOOL_NAME)
        c.setFont("Helvetica", 10)
        c.drawString(4 * cm, y - 0.5 * cm, SCHOOL_ADDRESS)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(2 * cm, y - 1.5 * cm, f"Students List — {klass}")

        # Table header
        y2 = y - 2.3 * cm
        c.setFont("Helvetica-Bold", 9)
        c.drawString(1.3 * cm, y2, "S/N")
        c.drawString(2.4 * cm, y2, "Full Name")
        c.drawString(7.9 * cm, y2, "Username")
        c.drawString(10.6 * cm, y2, "Password")
        c.drawString(13.0 * cm, y2, "Subjects")
        c.line(1.3 * cm, y2 - 0.2 * cm, width - 1.3 * cm, y2 - 0.2 * cm)

        return y2 - 0.7 * cm

    y = draw_header_and_watermark()
    c.setFont("Helvetica", 8.5)

    sn = 1
    for r in rows:
        if y < 2.2 * cm:
            c.showPage()
            y = draw_header_and_watermark()
            c.setFont("Helvetica", 8.5)

        full_name = (r["full_name"] or "").strip()
        username = (r["username"] or "").strip()
        password_plain = (r["password_plain"] or "").strip() or "—"
        subjects = (r["subjects"] or "").strip()

        name_lines = wrap_text(full_name, 26)
        subj_lines = wrap_text(subjects, 28)

        c.drawString(1.3 * cm, y, str(sn))
        c.drawString(2.4 * cm, y, name_lines[0])
        c.drawString(7.9 * cm, y, username[:18])
        c.drawString(10.6 * cm, y, password_plain[:16])
        c.drawString(13.0 * cm, y, subj_lines[0])

        extra = max(len(name_lines), len(subj_lines))
        yy = y
        for i in range(1, extra):
            yy -= 0.45 * cm
            if yy < 2.2 * cm:
                c.showPage()
                yy = draw_header_and_watermark()
                c.setFont("Helvetica", 8.5)

            c.drawString(2.4 * cm, yy, name_lines[i] if i < len(name_lines) else "")
            c.drawString(13.0 * cm, yy, subj_lines[i] if i < len(subj_lines) else "")

        y = yy - 0.55 * cm
        sn += 1

    c.save()
    pdf_bytes = buf.getvalue()
    buf.close()

    filename = f"PAC_{klass.replace(' ', '_')}_students_full_list.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# -------------------------
# EXAMS
# -------------------------
@app.route("/exams")
@admin_required
def exams():
    db = get_db()
    exams = db.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
    return render_template("exams.html", exams=exams)


@app.route("/exams/create", methods=["GET", "POST"])
@admin_required
def create_exam():
    flash("Exams are created by Teachers. Use this page to start/stop exams.", "info")
    return redirect(url_for("exams"))


@app.route("/exams/<int:exam_id>/start", methods=["POST"], endpoint="start_exam")
@admin_required
def start_exam(exam_id: int):
    db = get_db()
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE exams SET is_active=1, start_at=? WHERE id=?", (now_s, exam_id))
    db.commit()
    log_audit("exam_start", "admin", session.get("user_id"), f"Started exam id={exam_id}", exam_id=exam_id)
    flash("Exam started.", "success")
    return redirect(url_for("exams"))


@app.route("/exams/<int:exam_id>/stop", methods=["POST"], endpoint="stop_exam")
@admin_required
def stop_exam(exam_id: int):
    db = get_db()
    db.execute("UPDATE exams SET is_active=0 WHERE id=?", (exam_id,))
    db.commit()
    log_audit("exam_stop", "admin", session.get("user_id"), f"Stopped exam id={exam_id}", exam_id=exam_id)
    flash("Exam stopped.", "warning")
    return redirect(url_for("exams"))


@app.route("/exams/<int:exam_id>/delete", methods=["POST"])
@admin_required
def delete_exam(exam_id: int):
    db = get_db()
    ex = db.execute("SELECT id, title FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not ex:
        flash("Exam not found.", "warning")
        return redirect(url_for("exams"))

    _delete_exam_cascade(db, exam_id)
    db.commit()
    log_audit("exam_delete", "admin", session.get("user_id"), f"Deleted exam: {ex['title']}")
    flash(f"Deleted exam: {ex['title']}", "success")
    return redirect(url_for("exams"))


# -------------------------
# RESULTS
# -------------------------

@app.route("/results")
@admin_required
def results():
    if session.get("admin_type", "super") != "super":
        flash("Sub admins are not allowed to view results.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    subject_filter = (request.args.get("subject") or "").strip()
    class_filter = (request.args.get("class") or "").strip()
    exam_id_filter = (request.args.get("exam_id") or "").strip()

    # Exam options for filters (subject + class)
    exams_q = "SELECT id, title, class, COALESCE(subject,'General') as subject FROM exams ORDER BY id DESC"
    exams = db.execute(exams_q).fetchall()

    # Base query: submitted attempts
    q = """SELECT
                a.id as attempt_id,
                COALESCE(a.objective_score, a.score, 0) as objective_score,
                COALESCE(a.theory_score, 0) as theory_score,
                COALESCE(a.total_score, COALESCE(a.objective_score, a.score, 0) + COALESCE(a.theory_score, 0)) as total_score,
                a.submitted_at,
                COALESCE(a.violations,0) as violations,
                e.id as exam_id,
                e.title as exam_title,
                e.class as exam_class,
                COALESCE(e.subject,'General') as exam_subject,
                u.username as student_username,
                s.full_name
           FROM attempts a
           JOIN exams e ON e.id=a.exam_id
           JOIN students s ON s.id=a.student_id
           JOIN users u ON u.id=s.user_id
           WHERE a.submitted=1"""
    params = []

    if subject_filter:
        q += " AND COALESCE(e.subject,'General')=?"
        params.append(subject_filter)
    if class_filter:
        q += " AND e.class=?"
        params.append(class_filter)
    if exam_id_filter:
        try:
            q += " AND e.id=?"
            params.append(int(exam_id_filter))
        except Exception:
            pass

    q += " ORDER BY datetime(a.submitted_at) DESC"

    rows = db.execute(q, params).fetchall()

    # Subjects/classes for dropdowns
    subjects = db.execute("SELECT DISTINCT COALESCE(subject,'General') as subject FROM exams ORDER BY subject ASC").fetchall()
    classes = db.execute("SELECT DISTINCT class FROM exams ORDER BY class ASC").fetchall()

    return render_template(
        "results.html",
        rows=rows,
        exams=exams,
        subjects=[r["subject"] for r in subjects],
        classes=[r["class"] for r in classes],
        subject_filter=subject_filter,
        class_filter=class_filter,
        exam_id_filter=exam_id_filter,
    )



@app.route("/results/ogr.pdf")
@admin_required
def download_admin_ogr_pdf():
    """Admin OGR PDF: subject + class filter, includes objective/theory/total, class average + remark."""
    if session.get("admin_type", "super") != "super":
        flash("Sub admins are not allowed to view results.", "danger")
        return redirect(url_for("dashboard"))

    subject_filter = (request.args.get("subject") or "").strip()
    class_filter = (request.args.get("class") or "").strip()
    exam_id_filter = (request.args.get("exam_id") or "").strip()

    if not subject_filter or not class_filter:
        flash("Select subject and class first, then download the OGR PDF.", "warning")
        return redirect(url_for("results"))

    db = get_db()

    # Choose the exam (if multiple) - prefer explicit exam_id, else newest exam by id for this subject+class
    exam = None
    if exam_id_filter:
        try:
            exam = db.execute(
                "SELECT id, title, class, COALESCE(subject,'General') as subject FROM exams WHERE id=?",
                (int(exam_id_filter),),
            ).fetchone()
        except Exception:
            exam = None

    if not exam:
        exam = db.execute(
            """SELECT id, title, class, COALESCE(subject,'General') as subject
                 FROM exams
                 WHERE COALESCE(subject,'General')=? AND class=?
                 ORDER BY id DESC
                 LIMIT 1""",
            (subject_filter, class_filter),
        ).fetchone()

    if not exam:
        flash("No exam found for the selected subject/class.", "warning")
        return redirect(url_for("results"))

    exam_id = int(exam["id"])

    # Pull results (alphabetical by student name)
    rows = db.execute(
        """SELECT
                s.full_name,
                COALESCE(a.objective_score, a.score, 0) as objective_score,
                COALESCE(a.theory_score, 0) as theory_score,
                COALESCE(a.total_score, COALESCE(a.objective_score, a.score, 0) + COALESCE(a.theory_score, 0)) as total_score
           FROM attempts a
           JOIN students s ON s.id=a.student_id
           WHERE a.submitted=1 AND a.exam_id=?
           ORDER BY lower(s.full_name) ASC""",
        (exam_id,),
    ).fetchall()

    # Class average (total score)
    totals = [float(r["total_score"] or 0) for r in rows]
    class_avg = round(sum(totals) / len(totals), 2) if totals else 0.0

    def avg_remark(avg: float) -> str:
        if 0 <= avg <= 39:
            return "Failed"
        if 40 <= avg <= 44:
            return "Pass"
        if 45 <= avg <= 64:
            return "Credit"
        if 65 <= avg <= 69:
            return "Good"
        if 70 <= avg <= 74:
            return "Very Good"
        return "Excellent"

    remark = avg_remark(class_avg)

    buf = io.BytesIO()
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except Exception:
        flash("ReportLab not installed. Run: py -m pip install reportlab", "danger")
        return redirect(url_for("results"))

    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    logo_path = os.path.join(app.static_folder, "pac_logo.png")
    logo_exists = os.path.exists(logo_path)

    # Watermark (center)
    if logo_exists:
        try:
            c.saveState()
            if hasattr(c, "setFillAlpha"):
                c.setFillAlpha(0.08)
            wm = ImageReader(logo_path)
            wm_w = 12 * cm
            wm_h = 12 * cm
            c.drawImage(wm, (width - wm_w) / 2, (height - wm_h) / 2, wm_w, wm_h, mask="auto", preserveAspectRatio=True)
            c.restoreState()
        except Exception:
            pass

    # Header (logo top-center)
    y = height - 2.2 * cm
    if logo_exists:
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, (width - 2.0 * cm) / 2, y - 1.2 * cm, 2.0 * cm, 2.0 * cm, mask="auto", preserveAspectRatio=True)
        except Exception:
            pass

    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width / 2, y - 1.6 * cm, SCHOOL_NAME)
    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, y - 2.2 * cm, SCHOOL_ADDRESS)

    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(width / 2, y - 3.0 * cm, f"OFFICIAL OGR ({exam['subject']})")

    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, y - 3.6 * cm, f"Class: {exam['class']}   |   Class Average: {class_avg} ({remark})")

    # Table header
    y_table = y - 4.6 * cm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(2 * cm, y_table, "S/N")
    c.drawString(3.2 * cm, y_table, "Full Name")
    c.drawString(11.3 * cm, y_table, "Objective")
    c.drawString(13.4 * cm, y_table, "Theory")
    c.drawString(15.5 * cm, y_table, "Total")
    c.line(2 * cm, y_table - 0.2 * cm, width - 2 * cm, y_table - 0.2 * cm)

    c.setFont("Helvetica", 9)
    y = y_table - 0.7 * cm
    sn = 1

    def _new_page():
        nonlocal y
        c.showPage()
        # watermark again
        if logo_exists:
            try:
                c.saveState()
                if hasattr(c, "setFillAlpha"):
                    c.setFillAlpha(0.08)
                wm = ImageReader(logo_path)
                wm_w = 12 * cm
                wm_h = 12 * cm
                c.drawImage(wm, (width - wm_w) / 2, (height - wm_h) / 2, wm_w, wm_h, mask="auto", preserveAspectRatio=True)
                c.restoreState()
            except Exception:
                pass
        y = height - 2.5 * cm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(2 * cm, y, "S/N")
        c.drawString(3.2 * cm, y, "Full Name")
        c.drawString(11.3 * cm, y, "Objective")
        c.drawString(13.4 * cm, y, "Theory")
        c.drawString(15.5 * cm, y, "Total")
        c.line(2 * cm, y - 0.2 * cm, width - 2 * cm, y - 0.2 * cm)
        c.setFont("Helvetica", 9)
        y -= 0.7 * cm

    for r in rows:
        if y < 3.5 * cm:
            _new_page()

        c.drawString(2 * cm, y, str(sn))
        c.drawString(3.2 * cm, y, (r["full_name"] or "")[:52])
        c.drawRightString(12.6 * cm, y, f"{float(r['objective_score'] or 0):.0f}")
        c.drawRightString(14.7 * cm, y, f"{float(r['theory_score'] or 0):.0f}")
        c.drawRightString(16.8 * cm, y, f"{float(r['total_score'] or 0):.0f}")

        y -= 0.55 * cm
        sn += 1

    # Signatures
    if y < 5.0 * cm:
        _new_page()

    y -= 0.6 * cm
    c.setFont("Helvetica", 10)
    c.drawString(2 * cm, y, "ICT Director Signature: ____________________________")
    y -= 0.9 * cm
    c.drawString(2 * cm, y, "Assistant ICT Director Signature: ____________________")

    c.showPage()
    c.save()

    pdf_bytes = buf.getvalue()
    buf.close()

    safe_subject = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(exam["subject"]))
    safe_class = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(exam["class"]))
    filename = f"PAC_ADMIN_OGR_{safe_subject}_{safe_class}.pdf"

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

@app.route("/results/download")
@admin_required
def download_results():
    if session.get("admin_type", "super") != "super":
        flash("Sub admins are not allowed to view results.", "danger")
        return redirect(url_for("dashboard"))

    exam_id = request.args.get("exam_id", "").strip()
    db = get_db()

    q = """SELECT e.id as exam_id, e.title, e.class, e.access_code,
                  u.username as student_username, s.full_name, s.class as student_class,
                  a.id as attempt_id,
                  COALESCE(a.objective_score, a.score, 0) as objective_score,
                  COALESCE(a.theory_score, 0) as theory_score,
                  COALESCE(a.total_score, COALESCE(a.objective_score, a.score, 0) + COALESCE(a.theory_score,0)) as total_score,
                  a.submitted_at, COALESCE(a.violations,0) as violations
           FROM attempts a
           JOIN exams e ON e.id=a.exam_id
           JOIN students s ON s.id=a.student_id
           JOIN users u ON u.id=s.user_id
           WHERE a.submitted=1"""
    params: list[int] = []
    if exam_id:
        q += " AND e.id=?"
        params.append(int(exam_id))
    q += " ORDER BY e.id DESC, a.submitted_at DESC"

    rows = db.execute(q, params).fetchall()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(
        [
            "exam_id",
            "exam_title",
            "exam_class",
            "access_code",
            "student_username",
            "student_full_name",
            "student_class",
            "attempt_id",
            "objective_score","theory_score","total_score",
            "submitted_at",
            "violations",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r["exam_id"],
                r["title"],
                r["class"],
                r["access_code"],
                r["student_username"],
                r["full_name"],
                r["student_class"],
                r["attempt_id"],
                r["objective_score"],
                r["theory_score"],
                r["total_score"],
                r["submitted_at"],
                r["violations"],
            ]
        )

    filename = f"PAC_CBT_results_exam_{exam_id}.csv" if exam_id else "PAC_CBT_results_all.csv"
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/results/<int:attempt_id>")
@admin_required
def result_detail(attempt_id: int):
    if session.get("admin_type", "super") != "super":
        flash("Sub admins are not allowed to view results.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    attempt = db.execute(
        """SELECT a.id as attempt_id, a.score, a.submitted_at, COALESCE(a.violations,0) as violations,
                  e.title as exam_title,
                  u.username as student_username, s.full_name
           FROM attempts a
           JOIN exams e ON e.id=a.exam_id
           JOIN students s ON s.id=a.student_id
           JOIN users u ON u.id=s.user_id
           WHERE a.id=?""",
        (attempt_id,),
    ).fetchone()

    if not attempt:
        abort(404)

    answers = db.execute(
        """SELECT q.question_text, q.correct_option, aa.selected_option
           FROM attempt_answers aa
           JOIN questions q ON q.id=aa.question_id
           WHERE aa.attempt_id=?""",
        (attempt_id,),
    ).fetchall()

    logs = db.execute(
        "SELECT violation_type, details, created_at FROM proctor_logs WHERE attempt_id=? ORDER BY id ASC",
        (attempt_id,),
    ).fetchall()

    return render_template("result_detail.html", attempt=attempt, answers=answers, logs=logs)


if __name__ == "__main__":
    ensure_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("ADMIN_PORT", "5002")), debug=True)