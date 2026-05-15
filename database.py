"""
database.py — Magic Auto Center
PostgreSQL via pg8000 (pure Python, no system deps) in production,
SQLite for local dev.
"""
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _pg_connect():
    import pg8000
    u = urlparse(DATABASE_URL)
    return pg8000.connect(
        host=u.hostname,
        database=u.path.lstrip("/"),
        user=u.username,
        password=u.password,
        port=u.port or 5432,
        ssl_context=True,
    )


def get_db():
    if DATABASE_URL:
        return _pg_connect()
    import sqlite3
    conn = sqlite3.connect("magic_auto.db")
    conn.row_factory = sqlite3.Row
    return conn


def _rows(cur):
    """Convert pg8000 rows to list of dicts."""
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
    cur  = conn.cursor()
    cur.execute(sql, args if args else None)
    result = _row(cur)
    conn.commit() if DATABASE_URL else conn.commit()
    conn.close()
    return result


def _fetchall(sql, *args):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(sql, args if args else None)
    result = _rows(cur)
    conn.close()
    return result


def _run(sql, *args):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(sql, args if args else None)
    conn.commit()
    conn.close()


def _parse_dt(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    return datetime.strptime(str(val)[:19], "%Y-%m-%d %H:%M:%S")


def _now():
    return datetime.utcnow()


# ── Init ──────────────────────────────────────────────────────────────────────

def init_db():
    conn = get_db()
    cur  = conn.cursor()
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
    now = _now()
    _run(f"INSERT INTO sessions (job_id,employee_id,start_time) VALUES ({_ph()},{_ph()},{_ph()})",
         job_id, emp_id, now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S"))
    return now.strftime("%Y-%m-%d %H:%M:%S")

def close_session(session_id, start_val, end_str=None, auto=False):
    end_dt   = _now() if not end_str else datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
    start_dt = _parse_dt(start_val)
    minutes  = max(0, int((end_dt - start_dt).total_seconds() / 60))
    end_out  = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    _run(f"UPDATE sessions SET end_time={_ph()},duration_minutes={_ph()},auto_closed={_ph()} WHERE id={_ph()}",
         end_dt if DATABASE_URL else end_out, minutes, 1 if auto else 0, session_id)
    return end_out, minutes

def auto_close_all_open_sessions(shop_close_hour_utc):
    sessions = get_all_open_sessions()
    closed = []
    for s in sessions:
        _, minutes = close_session(s["id"], s["start_time"], auto=True)
        closed.append({**s, "minutes": minutes})
    return closed

def get_session(session_id):
    return _fetchone(f"SELECT * FROM sessions WHERE id={_ph()}", session_id)

def get_job_total_minutes(job_id):
    row = _fetchone(f"SELECT COALESCE(SUM(duration_minutes),0) AS total FROM sessions WHERE job_id={_ph()}", job_id)
    return row["total"] if row else 0

def has_active_sessions(job_id):
    row = _fetchone(f"SELECT COUNT(*) AS cnt FROM sessions WHERE job_id={_ph()} AND end_time IS NULL", job_id)
    return (int(row["cnt"]) > 0) if row else False

def get_report_data(days=1):
    since = _now() - timedelta(days=days)
    rows  = _fetchall(
        f"""SELECT s.*,e.name AS emp_name,j.car,j.plate,j.id AS job_id
            FROM sessions s JOIN employees e ON e.telegram_id=s.employee_id
            JOIN jobs j ON j.id=s.job_id
            WHERE s.start_time>={_ph()} AND s.end_time IS NOT NULL
            ORDER BY j.id,s.start_time""",
        since if DATABASE_URL else since.strftime("%Y-%m-%d %H:%M:%S"))
    by_job = {}
    for r in rows:
        jid = r["job_id"]
        by_job.setdefault(jid, {"car": r["car"], "plate": r["plate"], "rows": [], "total": 0})
        by_job[jid]["rows"].append(r)
        by_job[jid]["total"] += r["duration_minutes"] or 0
    return by_job


# ── Formatting ────────────────────────────────────────────────────────────────

from zoneinfo import ZoneInfo
LA = ZoneInfo("America/Los_Angeles")

def _to_la(dt: datetime) -> datetime:
    """Convert a naive UTC datetime to Los Angeles time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(LA)

def fmt_time(val):
    try:
        dt = _parse_dt(val)
        return _to_la(dt).strftime("%I:%M %p")
    except Exception:
        return "—"

def fmt_dur(minutes):
    if not minutes:
        return "0 min"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}min" if h else f"{m} min"

def live_dur(start_val):
    try:
        minutes = int((_now() - _parse_dt(start_val)).total_seconds() / 60)
        return fmt_dur(minutes)
    except Exception:
        return "—"

def parse_time_input(text):
    text = text.strip().upper().replace(".", ":")
    today = _now().strftime("%Y-%m-%d")
    for fmt in ["%I:%M %p", "%H:%M", "%I%p", "%I %p"]:
        try:
            t = datetime.strptime(text, fmt)
            return f"{today} {t.strftime('%H:%M')}:00"
        except ValueError:
            continue
    return None
