from __future__ import annotations

import os
import sqlite3
import re
from datetime import datetime, timedelta
from typing import Tuple, List, Optional

from werkzeug.security import generate_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))

SCHOOL_NAME = "PAC COLLEGE"
SCHOOL_ADDRESS = "FEDERAL HOUSING ESTATE EGBEADA"

ALLOWED_CLASSES = [
    "SS3 EMERALD",
    "SS3 DIAMOND",
]

DEFAULT_SUBJECTS_OFFERED = [
    "English Language",
    "Mathematics",
    "Biology",
    "Chemistry",
    "Physics",
    "Economics",
    "Government",
    "Literature in English",
    "Christian Religious Studies",
    "Civic Education",
    "Computer Studies",
    "Agricultural Science",
    "Geography",
]

def normalize_class(value: str) -> str:
    return (value or "").strip()

def is_allowed_class(value: str) -> bool:
    return normalize_class(value) in ALLOWED_CLASSES

def parse_subjects(value: str):
    if not value:
        return set()
    parts = re.split(r"[;,]+", value)
    return set(p.strip().lower() for p in parts if p.strip())

DB_PATH = os.environ.get("EXAMHUB_DB_PATH", os.path.join(APP_DIR, "examhub.db"))

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON;")
    return db


def init_db():
    """
    Creates the database using schema.sql first,
    then runs migrations/extra tables,
    then seeds default admin.
    """
    db = get_db()

    # 1) Create base schema (must include users/students/exams/etc)
    schema_path = os.path.join(APP_DIR, "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        db.executescript(f.read())
    db.commit()

    # 2) Run migrations (creates teachers/admins/subjects/audit_log, etc)
    db.close()
    migrate_db()

    # 3) Seed default admin if none exists
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            ("admin", generate_password_hash("admin123"), "admin"),
        )
        db.commit()
    db.close()


def ensure_db():
    if not os.path.exists(DB_PATH):
        init_db()
    migrate_db()


def migrate_db():
    """Apply lightweight migrations for existing databases."""
    db = get_db()

    # -------------------------
    # ✅ Ensure profile tables exist (fixes missing teachers/admins tables)
    # -------------------------
    db.execute(
        """CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );"""
    )

    db.execute(
        """CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            full_name TEXT,
            admin_type TEXT NOT NULL DEFAULT 'super' CHECK(admin_type IN ('super','sub')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );"""
    )
    db.commit()

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

    # Student archive flag
    try:
        db.execute("ALTER TABLE students ADD COLUMN is_archived INTEGER DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass

    # Student subjects column
    try:
        db.execute("ALTER TABLE students ADD COLUMN subjects TEXT DEFAULT ''")
        db.commit()
    except sqlite3.OperationalError:
        pass

    # Student photo path
    try:
        db.execute("ALTER TABLE students ADD COLUMN photo_path TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass

    # Add subject column to exams
    try:
        db.execute("ALTER TABLE exams ADD COLUMN subject TEXT NOT NULL DEFAULT 'General'")
        db.commit()
    except sqlite3.OperationalError:
        pass

    # Add optional image_path to questions
    try:
        db.execute("ALTER TABLE questions ADD COLUMN image_path TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass


    # Section headers/instructions for question sets
    try:
        db.execute("ALTER TABLE questions ADD COLUMN section_title TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass

    try:
        db.execute("ALTER TABLE questions ADD COLUMN section_instructions TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass
    # Track active attempts (student heartbeat)
    try:
        db.execute("ALTER TABLE attempts ADD COLUMN last_seen TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass

    # Student credential exports
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
    db.commit()

    # Admin dashboard activity feed
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
        )"""
    )
    db.commit()

    # Subjects management
    db.execute(
        """CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    db.commit()

    # Seed subjects if empty
    row = db.execute("SELECT COUNT(1) AS c FROM subjects").fetchone()
    if int(row["c"] or 0) == 0:
        for s in DEFAULT_SUBJECTS_OFFERED:
            try:
                db.execute("INSERT INTO subjects (name, is_active) VALUES (?,1)", (s,))
            except sqlite3.IntegrityError:
                pass
        db.commit()

    db.close()


def get_subjects_offered(db: Optional[sqlite3.Connection] = None) -> List[str]:
    close_after = False
    if db is None:
        db = get_db()
        close_after = True
    try:
        rows = db.execute("SELECT name FROM subjects WHERE is_active=1 ORDER BY name ASC").fetchall()
        subjects = [r["name"] for r in rows] if rows else list(DEFAULT_SUBJECTS_OFFERED)
        return subjects
    except Exception:
        return list(DEFAULT_SUBJECTS_OFFERED)
    finally:
        if close_after:
            try:
                db.close()
            except Exception:
                pass


# Backwards-compat for older code/templates
SUBJECTS_OFFERED = DEFAULT_SUBJECTS_OFFERED


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M")

def now() -> datetime:
    return datetime.now()

def exam_window(exam_row) -> Tuple[datetime, datetime]:
    start = datetime.fromisoformat(exam_row["start_at"])
    end = start + timedelta(minutes=int(exam_row["duration_minutes"]))
    return start, end

def exam_is_active(exam_row) -> bool:
    if int(exam_row["is_active"]) != 1:
        return False
    start, end = exam_window(exam_row)
    return start <= now() <= end

def get_student_profile(user_id: int):
    db = get_db()
    try:
        return db.execute("SELECT * FROM students WHERE user_id=?", (user_id,)).fetchone()
    finally:
        try:
            db.close()
        except Exception:
            pass
