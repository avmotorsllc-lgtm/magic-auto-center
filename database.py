"""
database.py — Magic Auto Center
PostgreSQL via pg8000 (pure Python) in production, SQLite for local dev.
All stored timestamps are naive UTC; display converts to America/Los_Angeles.
"""
import os
import ssl
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LA_TZ = ZoneInfo("America/Los_Angeles")

# Button labels to reject as employee names (with and without emoji)
BUTTON_NAME_BLACKLIST = {
    "📋 New Job", "🚗 Shop Status", "📊 Today Report", "📆 Report 7 Days",
    "👥 Staff", "📁 All Jobs", "⏱ My Today", "📋 My History",
    "New Job", "Shop Status", "Today Report", "Report 7 Days",
    "Staff", "All Jobs", "My Today", "My History",
}


# ── Connection ─────────────────────────────────────────────────────────────────

def _pg_connect():
    import pg8000
    u = urlparse(DATABASE_URL)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        return pg8000.connect(
            host=u.hostname, database=u.path.lstrip("/"),
            user=u.username, password=u.password,
            port=u.port or 5432, ssl_context=ssl_ctx,
        )
    except Exception:
        return pg8000.connect(
            host=u.hostname, database=u.path.lstrip("/"),
            user=u.username, password=u.password,
            port=u.port or 5432,
        )


def get_db():
    if DATABASE_URL:
        return _pg_connect()
    import sqlite3
    conn = sqlite3.connect("magic_auto.db")
    conn.row_factory = sqlite3.Row
    return conn


# ── Row helpers ────────────────────────────────────────────────────────────────

def _rows(cur):
    if DATABASE_URL:
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in (cur.fetchall() or [])]
    rows = cur.fetchall()
    return [dict(r) for r in rows] if rows else []


def _row(cur):
    if DATABASE_URL:
        cols = [d[0] for d in cur.description] if cur.description else []
        r = cur.fetchone()
        return dict(zip(cols, r)) if r else None
    r = cur.fetchone()
    return dict(r) if r else None


def _ph():
    return "%s" if DATABASE_URL else "?"


def _fetchone(sql, *args):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args) if args else cur.execute(sql)
        return _row(cur)
    except Exception as e:
        log.error("DB fetchone error: %s | sql: %s", e, sql[:80])
        return None
    finally:
        conn.close()


def _fetchall(sql, *args):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args) if args else cur.execute(sql)
        return _rows(cur)
    except Exception as e:
        log.error("DB fetchall error: %s | sql: %s", e, sql[:80])
        return []
    finally:
        conn.close()


def _run(sql, *args):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args) if args else cur.execute(sql)
        conn.commit()
    except Exception as e:
        log.error("DB run error: %s | sql: %s", e, sql[:80])
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ── Datetime helpers ───────────────────────────────────────────────────────────

def _now():
    """Current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


def _now_naive():
    """Current UTC time as naive datetime (for DB storage)."""
    return datetime.utcnow()


def _parse_dt(val):
    """Parse any timestamp value into a timezone-aware UTC datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)
    dt = datetime.strptime(str(val)[:19], "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def get_la_now():
    """Current time in LA timezone."""
    return datetime.now(LA_TZ)


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    conn = get_db()
    try:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("""CREATE TABLE IF NOT EXISTS employees (
                telegram_id BIGINT PRIMARY KEY, name TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, car TEXT NOT NULL,
                plate TEXT DEFAULT '', client TEXT DEFAULT '',
                works TEXT DEFAULT '', status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY, job_id TEXT NOT NULL,
                employee_id BIGINT NOT NULL, start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP, duration_minutes INTEGER,
                auto_closed INTEGER DEFAULT 0)""")
            for alter in [
                "ALTER TABLE jobs ADD COLUMN color TEXT DEFAULT ''",
                "ALTER TABLE jobs ADD COLUMN due_date TEXT DEFAULT ''",
                "ALTER TABLE jobs ADD COLUMN completed_at TIMESTAMP DEFAULT NULL",
                "ALTER TABLE employees ADD COLUMN status TEXT DEFAULT 'active'",
                "ALTER TABLE jobs ADD COLUMN year TEXT DEFAULT ''",
            ]:
                try:
                    cur.execute(alter)
                    conn.commit()
                except Exception:
                    conn.rollback()
        else:
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS employees (
                    telegram_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    registered_at TEXT DEFAULT (datetime('now')));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, car TEXT NOT NULL,
                    plate TEXT DEFAULT '', client TEXT DEFAULT '',
                    works TEXT DEFAULT '', status TEXT DEFAULT 'active',
                    created_at TEXT DEFAULT (datetime('now')));
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL, employee_id INTEGER NOT NULL,
                    start_time TEXT NOT NULL, end_time TEXT,
                    duration_minutes INTEGER, auto_closed INTEGER DEFAULT 0);
            """)
            for alter in [
                "ALTER TABLE sessions ADD COLUMN auto_closed INTEGER DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN color TEXT DEFAULT ''",
                "ALTER TABLE jobs ADD COLUMN due_date TEXT DEFAULT ''",
                "ALTER TABLE jobs ADD COLUMN completed_at TEXT DEFAULT NULL",
                "ALTER TABLE employees ADD COLUMN status TEXT DEFAULT 'active'",
                "ALTER TABLE jobs ADD COLUMN year TEXT DEFAULT ''",
            ]:
                try:
                    cur.execute(alter)
                except Exception:
                    pass
        conn.commit()
    finally:
        conn.close()

    # ── One-time startup cleanup ───────────────────────────────────────────────
    # Remove employees whose names are keyboard button labels
    for bad_name in BUTTON_NAME_BLACKLIST:
        try:
            _run(f"DELETE FROM employees WHERE name={_ph()}", bad_name)
        except Exception:
            pass

    # Delete 0-minute completed sessions (from double-scans)
    try:
        _run(
            "DELETE FROM sessions WHERE end_time IS NOT NULL "
            "AND (duration_minutes = 0 OR duration_minutes IS NULL)"
        )
    except Exception as e:
        log.warning("Could not clean 0-min sessions: %s", e)


# ── Employees ─────────────────────────────────────────────────────────────────

def get_employee(tid):
    try:
        return _fetchone(f"SELECT * FROM employees WHERE telegram_id={_ph()}", tid)
    except Exception:
        return None


def register_employee(tid, name):
    """Create or re-activate an employee. Always resets status."""
    if DATABASE_URL:
        _run(
            "INSERT INTO employees (telegram_id, name) VALUES (%s, %s) "
            "ON CONFLICT (telegram_id) DO UPDATE SET name=EXCLUDED.name, status=NULL",
            tid, name,
        )
    else:
        _run("INSERT OR IGNORE INTO employees (telegram_id, name) VALUES (?, ?)", tid, name)
        _run("UPDATE employees SET name=?, status=NULL WHERE telegram_id=?", name, tid)


def get_all_employees(include_inactive=False):
    """Return active employees, excluding button-label names."""
    if include_inactive:
        rows = _fetchall("SELECT * FROM employees ORDER BY name")
    else:
        rows = _fetchall(
            "SELECT * FROM employees "
            "WHERE status IS NULL OR status='' OR status='active' "
            "ORDER BY name"
        )
    return [r for r in rows if r.get("name") not in BUTTON_NAME_BLACKLIST]


def deactivate_employee(tid):
    """Soft-delete: mark inactive so historical sessions keep the name."""
    _run(f"UPDATE employees SET status='inactive' WHERE telegram_id={_ph()}", tid)


def delete_employee(tid):
    """Hard delete — kept for internal use only."""
    _run(f"DELETE FROM employees WHERE telegram_id={_ph()}", tid)


def rename_employee(tid, new_name):
    """Rename a technician. All historical sessions reference the new name via JOIN."""
    _run(f"UPDATE employees SET name={_ph()} WHERE telegram_id={_ph()}", new_name, tid)


def get_similar_employee(name):
    """Return an existing active employee whose name is similar (substring match)."""
    employees = get_all_employees()
    name_lower = name.lower().strip()
    for emp in employees:
        emp_lower = (emp.get("name") or "").lower()
        if emp_lower and (name_lower in emp_lower or emp_lower in name_lower):
            return emp
    return None


# ── Jobs ──────────────────────────────────────────────────────────────────────

def get_job(job_id):
    try:
        return _fetchone(f"SELECT * FROM jobs WHERE id={_ph()}", job_id)
    except Exception:
        return None


def get_all_jobs(status="active"):
    return _fetchall(f"SELECT * FROM jobs WHERE status={_ph()} ORDER BY created_at DESC", status)


def add_job(job_id, car, plate="", client="", works="", color="", due_date="", year=""):
    if DATABASE_URL:
        _run(
            "INSERT INTO jobs (id,car,plate,client,works,color,due_date,year,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active') "
            "ON CONFLICT (id) DO UPDATE SET car=EXCLUDED.car,plate=EXCLUDED.plate,"
            "client=EXCLUDED.client,works=EXCLUDED.works,color=EXCLUDED.color,"
            "due_date=EXCLUDED.due_date,year=EXCLUDED.year",
            job_id, car, plate, client, works, color, due_date, year)
    else:
        _run(
            "INSERT OR REPLACE INTO jobs "
            "(id,car,plate,client,works,color,due_date,year,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,'active',datetime('now'))",
            job_id, car, plate, client, works, color, due_date, year)


def update_job_field(job_id, field, value):
    """Update a single field of a job. Only safe fields are allowed."""
    allowed = {"car", "plate", "client", "works", "color", "year"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' not allowed for update")
    _run(f"UPDATE jobs SET {field}={_ph()} WHERE id={_ph()}", value, job_id)


def close_job(job_id):
    """Mark job closed and record completed_at timestamp automatically."""
    now = _now_naive()
    val = now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S")
    _run(
        f"UPDATE jobs SET status='closed', completed_at={_ph()} WHERE id={_ph()}",
        val, job_id,
    )


def reopen_job(job_id):
    """Reopen a closed job."""
    _run(f"UPDATE jobs SET status='active', completed_at=NULL WHERE id={_ph()}", job_id)


def mark_job_done(job_id):
    """Mark job as completed. Does not change status to closed."""
    now = _now_naive()
    val = now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S")
    _run(f"UPDATE jobs SET completed_at={_ph()} WHERE id={_ph()}", val, job_id)


def auto_close_sessions_for_job(job_id):
    """Close all open sessions for a job. Returns list of closed session dicts."""
    sessions = get_active_sessions_for_job(job_id)
    closed = []
    for s in sessions:
        result = close_session(s["id"], s["start_time"], auto=True)
        if result:
            _, minutes = result
            closed.append({**s, "minutes": minutes})
    return closed


def get_job_first_session(job_id):
    """Return the start_time of the earliest session for this job, or None."""
    row = _fetchone(
        f"SELECT MIN(start_time) AS first_start FROM sessions WHERE job_id={_ph()}",
        job_id,
    )
    return row.get("first_start") if row else None


def get_jobs_by_plate(plate, exclude_job_id=None):
    """Find other jobs with the same plate number."""
    if not plate:
        return []
    if exclude_job_id:
        return _fetchall(
            f"SELECT * FROM jobs WHERE plate={_ph()} AND id!={_ph()} ORDER BY created_at DESC",
            plate, exclude_job_id,
        )
    return _fetchall(
        f"SELECT * FROM jobs WHERE plate={_ph()} ORDER BY created_at DESC", plate)


# ── Sessions ──────────────────────────────────────────────────────────────────

def get_open_session(job_id, emp_id):
    return _fetchone(
        f"SELECT * FROM sessions WHERE job_id={_ph()} AND employee_id={_ph()} AND end_time IS NULL",
        job_id, emp_id)


def get_employee_open_session_any(emp_id, exclude_job_id=None):
    if exclude_job_id:
        return _fetchone(
            f"SELECT s.*,j.car,j.plate,j.year,j.color FROM sessions s JOIN jobs j ON j.id=s.job_id "
            f"WHERE s.employee_id={_ph()} AND s.end_time IS NULL AND s.job_id!={_ph()}",
            emp_id, exclude_job_id)
    return _fetchone(
        f"SELECT s.*,j.car,j.plate,j.year,j.color FROM sessions s JOIN jobs j ON j.id=s.job_id "
        f"WHERE s.employee_id={_ph()} AND s.end_time IS NULL", emp_id)


def get_employee_all_open_sessions(emp_id):
    """All currently open sessions for an employee."""
    return _fetchall(
        f"""SELECT s.*,j.car,j.plate,j.year,j.color FROM sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.employee_id={_ph()} AND s.end_time IS NULL
            ORDER BY s.start_time""",
        emp_id)


def get_all_open_sessions():
    return _fetchall("""
        SELECT s.*,e.name AS emp_name,e.telegram_id,j.car,j.plate,j.id AS job_id,j.year,j.color
        FROM sessions s
        JOIN employees e ON e.telegram_id=s.employee_id
        JOIN jobs j ON j.id=s.job_id
        WHERE s.end_time IS NULL ORDER BY s.start_time""")


def get_active_sessions_for_job(job_id):
    return _fetchall(
        f"SELECT s.*,e.name AS emp_name FROM sessions s "
        f"JOIN employees e ON e.telegram_id=s.employee_id "
        f"WHERE s.job_id={_ph()} AND s.end_time IS NULL ORDER BY s.start_time", job_id)


def open_session(job_id, emp_id):
    """Open a new session. Returns (session_id, start_time_str)."""
    now = _now_naive()
    start_str = now.strftime("%Y-%m-%d %H:%M:%S")
    if DATABASE_URL:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sessions (job_id,employee_id,start_time) VALUES (%s,%s,%s) RETURNING id",
                (job_id, emp_id, now))
            conn.commit()
            row = _row(cur)
            session_id = row["id"] if row else None
        finally:
            conn.close()
    else:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sessions (job_id,employee_id,start_time) VALUES (?,?,?)",
                (job_id, emp_id, start_str))
            conn.commit()
            session_id = cur.lastrowid
        finally:
            conn.close()
    return session_id, start_str


def close_session(session_id, start_val, end_str=None, auto=False):
    """Close a session. If duration < 1 min, DELETE it (no junk sessions).
    Returns (end_str, minutes) or None if the session was deleted."""
    if end_str:
        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        end_dt = _now()
    start_dt = _parse_dt(start_val)
    minutes  = max(0, int((end_dt - start_dt).total_seconds() / 60))

    if minutes < 1:
        try:
            _run(f"DELETE FROM sessions WHERE id={_ph()}", session_id)
        except Exception as e:
            log.error("Error deleting short session %s: %s", session_id, e)
        return None  # signal: session was deleted (< 1 min)

    end_naive = end_dt.replace(tzinfo=None)
    end_out   = end_naive.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _run(
            f"UPDATE sessions SET end_time={_ph()},duration_minutes={_ph()},auto_closed={_ph()} WHERE id={_ph()}",
            end_naive if DATABASE_URL else end_out,
            minutes, 1 if auto else 0, session_id,
        )
    except Exception as e:
        log.error("Error closing session %s: %s", session_id, e)
        return None
    return end_out, minutes


def delete_session(session_id):
    """Hard-delete a session (for undo clock-in)."""
    _run(f"DELETE FROM sessions WHERE id={_ph()}", session_id)


def auto_close_all_open_sessions():
    sessions = get_all_open_sessions()
    closed = []
    for s in sessions:
        result = close_session(s["id"], s["start_time"], auto=True)
        if result:
            _, minutes = result
            closed.append({**s, "minutes": minutes})
    return closed


def get_session(session_id):
    return _fetchone(f"SELECT * FROM sessions WHERE id={_ph()}", session_id)


def get_job_total_minutes(job_id):
    row = _fetchone(
        f"SELECT COALESCE(SUM(duration_minutes),0) AS total FROM sessions "
        f"WHERE job_id={_ph()} AND end_time IS NOT NULL AND duration_minutes > 0", job_id)
    total = int(row["total"]) if row else 0
    for s in get_active_sessions_for_job(job_id):
        try:
            total += int((_now() - _parse_dt(s["start_time"])).total_seconds() / 60)
        except Exception:
            pass
    return total


def has_active_sessions(job_id):
    row = _fetchone(
        f"SELECT COUNT(*) AS cnt FROM sessions WHERE job_id={_ph()} AND end_time IS NULL", job_id)
    return (int(row["cnt"]) > 0) if row else False


def get_report_data(days=1):
    """Session data grouped by job → by employee. Skips 0-min sessions."""
    la_now    = get_la_now()
    since_la  = (la_now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_la.astimezone(timezone.utc)
    since     = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")

    rows = _fetchall(
        f"""SELECT s.*,e.name AS emp_name,j.car,j.plate,j.id AS job_id,j.year,j.color
            FROM sessions s
            JOIN employees e ON e.telegram_id=s.employee_id
            JOIN jobs j ON j.id=s.job_id
            WHERE s.start_time>={_ph()} AND s.end_time IS NOT NULL AND s.duration_minutes > 0
            ORDER BY j.id,e.name,s.start_time""",
        since)

    by_job = {}
    for r in rows:
        jid   = r["job_id"]
        year  = (r.get("year")  or "").strip()
        color = (r.get("color") or "").strip()
        car_display = f"{year} {r['car']}".strip() if year else r["car"]
        if color:
            car_display = f"{car_display} · {color}"
        by_job.setdefault(jid, {"car": car_display, "plate": r["plate"], "by_emp": {}, "total": 0})
        emp = r["emp_name"]
        by_job[jid]["by_emp"].setdefault(emp, {"rows": [], "total": 0})
        by_job[jid]["by_emp"][emp]["rows"].append(r)
        by_job[jid]["by_emp"][emp]["total"] += r["duration_minutes"] or 0
        by_job[jid]["total"] += r["duration_minutes"] or 0
    return by_job


def get_report_data_by_day(days=7):
    """Session data grouped by day (LA) → by job → by employee. For multi-day reports."""
    la_now    = get_la_now()
    since_la  = (la_now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_la.astimezone(timezone.utc)
    since     = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")

    rows = _fetchall(
        f"""SELECT s.*,e.name AS emp_name,j.car,j.plate,j.id AS job_id,j.year,j.color
            FROM sessions s
            JOIN employees e ON e.telegram_id=s.employee_id
            JOIN jobs j ON j.id=s.job_id
            WHERE s.start_time>={_ph()} AND s.end_time IS NOT NULL AND s.duration_minutes > 0
            ORDER BY s.start_time""",
        since)

    by_day = {}
    for r in rows:
        d = la_date(r["start_time"])
        if not d:
            continue
        year  = (r.get("year")  or "").strip()
        color = (r.get("color") or "").strip()
        car_display = f"{year} {r['car']}".strip() if year else r["car"]
        if color:
            car_display = f"{car_display} · {color}"
        jid = r["job_id"]
        emp = r["emp_name"]
        if d not in by_day:
            by_day[d] = {"jobs": {}, "total": 0}
        if jid not in by_day[d]["jobs"]:
            by_day[d]["jobs"][jid] = {"car": car_display, "plate": r["plate"], "by_emp": {}, "total": 0}
        by_day[d]["jobs"][jid]["by_emp"].setdefault(emp, {"sessions": [], "total": 0})
        by_day[d]["jobs"][jid]["by_emp"][emp]["sessions"].append(r)
        by_day[d]["jobs"][jid]["by_emp"][emp]["total"] += r["duration_minutes"] or 0
        by_day[d]["jobs"][jid]["total"] += r["duration_minutes"] or 0
        by_day[d]["total"] += r["duration_minutes"] or 0
    return by_day


def get_sessions_for_job(job_id):
    """All sessions for a job with employee name, sorted chronologically."""
    return _fetchall(
        f"""SELECT s.*,e.name AS emp_name
            FROM sessions s JOIN employees e ON e.telegram_id=s.employee_id
            WHERE s.job_id={_ph()}
            ORDER BY s.start_time""",
        job_id)


def get_employee_week_hours(emp_id):
    """Completed sessions for this employee since Monday 00:00 LA time."""
    la_now    = get_la_now()
    monday_la = (la_now - timedelta(days=la_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    since_utc = monday_la.astimezone(timezone.utc)
    since     = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")
    return _fetchall(
        f"""SELECT s.start_time, s.end_time, s.duration_minutes, s.job_id, j.car, j.year, j.color
            FROM sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.employee_id={_ph()} AND s.start_time>={_ph()}
              AND s.end_time IS NOT NULL AND s.duration_minutes > 0
            ORDER BY s.start_time""",
        emp_id, since)


def get_sessions_today():
    """All sessions that started today (LA time), joined with employee + job info."""
    la_now    = get_la_now()
    since_la  = la_now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_la.astimezone(timezone.utc)
    since     = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")
    return _fetchall(
        f"""SELECT s.*,e.name AS emp_name,e.telegram_id,j.car,j.plate,j.id AS job_id,j.client,j.year,j.color
            FROM sessions s
            JOIN employees e ON e.telegram_id=s.employee_id
            JOIN jobs j ON j.id=s.job_id
            WHERE s.start_time>={_ph()}
            ORDER BY s.start_time""",
        since,
    )


def get_employee_sessions_today(emp_id):
    """All sessions (any status) for an employee today in LA time."""
    la_now    = get_la_now()
    since_la  = la_now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_la.astimezone(timezone.utc)
    since     = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")
    return _fetchall(
        f"SELECT id FROM sessions WHERE employee_id={_ph()} AND start_time>={_ph()}",
        emp_id, since)


def get_employee_sessions_last_days(emp_id, days=7):
    """All sessions (open + closed) for this employee in the last N calendar days (LA time)."""
    la_now    = get_la_now()
    since_la  = (la_now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_la.astimezone(timezone.utc)
    since     = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")
    return _fetchall(
        f"""SELECT s.*, j.car, j.year, j.color FROM sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.employee_id={_ph()} AND s.start_time>={_ph()}
            ORDER BY s.start_time""",
        emp_id, since)


def la_date(val):
    """Return the LA calendar date for a UTC timestamp, or None on error."""
    try:
        return _parse_dt(val).astimezone(LA_TZ).date()
    except Exception:
        return None


def get_all_jobs_all():
    """All jobs (active + closed), active first, newest first within each group."""
    return _fetchall(
        "SELECT * FROM jobs "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at DESC"
    )


def get_job_session_count(job_id):
    """Number of valid (> 0 min) sessions for a job."""
    row = _fetchone(
        f"SELECT COUNT(*) AS cnt FROM sessions "
        f"WHERE job_id={_ph()} AND (duration_minutes > 0 OR end_time IS NULL)", job_id)
    return int(row["cnt"]) if row else 0


def delete_job(job_id):
    """Delete all sessions for a job, then delete the job itself."""
    _run(f"DELETE FROM sessions WHERE job_id={_ph()}", job_id)
    _run(f"DELETE FROM jobs WHERE id={_ph()}", job_id)


def update_session_start(session_id, new_start_utc_str):
    """Update a session's start time. Recalculates duration if session is closed."""
    new_start       = datetime.strptime(new_start_utc_str, "%Y-%m-%d %H:%M:%S")
    new_start_aware = new_start.replace(tzinfo=timezone.utc)
    db_val          = new_start if DATABASE_URL else new_start_utc_str
    sess            = get_session(session_id)
    if not sess:
        return
    if sess["end_time"]:
        end_dt  = _parse_dt(sess["end_time"])
        minutes = max(0, int((end_dt - new_start_aware).total_seconds() / 60))
        _run(
            f"UPDATE sessions SET start_time={_ph()},duration_minutes={_ph()} WHERE id={_ph()}",
            db_val, minutes, session_id,
        )
    else:
        _run(f"UPDATE sessions SET start_time={_ph()} WHERE id={_ph()}", db_val, session_id)


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_time(val):
    """Format a UTC timestamp as LA local time, e.g. '9:30 AM'."""
    try:
        la_dt = _parse_dt(val).astimezone(LA_TZ)
        h = la_dt.strftime("%I").lstrip("0") or "12"
        return f"{h}:{la_dt.strftime('%M %p')}"
    except Exception:
        return "—"


def fmt_date(val):
    """Format a UTC timestamp as 'May 18 at 9:30 AM'."""
    try:
        la_dt = _parse_dt(val).astimezone(LA_TZ)
        h = la_dt.strftime("%I").lstrip("0") or "12"
        return la_dt.strftime(f"%b %-d at {h}:%M %p")
    except Exception:
        return "—"


def fmt_date_only(val):
    """Format a UTC timestamp as 'May 18'."""
    try:
        la_dt = _parse_dt(val).astimezone(LA_TZ)
        return la_dt.strftime("%b %-d")
    except Exception:
        return "—"


def fmt_dur(minutes):
    if not minutes:
        return "0 min"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}min" if h else f"{m} min"


def elapsed_minutes(start_val):
    """Minutes elapsed since a UTC timestamp."""
    try:
        return max(0, int((_now() - _parse_dt(start_val)).total_seconds() / 60))
    except Exception:
        return 0


def live_dur(start_val):
    try:
        minutes = int((_now() - _parse_dt(start_val)).total_seconds() / 60)
        return fmt_dur(minutes)
    except Exception:
        return "—"


def parse_time_input(text, date_la=None):
    """Parse clock time string (e.g. '5:30 PM' or '17:30') as LA local time.
    Returns UTC naive datetime string for DB storage, or None on failure."""
    text = text.strip().upper().replace(".", ":")
    if date_la is None:
        date_la = get_la_now().date()
    for fmt in ["%I:%M %p", "%H:%M", "%I%p", "%I %p"]:
        try:
            t      = datetime.strptime(text, fmt)
            la_dt  = datetime.combine(date_la, t.time(), tzinfo=LA_TZ)
            utc_dt = la_dt.astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None
