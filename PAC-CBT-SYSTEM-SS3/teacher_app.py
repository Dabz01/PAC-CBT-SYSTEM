from __future__ import annotations

import os
import secrets
import io
import re
from datetime import timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, Response
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from common import (
    SCHOOL_NAME,
    SCHOOL_ADDRESS,
    ALLOWED_CLASSES,
    is_allowed_class,
    get_subjects_offered,
    ensure_db,
    get_db,
    parse_dt,
    now,
)

app = Flask(__name__, template_folder="templates_teacher", static_folder="static")
app.secret_key = os.environ.get("EXAMHUB_TEACHER_SECRET", secrets.token_hex(32))

# Teacher portal classes are explicitly limited as requested.
TEACHER_ALLOWED_CLASSES = ("SS3 DIAMOND", "SS3 EMERALD")


def _is_teacher_allowed_class(klass: str) -> bool:
    return (klass or "").strip().upper() in {c.upper() for c in TEACHER_ALLOWED_CLASSES}

UPLOAD_SUBDIR = os.path.join('uploads','questions')
UPLOAD_DIR = os.path.join(app.static_folder, UPLOAD_SUBDIR)
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGE_EXT = {'.png','.jpg','.jpeg','.gif','.webp'}

def teacher_required(f):
    @wraps(f)
    def _w(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "teacher":
            flash("Teacher access required.", "danger")
            return redirect(url_for("logout"))
        return f(*args, **kwargs)
    return _w

def _extract_images_from_paragraph(doc, paragraph):
    """Return a list of (ext, blob) images found in a paragraph (best-effort)."""
    images = []
    try:
        blips = paragraph._element.xpath('.//a:blip')
    except Exception:
        blips = []
    for blip in blips:
        rid = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
        if not rid:
            continue
        part = doc.part.related_parts.get(rid)
        if not part:
            continue
        blob = part.blob
        # Try to infer extension from content type
        ext = ''
        ctype = getattr(part, 'content_type', '') or ''
        if 'png' in ctype:
            ext = '.png'
        elif 'jpeg' in ctype or 'jpg' in ctype:
            ext = '.jpg'
        elif 'gif' in ctype:
            ext = '.gif'
        elif 'webp' in ctype:
            ext = '.webp'
        else:
            ext = '.png'
        images.append((ext, blob))
    return images


def _save_docx_image(img_tuple):
    """Save a (ext, blob) image extracted from a DOCX question block.
    Returns a relative static path like 'uploads/questions/<file>' or None.
    """
    if not img_tuple:
        return None
    try:
        ext, blob = img_tuple
    except Exception:
        return None

    ext = (ext or "").lower().strip()
    if not ext.startswith("."):
        ext = "." + ext if ext else ".png"
    if ext not in ALLOWED_IMAGE_EXT:
        ext = ".png"

    if not blob:
        return None

    fname = f"qimg_{secrets.token_hex(12)}{ext}"
    abs_path = os.path.join(UPLOAD_DIR, fname)
    try:
        with open(abs_path, "wb") as f:
            f.write(blob)
    except Exception:
        return None

    rel = os.path.join(UPLOAD_SUBDIR, fname).replace("\\", "/")
    return rel

def parse_docx_questions(docx_bytes: bytes):
    """
    Parse a .docx using a strict, teacher-friendly format.

    Expected per-question block (repeat):

    QUESTION 1:
    Question text... (images allowed here)

    A. option
    B. option
    C. option
    D. option

    ANSWER: B
    MARKS: 2
    ---

    Notes:
    - If a question contains an image, the first image found in that question block is attached to the question.
    - Images are saved into static/uploads/questions/ and referenced by the question row.
    """
    try:
        from docx import Document  # python-docx
    except Exception as e:
        raise RuntimeError("python-docx is not installed. Run: pip install python-docx") from e

    doc = Document(io.BytesIO(docx_bytes))

    # Build blocks separated by '---' lines, capturing images per block.
    blocks = []
    cur_lines = []
    cur_images = []

    for p in doc.paragraphs:
        t = (p.text or "").strip()
        imgs = _extract_images_from_paragraph(doc, p)
        if imgs:
            cur_images.extend(imgs)

        if t == "":
            continue

        if t.strip() == "---":
            if cur_lines:
                blocks.append({"text": "\n".join(cur_lines).strip(), "images": cur_images[:]})
            cur_lines = []
            cur_images = []
            continue

        cur_lines.append(t)

    if cur_lines:
        blocks.append({"text": "\n".join(cur_lines).strip(), "images": cur_images[:]})

    # If no explicit separators, fallback to splitting on QUESTION headers.
    if len(blocks) == 0:
        joined = "\n".join([(p.text or '').strip() for p in doc.paragraphs if (p.text or '').strip()])
        parts = re.split(r"(?im)^QUESTION\s+\d+\s*:\s*", joined)
        headers = re.findall(r"(?im)^QUESTION\s+\d+\s*:\s*", joined)
        for i, part in enumerate(parts[1:]):
            blocks.append({"text": (headers[i].strip() + "\n" + part).strip(), "images": []})
    last_section_title = None
    last_section_instructions = None

    results = []
    for idx, b in enumerate(blocks, start=1):
        raw = b["text"]

        

        # Skip template/sample blocks if someone uploads the template document directly
        uraw = (raw or "").upper()
        if "CBT QUESTION UPLOAD TEMPLATE" in uraw or "EXAMPLE STRUCTURE" in uraw or "INSTRUCTION BLOCK EXAMPLE" in uraw:
            continue
# Extract question number if present
        mnum = re.search(r"(?im)^QUESTION\s+(\d+)\s*:\s*(.*)$", raw)
        qnum = int(mnum.group(1)) if mnum else None

        # Remove QUESTION header
        content = raw
        if mnum:
            content = re.sub(r"(?im)^QUESTION\s+\d+\s*:\s*", "", content, count=1).strip()

        # Optional section header/instructions (can be used to group objective questions without changing numbering)
        msec = re.search(r"(?im)^SECTION\s*:\s*(.+)$", content)
        mins = re.search(r"(?im)^INSTRUCTIONS\s*:\s*(.+)$", content)
        section_title = msec.group(1).strip() if msec else None
        section_instructions = mins.group(1).strip() if mins else None

        # Carry forward the last seen SECTION/INSTRUCTIONS if not provided in this block
        if section_title is None:
            section_title = last_section_title
        else:
            last_section_title = section_title

        if section_instructions is None:
            section_instructions = last_section_instructions
        else:
            last_section_instructions = section_instructions

        # Remove SECTION/INSTRUCTIONS lines
        content = re.sub(r"(?im)^SECTION\s*:\s*.+$", "", content).strip()
        content = re.sub(r"(?im)^INSTRUCTIONS\s*:\s*.+$", "", content).strip()

        # ANSWER and MARKS
        mans = re.search(r"(?im)^ANSWER\s*:\s*([ABCD])\s*$", content)
        mmarks = re.search(r"(?im)^MARKS\s*:\s*(\d+)\s*$", content)

        correct = mans.group(1).upper() if mans else None
        marks = int(mmarks.group(1)) if mmarks else 1

        # Remove ANSWER/MARKS lines
        content_wo = re.sub(r"(?im)^ANSWER\s*:\s*[ABCD]\s*$", "", content).strip()
        content_wo = re.sub(r"(?im)^MARKS\s*:\s*\d+\s*$", "", content_wo).strip()

        # Options
        opt = {}
        for letter in ["A", "B", "C", "D"]:
            mm = re.search(rf"(?im)^{letter}[\.|\)]\s*(.+)$", content_wo)
            opt[letter] = mm.group(1).strip() if mm else None

        # Stem
        stem = content_wo
        ma = re.search(r"(?im)^A[\.|\)]\s*", content_wo)
        if ma:
            stem = content_wo[: ma.start()].strip()

        errors = []
        if not stem:
            errors.append("Missing question text.")
        if any(opt[l] is None for l in ["A", "B", "C", "D"]):
            errors.append("Missing one or more options (A–D). Use lines like 'A. ...'.")
        if correct is None:
            errors.append("Missing ANSWER line (ANSWER: A/B/C/D).")
        if correct and correct not in ("A", "B", "C", "D"):
            errors.append("Invalid ANSWER value. Must be A, B, C, or D.")

        img = b.get("images") or []
        first_img = img[0] if img else None  # (ext, blob)

        results.append(
            {
                "qnum": qnum,
                "stem": stem,
                "options": opt,
                "correct": correct,
                "marks": marks,
                "image": first_img,
                "errors": errors,
                "section_title": section_title,
                "section_instructions": section_instructions,
            }
        )

    return results


@app.context_processor
def inject_brand():
    return {"SCHOOL_NAME": SCHOOL_NAME, "SCHOOL_ADDRESS": SCHOOL_ADDRESS, "PORTAL_NAME": "Teacher Portal"}

@app.route("/")
def home():
    if session.get("user_id") and session.get("role") == "teacher":
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("login"))
        if user["role"] != "teacher":
            flash("This login is for TEACHERS only.", "danger")
            return redirect(url_for("login"))
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/dashboard")
@teacher_required
def dashboard():
    """Teacher welcome page."""
    db = get_db()
    counts = db.execute(
        """SELECT
                COUNT(1) AS total,
                SUM(CASE WHEN COALESCE(is_active,0)=1 THEN 1 ELSE 0 END) AS active
            FROM exams
            WHERE created_by=?""",
        (session["user_id"],),
    ).fetchone()
    total = int(counts["total"] or 0) if counts else 0
    active = int(counts["active"] or 0) if counts else 0
    return render_template("dashboard.html", total_exams=total, active_exams=active)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/exams")
@teacher_required
def exams():
    db = get_db()
    exams = db.execute("SELECT * FROM exams WHERE created_by=? ORDER BY id DESC", (session["user_id"],)).fetchall()
    return render_template("exams.html", exams=exams)




@app.route("/exams/<int:exam_id>/theory", methods=["GET", "POST"], endpoint="theory_scores")
@teacher_required
def theory_scores(exam_id: int):
    """Teacher enters handwritten theory score after marking scripts."""
    db = get_db()

    exam = db.execute(
        "SELECT id, title, class, created_by FROM exams WHERE id=?",
        (exam_id,),
    ).fetchone()
    if not exam or int(exam["created_by"]) != int(session.get("user_id")):
        abort(404)

    # Ensure column exists (older DB)
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(attempts)").fetchall()]
        if "theory_score" not in cols:
            db.execute("ALTER TABLE attempts ADD COLUMN theory_score INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception:
        pass

    if request.method == "POST":
        updated = 0
        attempt_rows = db.execute(
            "SELECT id FROM attempts WHERE exam_id=? AND submitted=1",
            (exam_id,),
        ).fetchall()

        for ar in attempt_rows:
            aid = int(ar["id"])
            key = f"theory_{aid}"
            if key not in request.form:
                continue
            raw = (request.form.get(key) or "").strip()
            try:
                val = int(float(raw)) if raw != "" else 0
            except Exception:
                val = 0
            # clamp 0..50 (CBT is 50%)
            if val < 0:
                val = 0
            if val > 50:
                val = 50
            db.execute("UPDATE attempts SET theory_score=? WHERE id=?", (val, aid))
            updated += 1

        db.commit()
        flash("Theory scores saved." if updated else "No scores updated.", "success" if updated else "info")
        return redirect(url_for("theory_scores", exam_id=exam_id))

    students = db.execute(
        """SELECT a.id as attempt_id,
                  COALESCE(a.score,0) as objective_score,
                  COALESCE(a.theory_score,0) as theory_score,
                  s.full_name
           FROM attempts a
           JOIN students s ON s.id=a.student_id
           WHERE a.exam_id=? AND a.submitted=1
           ORDER BY s.full_name ASC""",
        (exam_id,),
    ).fetchall()

    return render_template("theory_scores.html", exam=exam, students=students)

@app.route("/exams/<int:exam_id>/report.pdf")
@teacher_required
def download_exam_report_pdf(exam_id: int):
    """Download an OGR-style PDF report sheet for a given exam."""
    db = get_db()

    exam = db.execute(
        "SELECT id, title, subject, class FROM exams WHERE id=? AND created_by=?",
        (exam_id, session["user_id"]),
    ).fetchone()
    if not exam:
        flash("Exam not found (or you do not own it).", "warning")
        return redirect(url_for("exams"))

    rows = db.execute(
        """SELECT s.full_name,
                  COALESCE(a.score,0) as objective_score,
                  COALESCE(a.theory_score,0) as theory_score
           FROM attempts a
           JOIN students s ON s.id=a.student_id
           WHERE a.submitted=1 AND a.exam_id=?
           ORDER BY s.full_name COLLATE NOCASE ASC""",
        (exam_id,),
    ).fetchall()

    buf = io.BytesIO()
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except Exception:
        flash("ReportLab not installed. Run: py -m pip install reportlab", "danger")
        return redirect(url_for("exams"))

    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    logo_path = os.path.join(app.static_folder, "pac_logo.png")
    logo_exists = os.path.exists(logo_path)

    # Watermark
    if logo_exists:
        try:
            c.saveState()
            if hasattr(c, "setFillAlpha"):
                c.setFillAlpha(0.07)
            wm = ImageReader(logo_path)
            wm_w = 13 * cm
            wm_h = 13 * cm
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

    # Header: logo top-center
    top_y = height - 2.2 * cm
    if logo_exists:
        try:
            img = ImageReader(logo_path)
            img_w = 2.2 * cm
            img_h = 2.2 * cm
            c.drawImage(img, (width - img_w) / 2, top_y - img_h + 0.2 * cm, img_w, img_h, mask="auto")
        except Exception:
            pass

    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width / 2, top_y - 2.4 * cm, "PAC COLLEGE- FEDERAL HOUSING ESTATE EGBEADA")

    subject_name = (exam["subject"] or exam["title"] or "").strip()
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(width / 2, top_y - 3.2 * cm, f"OFFICIAL OGR ({subject_name})")

    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, top_y - 3.9 * cm, f"Class: {exam['class']}")

    # Table
    y = top_y - 5.0 * cm
    left = 2.0 * cm
    right = width - 2.0 * cm

    c.setFont("Helvetica-Bold", 10)
    c.line(left, y + 0.4 * cm, right, y + 0.4 * cm)
    c.drawString(left, y, "S/N")
    c.drawString(left + 1.4 * cm, y, "FULL NAME")
    c.drawRightString(right - 4.2 * cm, y, "OBJECTIVE")
    c.drawRightString(right - 2.1 * cm, y, "THEORY")
    c.drawRightString(right, y, "TOTAL")
    c.line(left, y - 0.2 * cm, right, y - 0.2 * cm)

    c.setFont("Helvetica", 10)
    y -= 0.8 * cm
    sn = 1
    for r in rows:
        if y < 3.5 * cm:
            c.showPage()
            y = height - 3.0 * cm
            # light watermark again on new page
            if logo_exists:
                try:
                    c.saveState()
                    if hasattr(c, "setFillAlpha"):
                        c.setFillAlpha(0.07)
                    wm = ImageReader(logo_path)
                    wm_w = 13 * cm
                    wm_h = 13 * cm
                    c.drawImage(wm, (width - wm_w) / 2, (height - wm_h) / 2, wm_w, wm_h, mask="auto")
                    c.restoreState()
                except Exception:
                    pass

            c.setFont("Helvetica-Bold", 10)
            c.drawString(left, y, "S/N")
            c.drawString(left + 1.4 * cm, y, "FULL NAME")
            c.drawRightString(right - 4.2 * cm, y, "OBJECTIVE")
            c.drawRightString(right - 2.1 * cm, y, "THEORY")
            c.drawRightString(right, y, "TOTAL")
            c.line(left, y - 0.2 * cm, right, y - 0.2 * cm)
            c.setFont("Helvetica", 10)
            y -= 0.8 * cm

        name = (r["full_name"] or "").strip()
        obj = int(r["objective_score"] or 0)
        th = int(r["theory_score"] or 0)
        total = obj + th

        c.drawString(left, y, str(sn))
        c.drawString(left + 1.4 * cm, y, name[:55])
        c.drawRightString(right - 4.2 * cm, y, str(obj))
        c.drawRightString(right - 2.1 * cm, y, str(th))
        c.drawRightString(right, y, str(total))
        y -= 0.65 * cm
        sn += 1

    # Signature line
    if y < 4.5 * cm:
        c.showPage()
        y = height - 4.0 * cm
    c.setFont("Helvetica", 10)
    c.drawString(left, y - 1.0 * cm, "Teacher's Signature:")
    c.line(left + 4.0 * cm, y - 1.05 * cm, left + 12.0 * cm, y - 1.05 * cm)

    c.save()
    pdf_bytes = buf.getvalue()
    buf.close()

    safe_subject = "_".join((subject_name or "EXAM").split())
    filename = f"OGR_{safe_subject}_{(exam['class'] or '').replace(' ', '_')}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

@app.route("/exams/create", methods=["GET","POST"])
@teacher_required
def create_exam():
    db = get_db()
    if request.method == "POST":
        # In this system, the "Title" is the subject (dropdown from Admin-managed subjects).
        title = (request.form.get("title") or "").strip()
        exam_class = (request.form.get("class") or "").strip()

        # Teachers select ONLY the exam DATE (no start time).
        exam_date = request.form.get("exam_date", "").strip()  # YYYY-MM-DD from <input type="date">

        duration = int(request.form.get("duration_minutes", "30"))

        # Access code is not required (students select exams from timetable). We keep a hidden value in DB
        # for backward compatibility with any legacy flows.
        access_code = secrets.token_hex(3).upper()

        if not title:
            flash("Please select a subject (Title).", "danger")
            return redirect(url_for("create_exam"))
        if not exam_date:
            flash("Exam date is required.", "danger")
            return redirect(url_for("create_exam"))
        if not _is_teacher_allowed_class(exam_class):
            flash("Invalid class. Please select SS3 DIAMOND or SS3 EMERALD.", "danger")
            return redirect(url_for("create_exam"))

        # Subject column also mirrors the selected title for student subject-based filtering.
        subject = title

        # Store a placeholder start_at (midnight) at creation time.
        # Admin will set the real start_at when the exam is started.
        start_at = f"{exam_date} 00:00"

        db.execute(
            "INSERT INTO exams (title, class, subject, start_at, duration_minutes, access_code, is_active, created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (title, exam_class, subject, start_at, duration, access_code, 0, session["user_id"]),
        )
        db.commit()
        flash("Exam created. Admin will start/stop it from the Admin portal.", "success")
        return redirect(url_for("exams"))

    return render_template(
        "create_exam.html",
        subjects_offered=get_subjects_offered(),
        allowed_classes=TEACHER_ALLOWED_CLASSES,
    )

@app.route("/exams/<int:exam_id>/questions", methods=["GET","POST"])
@teacher_required
def questions(exam_id: int):
    db = get_db()
    exam = db.execute("SELECT * FROM exams WHERE id=? AND created_by=?", (exam_id, session["user_id"])).fetchone()
    if not exam:
        abort(404)

    if request.method == "POST":
        text = request.form.get("question_text","").strip()
        a = request.form.get("option_a","").strip()
        b = request.form.get("option_b","").strip()
        c = request.form.get("option_c","").strip()
        d = request.form.get("option_d","").strip()
        correct = request.form.get("correct_option","").strip().upper()
        marks = int(request.form.get("marks","1"))
        image_file = request.files.get('question_image')
        image_path = None
        if image_file and image_file.filename:
            ext = os.path.splitext(image_file.filename)[1].lower()
            if ext not in ALLOWED_IMAGE_EXT:
                flash('Invalid image type. Use PNG/JPG/JPEG/GIF/WEBP.', 'danger')
                return redirect(url_for('questions', exam_id=exam_id))
            safe = secure_filename(image_file.filename)
            unique = f"{secrets.token_hex(8)}{ext}"
            save_path = os.path.join(UPLOAD_DIR, unique)
            image_file.save(save_path)
            image_path = os.path.join(UPLOAD_SUBDIR, unique).replace('\\','/')

        if not text or correct not in ("A","B","C","D"):
            flash("Question text and correct option are required.", "danger")
            return redirect(url_for("questions", exam_id=exam_id))

        db.execute(
            """INSERT INTO questions (exam_id, question_text, image_path, option_a, option_b, option_c, option_d, correct_option, marks, section_title, section_instructions)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (exam_id, text, image_path, a, b, c, d, correct, marks, request.form.get('section_title') or None, request.form.get('section_instructions') or None)
        )
        db.commit()
        flash("Question added.", "success")
        return redirect(url_for("questions", exam_id=exam_id))

    qs = db.execute("SELECT * FROM questions WHERE exam_id=? ORDER BY id ASC", (exam_id,)).fetchall()
    return render_template("questions.html", exam=exam, questions=qs)

@app.route("/exams/<int:exam_id>/questions/upload-docx", methods=["POST"])
@teacher_required
def upload_questions_docx(exam_id: int):
    db = get_db()
    exam = db.execute("SELECT * FROM exams WHERE id=? AND created_by=?", (exam_id, session["user_id"])).fetchone()
    if not exam:
        abort(404)

    f = request.files.get("docx_file")
    if not f or f.filename.strip() == "":
        flash("Please choose a .docx file to upload.", "warning")
        return redirect(url_for("questions", exam_id=exam_id))

    filename = f.filename.lower()
    if not filename.endswith(".docx"):
        flash("Invalid file type. Please upload a Microsoft Word .docx file.", "danger")
        return redirect(url_for("questions", exam_id=exam_id))

    data = f.read()
    try:
        parsed = parse_docx_questions(data)
    except RuntimeError as e:
        flash(str(e), "danger")
        return redirect(url_for("questions", exam_id=exam_id))
    except Exception as e:
        flash(f"Could not read the Word file: {e}", "danger")
        return redirect(url_for("questions", exam_id=exam_id))

    inserted = 0
    failures = []

    for idx, item in enumerate(parsed, start=1):
        qlabel = item["qnum"] if item["qnum"] is not None else idx
        if item["errors"]:
            failures.append((qlabel, "; ".join(item["errors"])))
            continue

        opt = item["options"]
        db.execute(
            """INSERT INTO questions (exam_id, question_text, image_path, option_a, option_b, option_c, option_d, correct_option, marks, section_title, section_instructions)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (exam_id, item['stem'], _save_docx_image(item.get('image')), opt['A'], opt['B'], opt['C'], opt['D'], item['correct'], int(item['marks']), item.get('section_title'), item.get('section_instructions'))
        )
        inserted += 1

    db.commit()

    if inserted:
        flash(f"Uploaded successfully: {inserted} question(s) added.", "success")

    if failures:
        # Notify teacher and leave them to fill missing manually
        preview = "; ".join([f"Q{n}: {msg}" for n, msg in failures[:5]])
        more = "" if len(failures) <= 5 else f" (+{len(failures)-5} more)"
        flash(
            "Some questions were not uploaded. Please fix them in the Word file or add them manually. "
            + preview + more,
            "warning",
        )

    if not inserted and failures:
        flash("No questions were uploaded from this file due to formatting errors.", "danger")

    return redirect(url_for("questions", exam_id=exam_id))

if __name__ == "__main__":
    ensure_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("TEACHER_PORT","5001")), debug=True)
