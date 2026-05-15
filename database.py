"""
database.py — Magic Auto Center data layer
"""
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "magic_auto.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS employees (
                telegram_id   INTEGER PRIMARY KEY,
                name          TEXT NOT NULL,
                registered_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id         TEXT PRIMARY KEY,
                car        TEXT NOT NULL,
                plate      TEXT DEFAULT '',
                client     TEXT DEFAULT '',
                works      TEXT DEFAULT '',
                status     TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id           TEXT    NOT NULL,
                employee_id      INTEGER NOT NULL,
                start_time       TEXT    NOT NULL,
                end_time         TEXT,
                duration_minutes INTEGER,
                auto_closed      INTEGER DEFAULT 0,
                FOREIGN KEY (job_id)      REFERENCES jobs(id),
                FOREIGN KEY (employee_id) REFERENCES employees(telegram_id)
            );
        """)
        # Add auto_closed column if upgrading from older version
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN auto_closed INTEGER DEFAULT 0")
        except Exception:
            pass


# ── Employees ─────────────────────────────────────────────────────────────────

def get_employee(telegram_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM employees WHERE telegram_id=?", (telegram_id,)
        ).fetchone()


def register_employee(telegram_id, name):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO employees (telegram_id, name) VALUES (?,?)",
            (telegram_id, name)
        )


def get_all_employees():
    with get_db() as conn:
        return conn.execute("SELECT * FROM employees ORDER BY name").fetchall()


def delete_employee(telegram_id):
    with get_db() as conn:
        conn.execute("DELETE FROM employees WHERE telegram_id=?", (telegram_id,))


# ── Jobs ──────────────────────────────────────────────────────────────────────

def get_job(job_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def get_all_jobs(status="active"):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC", (status,)
        ).fetchall()


def add_job(job_id, car, plate="", client="", works=""):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,'active',datetime('now'))",
            (job_id, car, plate, client, works)
        )


def close_job(job_id):
    with get_db() as conn:
        conn.execute("UPDATE jobs SET status='closed' WHERE id=?", (job_id,))


# ── Sessions ──────────────────────────────────────────────────────────────────

def get_open_session(job_id, emp_id):
    """Open session for this employee on this specific job."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE job_id=? AND employee_id=? AND end_time IS NULL",
            (job_id, emp_id)
        ).fetchone()


def get_employee_open_session_any(emp_id, exclude_job_id=None):
    """Any open session for this employee, optionally ignoring one job."""
    with get_db() as conn:
        if exclude_job_id:
            return conn.execute(
                """SELECT s.*, j.car, j.plate
                   FROM sessions s JOIN jobs j ON j.id = s.job_id
                   WHERE s.employee_id=? AND s.end_time IS NULL AND s.job_id != ?""",
                (emp_id, exclude_job_id)
            ).fetchone()
        return conn.execute(
            """SELECT s.*, j.car, j.plate
               FROM sessions s JOIN jobs j ON j.id = s.job_id
               WHERE s.employee_id=? AND s.end_time IS NULL""",
            (emp_id,)
        ).fetchone()


def get_all_open_sessions():
    """All open sessions across all employees — for admin view and auto-close."""
    with get_db() as conn:
        return conn.execute("""
            SELECT s.*, e.name AS emp_name, e.telegram_id,
                   j.car, j.plate, j.id AS job_id
            FROM   sessions  s
            JOIN   employees e ON e.telegram_id = s.employee_id
            JOIN   jobs      j ON j.id = s.job_id
            WHERE  s.end_time IS NULL
            ORDER  BY s.start_time
        """).fetchall()


def open_session(job_id, emp_id):
    now = _now()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (job_id, employee_id, start_time) VALUES (?,?,?)",
            (job_id, emp_id, now)
        )
    return now


def close_session(session_id, start_str, end_str=None, auto=False):
    """Close a session. end_str defaults to now if not provided."""
    if end_str is None:
        end_dt = datetime.utcnow()
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    else:
        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")

    start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
    minutes  = max(0, int((end_dt - start_dt).total_seconds() / 60))

    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET end_time=?, duration_minutes=?, auto_closed=? WHERE id=?",
            (end_str, minutes, 1 if auto else 0, session_id)
        )
    return end_str, minutes


def auto_close_all_open_sessions(shop_close_hour_utc: int):
    """
    Auto-close every open session at shop closing time.
    Returns list of closed sessions for notification.
    """
    open_sessions = get_all_open_sessions()
    closed = []
    today = datetime.utcnow().strftime("%Y-%m-%d")
    end_str = f"{today} {shop_close_hour_utc:02d}:00:00"

    for s in open_sessions:
        _, minutes = close_session(s["id"], s["start_time"], end_str=end_str, auto=True)
        closed.append({
            "emp_name":  s["emp_name"],
            "telegram_id": s["telegram_id"],
            "car":       s["car"],
            "job_id":    s["job_id"],
            "start_time": s["start_time"],
            "minutes":   minutes,
        })
    return closed


# ── Reports ───────────────────────────────────────────────────────────────────

def get_report_data(days=1):
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute("""
            SELECT s.*, e.name AS emp_name, j.car, j.plate, j.id AS job_id
            FROM   sessions  s
            JOIN   employees e ON e.telegram_id = s.employee_id
            JOIN   jobs      j ON j.id = s.job_id
            WHERE  s.start_time >= ? AND s.end_time IS NOT NULL
            ORDER  BY j.id, s.start_time
        """, (since,)).fetchall()

    by_job = {}
    for r in rows:
        jid = r["job_id"]
        by_job.setdefault(jid, {"car": r["car"], "plate": r["plate"], "rows": [], "total": 0})
        by_job[jid]["rows"].append(r)
        by_job[jid]["total"] += r["duration_minutes"] or 0
    return by_job


def get_job_total_minutes(job_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_minutes),0) AS total FROM sessions WHERE job_id=?",
            (job_id,)
        ).fetchone()
    return row["total"] if row else 0


def has_active_sessions(job_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sessions WHERE job_id=? AND end_time IS NULL",
            (job_id,)
        ).fetchone()
    return (row["cnt"] > 0) if row else False


def get_session(session_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def fmt_time(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%I:%M %p")
    except Exception:
        return ts or "—"


def fmt_dur(minutes):
    if not minutes:
        return "0 min"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}min" if h else f"{m} min"


def parse_time_input(text: str):
    """
    Parse admin-entered time like '5:30 PM', '17:30', '5:30pm'.
    Returns a datetime string or None if invalid.
    """
    text = text.strip().upper().replace(".", ":")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    formats = ["%I:%M %p", "%H:%M", "%I%p", "%I %p"]
    for fmt in formats:
        try:
            t = datetime.strptime(text, fmt)
            return f"{today} {t.strftime('%H:%M')}:00"
        except ValueError:
            continue
    return None
