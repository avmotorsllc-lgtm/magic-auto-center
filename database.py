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
        # Railway internal networking may not require SSL
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
    finally:
        conn.close()


def _fetchall(sql, *args):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args) if args else cur.execute(sql)
        return _rows(cur)
    finally:
        conn.close()


def _run(sql, *args):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args) if args else cur.execute(sql)
        conn.commit()
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
            try:
                cur.execute("ALTER TABLE sessions ADD COLUMN auto_closed INTEGER DEFAULT 0")
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


# ── Employees ─────────────────────────────────────────────────────────────────

def get_employee(tid):
    return _fetchone(f"SELECT * FROM employees WHERE telegram_id={_ph()}", tid)


def register_employee(tid, name):
    if DATABASE_URL:
        _run("INSERT INTO employees (telegram_id,name) VALUES (%s,%s) "
             "ON CONFLICT (telegram_id) DO UPDATE SET name=EXCLUDED.name", tid, name)
    else:
        _run("INSERT OR REPLACE INTO employees (telegram_id,name) VALUES (?,?)", tid, name)


def get_all_employees():
    return _fetchall("SELECT * FROM employees ORDER BY name")


def delete_employee(tid):
    _run(f"DELETE FROM employees WHERE telegram_id={_ph()}", tid)


# ── Jobs ──────────────────────────────────────────────────────────────────────

def get_job(job_id):
    return _fetchone(f"SELECT * FROM jobs WHERE id={_ph()}", job_id)


def get_all_jobs(status="active"):
    return _fetchall(f"SELECT * FROM jobs WHERE status={_ph()} ORDER BY created_at DESC", status)


def add_job(job_id, car, plate="", client="", works=""):
    if DATABASE_URL:
        _run("INSERT INTO jobs (id,car,plate,client,works,status) VALUES (%s,%s,%s,%s,%s,'active') "
             "ON CONFLICT (id) DO UPDATE SET car=EXCLUDED.car,plate=EXCLUDED.plate,"
             "client=EXCLUDED.client,works=EXCLUDED.works",
             job_id, car, plate, client, works)
    else:
        _run("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,'active',datetime('now'))",
             job_id, car, plate, client, works)


def close_job(job_id):
    _run(f"UPDATE jobs SET status='closed' WHERE id={_ph()}", job_id)


# ── Sessions ──────────────────────────────────────────────────────────────────

def get_open_session(job_id, emp_id):
    return _fetchone(
        f"SELECT * FROM sessions WHERE job_id={_ph()} AND employee_id={_ph()} AND end_time IS NULL",
        job_id, emp_id)


def get_employee_open_session_any(emp_id, exclude_job_id=None):
    if exclude_job_id:
        return _fetchone(
            f"SELECT s.*,j.car,j.plate FROM sessions s JOIN jobs j ON j.id=s.job_id "
            f"WHERE s.employee_id={_ph()} AND s.end_time IS NULL AND s.job_id!={_ph()}",
            emp_id, exclude_job_id)
    return _fetchone(
        f"SELECT s.*,j.car,j.plate FROM sessions s JOIN jobs j ON j.id=s.job_id "
        f"WHERE s.employee_id={_ph()} AND s.end_time IS NULL", emp_id)


def get_all_open_sessions():
    return _fetchall("""
        SELECT s.*,e.name AS emp_name,e.telegram_id,j.car,j.plate,j.id AS job_id
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
    now = _now_naive()
    _run(f"INSERT INTO sessions (job_id,employee_id,start_time) VALUES ({_ph()},{_ph()},{_ph()})",
         job_id, emp_id, now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S"))
    return now.strftime("%Y-%m-%d %H:%M:%S")


def close_session(session_id, start_val, end_str=None, auto=False):
    if end_str:
        # end_str is a UTC naive string produced by parse_time_input or stored timestamps
        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        end_dt = _now()
    start_dt = _parse_dt(start_val)
    minutes  = max(0, int((end_dt - start_dt).total_seconds() / 60))
    end_naive = end_dt.replace(tzinfo=None)
    end_out   = end_naive.strftime("%Y-%m-%d %H:%M:%S")
    _run(
        f"UPDATE sessions SET end_time={_ph()},duration_minutes={_ph()},auto_closed={_ph()} WHERE id={_ph()}",
        end_naive if DATABASE_URL else end_out,
        minutes, 1 if auto else 0, session_id,
    )
    return end_out, minutes


def auto_close_all_open_sessions():
    sessions = get_all_open_sessions()
    closed = []
    for s in sessions:
        _, minutes = close_session(s["id"], s["start_time"], auto=True)
        closed.append({**s, "minutes": minutes})
    return closed


def get_session(session_id):
    return _fetchone(f"SELECT * FROM sessions WHERE id={_ph()}", session_id)


def get_job_total_minutes(job_id):
    # Closed sessions
    row = _fetchone(
        f"SELECT COALESCE(SUM(duration_minutes),0) AS total FROM sessions "
        f"WHERE job_id={_ph()} AND end_time IS NOT NULL", job_id)
    total = int(row["total"]) if row else 0
    # Add live time for currently open sessions
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
    """Return session data grouped by job. 'days=1' means today in LA timezone."""
    la_now   = get_la_now()
    since_la = (la_now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_la.astimezone(timezone.utc)
    since = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")

    rows = _fetchall(
        f"""SELECT s.*,e.name AS emp_name,j.car,j.plate,j.id AS job_id
            FROM sessions s JOIN employees e ON e.telegram_id=s.employee_id
            JOIN jobs j ON j.id=s.job_id
            WHERE s.start_time>={_ph()} AND s.end_time IS NOT NULL
            ORDER BY j.id,s.start_time""",
        since)

    by_job = {}
    for r in rows:
        jid = r["job_id"]
        by_job.setdefault(jid, {"car": r["car"], "plate": r["plate"], "rows": [], "total": 0})
        by_job[jid]["rows"].append(r)
        by_job[jid]["total"] += r["duration_minutes"] or 0
    return by_job


def get_employee_week_hours(emp_id):
    """Completed sessions for this employee since Monday 00:00 LA time."""
    la_now = get_la_now()
    monday_la = (la_now - timedelta(days=la_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    since_utc = monday_la.astimezone(timezone.utc)
    since = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")

    return _fetchall(
        f"""SELECT s.start_time, s.end_time, s.duration_minutes, s.job_id, j.car
            FROM sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.employee_id={_ph()} AND s.start_time>={_ph()} AND s.end_time IS NOT NULL
            ORDER BY s.start_time""",
        emp_id, since)


def get_sessions_today():
    """All sessions that started today (LA time), joined with employee + job info."""
    la_now    = get_la_now()
    since_la  = la_now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_la.astimezone(timezone.utc)
    since     = since_utc.replace(tzinfo=None) if DATABASE_URL else since_utc.strftime("%Y-%m-%d %H:%M:%S")
    return _fetchall(
        f"""SELECT s.*,e.name AS emp_name,e.telegram_id,j.car,j.plate,j.id AS job_id,j.client
            FROM sessions s
            JOIN employees e ON e.telegram_id=s.employee_id
            JOIN jobs j ON j.id=s.job_id
            WHERE s.start_time>={_ph()}
            ORDER BY s.start_time""",
        since,
    )


def get_all_jobs_all():
    """All jobs (active + closed), active first, newest first within each group."""
    return _fetchall(
        "SELECT * FROM jobs "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at DESC"
    )


def get_job_session_count(job_id):
    """Number of sessions (open or closed) for a job."""
    row = _fetchone(f"SELECT COUNT(*) AS cnt FROM sessions WHERE job_id={_ph()}", job_id)
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


def parse_time_input(text):
    """
    Parse a clock time string entered by the admin (e.g. '5:30 PM' or '17:30')
    as LA local time and return a UTC naive datetime string for DB storage.
    """
    text = text.strip().upper().replace(".", ":")
    today_la = get_la_now().date()
    for fmt in ["%I:%M %p", "%H:%M", "%I%p", "%I %p"]:
        try:
            t = datetime.strptime(text, fmt)
            la_dt  = datetime.combine(today_la, t.time(), tzinfo=LA_TZ)
            utc_dt = la_dt.astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None
