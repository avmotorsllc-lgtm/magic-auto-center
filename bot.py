"""
bot.py — Magic Auto Center | Telegram Time Tracker
Includes: parallel session detection, forgotten clock-out reminders,
auto-close at end of day, admin manual correction of sessions.
"""
import os
from datetime import datetime, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import database as db

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN        = os.environ["BOT_TOKEN"]
ADMIN_IDS    = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

# Shop hours in UTC. Adjust to match your timezone.
# Example: shop closes 7 PM EST = 23:00 UTC (EST = UTC-5 in winter, UTC-4 in summer)
REMINDER_HOUR  = int(os.environ.get("REMINDER_HOUR", "22"))   # UTC — send reminder 1hr before close
AUTO_CLOSE_HOUR = int(os.environ.get("AUTO_CLOSE_HOUR", "23")) # UTC — auto-close all open sessions
REPORT_HOUR    = int(os.environ.get("REPORT_HOUR", "23"))      # UTC — daily report

BRAND = "🔧 Magic Auto Center"

# Conversation states
(ADD_JOB_ID, ADD_JOB_CAR, ADD_JOB_PLATE,
 ADD_JOB_CLIENT, ADD_JOB_WORKS,
 WAITING_NAME,
 EDITING_SESSION_TIME) = range(7)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ═══════════════════════════════════════════════════════════════════════════════
# /start — handles first-time users, returning users, AND QR scans
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    args     = ctx.args

    # ── Came from QR scan (/start RO-1041) ────────────────────────────────
    if args:
        job_id   = args[0].upper()
        employee = db.get_employee(user_id)

        if not employee:
            ctx.user_data["pending_job"] = job_id
            await update.message.reply_text(
                f"{BRAND}\n\n"
                "👋 Welcome! You're not registered yet.\n\n"
                "What's your name? (as it appears on the schedule)"
            )
            return WAITING_NAME

        job = db.get_job(job_id)
        if not job:
            await update.message.reply_text(f"❌ Job *{job_id}* not found. Ask the office manager.", parse_mode="Markdown")
            return ConversationHandler.END

        await _process_scan(update, ctx, employee, job)
        return ConversationHandler.END

    # ── Regular /start ────────────────────────────────────────────────────
    employee = db.get_employee(user_id)
    if employee:
        if is_admin(user_id):
            await _show_admin_menu(update)
        else:
            await update.message.reply_text(
                f"{BRAND}\n\n"
                f"👋 Hey, *{employee['name']}*!\n\n"
                "Scan the QR sticker on a car to clock in or out.\n"
                "Everything is automatic — no buttons needed. ✅",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            f"{BRAND}\n\n👋 Welcome!\n\nWhat's your name?",
            parse_mode="Markdown"
        )
        return WAITING_NAME

    return ConversationHandler.END


async def receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name    = update.message.text.strip()
    user_id = update.effective_user.id
    db.register_employee(user_id, name)

    await update.message.reply_text(
        f"✅ Registered as *{name}*!\n\n"
        "Now just scan the QR code on any car — everything is automatic.",
        parse_mode="Markdown"
    )

    pending = ctx.user_data.pop("pending_job", None)
    if pending:
        job = db.get_job(pending)
        emp = db.get_employee(user_id)
        if job and emp:
            await _process_scan(update, ctx, emp, job)

    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# CORE: Clock in / Clock out logic
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE, employee, job):
    emp_id  = employee["telegram_id"]
    open_s  = db.get_open_session(job["id"], emp_id)

    if open_s:
        # ── CLOCK OUT ────────────────────────────────────────────────────────
        end_time, minutes = db.close_session(open_s["id"], open_s["start_time"])
        await update.message.reply_text(
            f"⏹ *CLOCKED OUT*\n\n"
            f"👤 {employee['name']}\n"
            f"🚗 {job['car']}  ·  {job['plate'] or '—'}\n"
            f"📋 {job['id']}\n\n"
            f"🕐 Start:    {db.fmt_time(open_s['start_time'])}\n"
            f"🕐 End:      {db.fmt_time(end_time)}\n"
            f"⏱ Duration: *{db.fmt_dur(minutes)}*",
            parse_mode="Markdown"
        )
        return

    # ── About to CLOCK IN — check for open session on another car ─────────
    other = db.get_employee_open_session_any(emp_id, exclude_job_id=job["id"])

    if other:
        # ⚠️ Employee already working on a different car
        since = db.fmt_time(other["start_time"])
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ Clock out of {other['car']} first",
                callback_data=f"sw_close_{other['id']}_{job['id']}_{emp_id}"
            )],
            [InlineKeyboardButton(
                "⚡ I'm working both cars",
                callback_data=f"sw_both_{job['id']}_{emp_id}"
            )],
        ])
        await update.message.reply_text(
            f"⚠️ *Hold on, {employee['name']}!*\n\n"
            f"You're still clocked in on:\n"
            f"🚗 *{other['car']}*  ·  {other['plate'] or other['job_id']}\n"
            f"🕐 Since {since}\n\n"
            f"What do you want to do?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    # ── CLOCK IN (no conflicts) ───────────────────────────────────────────
    start_time = db.open_session(job["id"], emp_id)
    await update.message.reply_text(
        f"▶️ *CLOCKED IN*\n\n"
        f"👤 {employee['name']}\n"
        f"🚗 {job['car']}  ·  {job['plate'] or '—'}\n"
        f"📋 {job['id']}\n"
        f"🔧 {job['works'] or '—'}\n\n"
        f"🕐 Start: {db.fmt_time(start_time)}\n\n"
        f"_Scan the QR again when you're done._",
        parse_mode="Markdown"
    )


# ── Callback: parallel session conflict choice ────────────────────────────────

async def handle_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")

    if parts[1] == "close":
        # sw_close_{old_session_id}_{new_job_id}_{emp_id}
        old_sess_id = int(parts[2])
        new_job_id  = parts[3]
        emp_id      = int(parts[4])

        old_sess = db.get_session(old_sess_id)
        new_job  = db.get_job(new_job_id)
        emp      = db.get_employee(emp_id)

        if not old_sess or not new_job or not emp:
            await query.edit_message_text("❌ Session data not found. Please scan again.")
            return

        # Close old session
        _, old_min = db.close_session(old_sess_id, old_sess["start_time"])

        # Open new session
        start_time = db.open_session(new_job_id, emp_id)

        await query.edit_message_text(
            f"✅ *Done!*\n\n"
            f"⏹ Clocked out of *{old_sess['job_id']}* ({db.fmt_dur(old_min)})\n\n"
            f"▶️ *CLOCKED IN*\n"
            f"🚗 {new_job['car']}  ·  {new_job['plate'] or '—'}\n"
            f"📋 {new_job_id}\n"
            f"🕐 Start: {db.fmt_time(start_time)}",
            parse_mode="Markdown"
        )

    elif parts[1] == "both":
        # sw_both_{new_job_id}_{emp_id}
        new_job_id = parts[2]
        emp_id     = int(parts[3])
        new_job    = db.get_job(new_job_id)
        emp        = db.get_employee(emp_id)

        if not new_job or not emp:
            await query.edit_message_text("❌ Data not found. Please scan again.")
            return

        start_time = db.open_session(new_job_id, emp_id)
        await query.edit_message_text(
            f"▶️ *CLOCKED IN (both cars)*\n\n"
            f"👤 {emp['name']}\n"
            f"🚗 {new_job['car']}  ·  {new_job['plate'] or '—'}\n"
            f"📋 {new_job_id}\n"
            f"🕐 Start: {db.fmt_time(start_time)}\n\n"
            f"_Scan each QR to clock out of each car separately._",
            parse_mode="Markdown"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Admin: /opensessions — see & fix forgotten clock-outs
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_opensessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    open_sessions = db.get_all_open_sessions()

    if not open_sessions:
        await update.message.reply_text("✅ No open sessions — everyone is clocked out.")
        return

    text = f"⚠️ *{len(open_sessions)} open session(s):*\n\n"
    keyboard = []

    for s in open_sessions:
        since = db.fmt_time(s["start_time"])
        text += f"👤 *{s['emp_name']}* on {s['car']} ({s['job_id']}) since {since}\n"
        keyboard.append([
            InlineKeyboardButton(
                f"Close {s['emp_name']} now",
                callback_data=f"adm_close_{s['id']}"
            ),
            InlineKeyboardButton(
                "✏️ Edit time",
                callback_data=f"adm_edit_{s['id']}"
            ),
        ])

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_admin_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    action     = parts[1]   # "close" or "edit"
    session_id = int(parts[2])

    sess = db.get_session(session_id)
    if not sess:
        await query.edit_message_text("Session not found.")
        return

    if action == "close":
        _, minutes = db.close_session(session_id, sess["start_time"])
        await query.edit_message_text(
            f"✅ Session closed.\n"
            f"Duration recorded: *{db.fmt_dur(minutes)}*\n\n"
            f"_If the time is wrong, use the Edit time button._",
            parse_mode="Markdown"
        )

    elif action == "edit":
        ctx.user_data["editing_session_id"] = session_id
        emp   = db.get_employee(sess["employee_id"])
        job   = db.get_job(sess["job_id"])
        since = db.fmt_time(sess["start_time"])
        await query.edit_message_text(
            f"✏️ *Edit end time*\n\n"
            f"👤 {emp['name'] if emp else '?'} on {job['car'] if job else sess['job_id']}\n"
            f"🕐 Started at {since}\n\n"
            f"Type the correct clock-out time:\n"
            f"_(e.g.  5:30 PM  or  17:30)_",
            parse_mode="Markdown"
        )
        return EDITING_SESSION_TIME


async def receive_edited_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session_id = ctx.user_data.pop("editing_session_id", None)
    if not session_id:
        return ConversationHandler.END

    sess    = db.get_session(session_id)
    end_str = db.parse_time_input(update.message.text)

    if not end_str or not sess:
        await update.message.reply_text(
            "❌ Couldn't parse that time. Try again:\n_(e.g.  5:30 PM  or  17:30)_",
            parse_mode="Markdown"
        )
        ctx.user_data["editing_session_id"] = session_id
        return EDITING_SESSION_TIME

    _, minutes = db.close_session(session_id, sess["start_time"], end_str=end_str)
    await update.message.reply_text(
        f"✅ *Session updated!*\n\n"
        f"🕐 End time: {db.fmt_time(end_str)}\n"
        f"⏱ Duration: *{db.fmt_dur(minutes)}*",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduled jobs: reminder, auto-close, daily report
# ═══════════════════════════════════════════════════════════════════════════════

async def evening_reminder_job(ctx):
    """
    Sent 1 hour before auto-close.
    Pings every employee who is still clocked in.
    """
    open_sessions = db.get_all_open_sessions()
    if not open_sessions:
        return

    # Notify each employee
    notified = set()
    for s in open_sessions:
        tid = s["telegram_id"]
        if tid in notified:
            continue
        notified.add(tid)
        try:
            await ctx.bot.send_message(
                tid,
                f"⏰ *Reminder — {BRAND}*\n\n"
                f"You're still clocked in on:\n"
                f"🚗 *{s['car']}*  ·  {s['job_id']}\n"
                f"🕐 Since {db.fmt_time(s['start_time'])}\n\n"
                f"Don't forget to scan the QR when you're done!\n"
                f"_(If you already finished, ask the office manager to fix it.)_",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    # Notify admins too
    emp_list = ", ".join(set(s["emp_name"] for s in open_sessions))
    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                admin_id,
                f"⚠️ *{BRAND} — Reminder sent*\n\n"
                f"{len(open_sessions)} open session(s):\n"
                f"👤 {emp_list}\n\n"
                f"Use /opensessions to review or fix.",
                parse_mode="Markdown"
            )
        except Exception:
            pass


async def auto_close_job(ctx):
    """
    Auto-closes all remaining open sessions at shop closing time.
    Notifies admins and affected employees.
    """
    closed = db.auto_close_all_open_sessions(shop_close_hour_utc=AUTO_CLOSE_HOUR)
    if not closed:
        return

    # Notify each affected employee
    for s in closed:
        try:
            await ctx.bot.send_message(
                s["telegram_id"],
                f"🔒 *Auto clock-out — {BRAND}*\n\n"
                f"Your shift on *{s['car']}* was automatically closed.\n"
                f"🕐 Start: {db.fmt_time(s['start_time'])}\n"
                f"⏱ Recorded: *{db.fmt_dur(s['minutes'])}*\n\n"
                f"_If this is wrong, let the office manager know._",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    # Notify admins
    summary = "\n".join(
        f"· {s['emp_name']} on {s['car']} — {db.fmt_dur(s['minutes'])}"
        for s in closed
    )
    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                admin_id,
                f"🔒 *Auto clock-out complete — {BRAND}*\n\n"
                f"Automatically closed {len(closed)} session(s):\n{summary}\n\n"
                f"To correct any times: /opensessions",
                parse_mode="Markdown"
            )
        except Exception:
            pass


async def daily_report_job(ctx):
    """Sends daily time report to all admins."""
    date    = datetime.utcnow().strftime("%m/%d/%Y")
    by_job  = db.get_report_data(days=1)
    if not by_job:
        return
    text = f"📊 *Daily Report — {date}*\n\n{_build_report_text(by_job)}"
    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(admin_id, text, parse_mode="Markdown")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Admin commands
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_admin_menu(update: Update):
    await update.message.reply_text(
        f"*{BRAND}*\n\n"
        "📋 /addjob — create a repair order\n"
        "🚗 /jobs — active jobs\n"
        "✅ /closejob RO-XXXX — close a job\n"
        "🔗 /qrlink RO-XXXX — get QR link\n\n"
        "👥 /staff — list technicians\n"
        "❌ /removestaff — remove technician\n\n"
        "📊 /report — today's report\n"
        "📆 /report 7 — last 7 days\n\n"
        "⚠️ /opensessions — fix forgotten clock-outs",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await _show_admin_menu(update)


# ── Add job dialog ─────────────────────────────────────────────────────────────

async def cmd_addjob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "*New Repair Order*\n\nStep 1/5 — RO number:\n_(e.g. RO-1043)_",
        parse_mode="Markdown"
    )
    return ADD_JOB_ID

async def addjob_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    job_id = update.message.text.strip().upper()
    if db.get_job(job_id):
        await update.message.reply_text(f"⚠️ *{job_id}* already exists. Try another:", parse_mode="Markdown")
        return ADD_JOB_ID
    ctx.user_data["new_job"] = {"id": job_id}
    await update.message.reply_text(f"✅ {job_id}\n\nStep 2/5 — Make & model:\n_(e.g. BMW X5)_", parse_mode="Markdown")
    return ADD_JOB_CAR

async def addjob_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_job"]["car"] = update.message.text.strip()
    await update.message.reply_text("Step 3/5 — License plate:\n_(or /skip)_", parse_mode="Markdown")
    return ADD_JOB_PLATE

async def addjob_plate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    ctx.user_data["new_job"]["plate"] = "" if t == "/skip" else t.upper()
    await update.message.reply_text("Step 4/5 — Customer name:\n_(or /skip)_", parse_mode="Markdown")
    return ADD_JOB_CLIENT

async def addjob_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    ctx.user_data["new_job"]["client"] = "" if t == "/skip" else t
    await update.message.reply_text(
        "Step 5/5 — Work description:\n_(e.g. Front bumper repaint, hood dent)_\n_(or /skip)_",
        parse_mode="Markdown"
    )
    return ADD_JOB_WORKS

async def addjob_works(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    j = ctx.user_data.pop("new_job", {})
    j["works"] = "" if t == "/skip" else t
    db.add_job(j["id"], j["car"], j.get("plate",""), j.get("client",""), j.get("works",""))
    qr_link = f"https://t.me/{BOT_USERNAME}?start={j['id']}"
    await update.message.reply_text(
        f"✅ *Job created!*\n\n"
        f"📋 *{j['id']}*\n"
        f"🚗 {j['car']}  ·  {j.get('plate') or '—'}\n"
        f"👤 {j.get('client') or '—'}\n"
        f"🔧 {j.get('works') or '—'}\n\n"
        f"🔗 *QR link:*\n`{qr_link}`\n\n"
        f"Run `python generate\\_qr.py` to print the sticker.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("new_job", None)
    ctx.user_data.pop("editing_session_id", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ── Jobs ──────────────────────────────────────────────────────────────────────

async def cmd_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    jobs = db.get_all_jobs("active")
    if not jobs:
        await update.message.reply_text("No active jobs. Create one with /addjob")
        return
    text = "🚗 *Active Jobs:*\n\n"
    for j in jobs:
        total  = db.get_job_total_minutes(j["id"])
        active = db.has_active_sessions(j["id"])
        badge  = " 🟢" if active else ""
        text  += f"*{j['id']}*{badge}  {j['car']}  {j['plate'] or ''}\n⏱ {db.fmt_dur(total)}  ·  👤 {j['client'] or '—'}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_closejob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/closejob RO-1043`", parse_mode="Markdown")
        return
    job_id = ctx.args[0].upper()
    if not db.get_job(job_id):
        await update.message.reply_text(f"Job *{job_id}* not found.", parse_mode="Markdown")
        return
    db.close_job(job_id)
    await update.message.reply_text(f"✅ Job *{job_id}* closed.", parse_mode="Markdown")

async def cmd_qrlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/qrlink RO-1043`", parse_mode="Markdown")
        return
    job_id = ctx.args[0].upper()
    job    = db.get_job(job_id)
    if not job:
        await update.message.reply_text(f"Job *{job_id}* not found.", parse_mode="Markdown")
        return
    qr_link = f"https://t.me/{BOT_USERNAME}?start={job_id}"
    await update.message.reply_text(
        f"🔗 *QR link — {job_id}*\n`{qr_link}`\n\n"
        f"Run `python generate\\_qr.py {job_id}` to print.",
        parse_mode="Markdown"
    )


# ── Staff ─────────────────────────────────────────────────────────────────────

async def cmd_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    employees = db.get_all_employees()
    if not employees:
        await update.message.reply_text(
            "No technicians yet.\n\nThey register automatically on first QR scan.\n"
            f"Share the bot link: t.me/{BOT_USERNAME}"
        )
        return
    text = "👥 *Registered Technicians:*\n\n"
    for emp in employees:
        text += f"· {emp['name']}\n"
    text += "\n/removestaff — remove someone"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_removestaff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    employees = db.get_all_employees()
    if not employees:
        await update.message.reply_text("No technicians to remove.")
        return
    keyboard = [[InlineKeyboardButton(emp["name"], callback_data=f"rem_{emp['telegram_id']}")] for emp in employees]
    keyboard.append([InlineKeyboardButton("✖ Cancel", callback_data="rem_cancel")])
    await update.message.reply_text("Who do you want to remove?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_remove_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "rem_cancel":
        await query.edit_message_text("Cancelled.")
        return
    emp_id = int(query.data.replace("rem_", ""))
    emp    = db.get_employee(emp_id)
    if emp:
        db.delete_employee(emp_id)
        await query.edit_message_text(f"✅ *{emp['name']}* removed.", parse_mode="Markdown")
    else:
        await query.edit_message_text("Not found.")


# ── Reports ───────────────────────────────────────────────────────────────────

def _build_report_text(by_job: dict) -> str:
    text = ""
    for jid, d in by_job.items():
        text += f"🚗 *{jid}*  {d['car']}"
        if d["plate"]:
            text += f"  ·  {d['plate']}"
        text += "\n"
        for r in d["rows"]:
            auto = " _(auto)_" if r["auto_closed"] else ""
            text += (
                f"  · {r['emp_name']}: "
                f"{db.fmt_time(r['start_time'])} → {db.fmt_time(r['end_time'])}"
                f"  ({db.fmt_dur(r['duration_minutes'])}){auto}\n"
            )
        text += f"  ⏱ *Total: {db.fmt_dur(d['total'])}*\n\n"
    return text.strip()

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    days   = int(ctx.args[0]) if ctx.args else 1
    by_job = db.get_report_data(days)
    label  = "today" if days == 1 else f"last {days} days"
    if not by_job:
        await update.message.reply_text(f"No data for {label}.")
        return
    text = f"📊 *{BRAND}*\n*Time Report — {label}*\n\n{_build_report_text(by_job)}"
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# App setup
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    db.init_db()
    app = Application.builder().token(TOKEN).build()

    # /start — handles both registration and QR scans
    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={WAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # /addjob — step by step
    job_conv = ConversationHandler(
        entry_points=[CommandHandler("addjob", cmd_addjob)],
        states={
            ADD_JOB_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, addjob_id)],
            ADD_JOB_CAR:    [MessageHandler(filters.TEXT & ~filters.COMMAND, addjob_car)],
            ADD_JOB_PLATE:  [MessageHandler(filters.TEXT, addjob_plate)],
            ADD_JOB_CLIENT: [MessageHandler(filters.TEXT, addjob_client)],
            ADD_JOB_WORKS:  [MessageHandler(filters.TEXT, addjob_works)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    # Edit session time (triggered by inline button)
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_admin_session, pattern="^adm_edit_")],
        states={EDITING_SESSION_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edited_time)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    app.add_handler(start_conv)
    app.add_handler(job_conv)
    app.add_handler(edit_conv)

    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("jobs",         cmd_jobs))
    app.add_handler(CommandHandler("closejob",     cmd_closejob))
    app.add_handler(CommandHandler("qrlink",       cmd_qrlink))
    app.add_handler(CommandHandler("staff",        cmd_staff))
    app.add_handler(CommandHandler("removestaff",  cmd_removestaff))
    app.add_handler(CommandHandler("report",       cmd_report))
    app.add_handler(CommandHandler("opensessions", cmd_opensessions))

    app.add_handler(CallbackQueryHandler(handle_switch,       pattern="^sw_"))
    app.add_handler(CallbackQueryHandler(handle_admin_session, pattern="^adm_close_"))
    app.add_handler(CallbackQueryHandler(handle_remove_staff,  pattern="^rem_"))

    # Scheduled jobs
    app.job_queue.run_daily(evening_reminder_job, time=time(hour=REMINDER_HOUR,   minute=0))
    app.job_queue.run_daily(auto_close_job,       time=time(hour=AUTO_CLOSE_HOUR, minute=0))
    app.job_queue.run_daily(daily_report_job,     time=time(hour=REPORT_HOUR,     minute=0))

    print(f"✅ {BRAND} — running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
