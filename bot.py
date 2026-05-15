"""
bot.py — Magic Auto Center | Telegram Time Tracker
Technicians scan QR codes on cars to clock in/out automatically.
"""
import logging
import os
import urllib.parse
from datetime import time

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

import database as db

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN        = os.environ["BOT_TOKEN"]
ADMIN_IDS    = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
REMINDER_HOUR   = int(os.environ.get("REMINDER_HOUR",   "22"))   # UTC
AUTO_CLOSE_HOUR = int(os.environ.get("AUTO_CLOSE_HOUR", "23"))   # UTC
REPORT_HOUR     = int(os.environ.get("REPORT_HOUR",     "15"))   # UTC (≈ 8 AM PT)

BRAND = "🔧 Magic Auto Center"

(ADD_JOB_ID, ADD_JOB_CAR, ADD_JOB_PLATE,
 ADD_JOB_CLIENT, ADD_JOB_WORKS,
 WAITING_NAME, EDITING_SESSION_TIME) = range(7)

# ── Admin keyboard ─────────────────────────────────────────────────────────────
ADMIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📋 New Job"),       KeyboardButton("🚗 Shop Status")],
    [KeyboardButton("📊 Today Report"),  KeyboardButton("📆 Report 7 Days")],
    [KeyboardButton("👥 Staff"),         KeyboardButton("📋 Report 30 Days")],
], resize_keyboard=True, input_field_placeholder="Choose an action...")

# Maps button label → logical command name.
# "📋 New Job" is handled exclusively by job_conv ConversationHandler (not handle_buttons).
BUTTON_COMMANDS = {
    "🚗 Shop Status":    "shop_status",
    "📊 Today Report":   "report_1",
    "📆 Report 7 Days":  "report_7",
    "📋 Report 30 Days": "report_30",
    "👥 Staff":          "staff",
}

# Set of ALL keyboard button labels — used to guard conversation state handlers
# from accidentally treating a button press as conversational input.
BUTTON_LABELS = {"📋 New Job"} | set(BUTTON_COMMANDS.keys())


def is_admin(uid): return uid in ADMIN_IDS


# ── Helpers ───────────────────────────────────────────────────────────────────

async def notify_admins(bot, text: str, exclude_uid: int = 0):
    for aid in ADMIN_IDS:
        if aid == exclude_uid:
            continue
        try:
            await bot.send_message(aid, text, parse_mode="Markdown")
        except Exception:
            pass


async def _send_qr(msg, job_id, car, plate, tg_link):
    enc = urllib.parse.quote(tg_link, safe="")
    img = (f"https://api.qrserver.com/v1/create-qr-code/"
           f"?size=400x400&data={enc}&color=0f172a&bgcolor=ffffff&margin=20")
    cap = (
        f"🖨 *QR Sticker — {job_id}*\n"
        f"🚗 {car}  ·  {plate or '—'}\n\n"
        f"Screenshot & print this. Attach to the windshield.\n\n"
        f"🔗 `{tg_link}`"
    )
    try:
        await msg.reply_photo(photo=img, caption=cap, parse_mode="Markdown")
    except Exception:
        await msg.reply_text(f"🔗 *QR — {job_id}*\n`{tg_link}`", parse_mode="Markdown")


async def _show_admin_menu(update: Update):
    await update.message.reply_text(
        f"*{BRAND}*\n\nUse the buttons below or type commands:",
        parse_mode="Markdown", reply_markup=ADMIN_KB,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /start — QR scans + first-time registration
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args

    if args:
        job_id   = args[0].upper()
        employee = db.get_employee(uid)
        if not employee:
            ctx.user_data["pending_job"] = job_id
            await update.message.reply_text(
                f"{BRAND}\n\n👋 Welcome! You're not registered yet.\n\nWhat's your name?"
            )
            return WAITING_NAME
        job = db.get_job(job_id)
        if not job:
            await update.message.reply_text(f"❌ Job *{job_id}* not found.", parse_mode="Markdown")
            return ConversationHandler.END
        await _process_scan(update, ctx, employee, job)
        return ConversationHandler.END

    employee = db.get_employee(uid)
    if employee:
        if is_admin(uid):
            await _show_admin_menu(update)
        else:
            await update.message.reply_text(
                f"{BRAND}\n\n👋 Hey, *{employee['name']}*!\n\n"
                "Scan the QR sticker on a car to clock in or out. Everything is automatic ✅",
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(f"{BRAND}\n\n👋 Welcome!\n\nWhat's your name?")
        return WAITING_NAME

    return ConversationHandler.END


async def receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    uid  = update.effective_user.id
    db.register_employee(uid, name)
    await update.message.reply_text(
        f"✅ Registered as *{name}*!\n\nScan any car QR to clock in — fully automatic.",
        parse_mode="Markdown",
    )
    pending = ctx.user_data.pop("pending_job", None)
    if pending:
        job = db.get_job(pending)
        emp = db.get_employee(uid)
        if job and emp:
            await _process_scan(update, ctx, emp, job)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Clock in / Clock out
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE, employee, job):
    emp_id = employee["telegram_id"]
    open_s = db.get_open_session(job["id"], emp_id)

    if open_s:
        # ── CLOCK OUT ────────────────────────────────────────────────────────
        end_time, minutes = db.close_session(open_s["id"], open_s["start_time"])
        msg = (
            f"⏹ *CLOCKED OUT*\n\n"
            f"👤 {employee['name']}\n"
            f"🚗 {job['car']}  ·  {job['plate'] or '—'}\n"
            f"📋 {job['id']}\n\n"
            f"🕐 In:  {db.fmt_time(open_s['start_time'])}\n"
            f"🕐 Out: {db.fmt_time(end_time)}\n"
            f"⏱ *{db.fmt_dur(minutes)}*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        await notify_admins(
            ctx.bot,
            f"⏹ *Clock Out*\n👤 {employee['name']}\n"
            f"🚗 {job['car']}  ·  {job['plate'] or '—'}\n"
            f"📋 {job['id']}  ·  ⏱ {db.fmt_dur(minutes)}",
            exclude_uid=emp_id,
        )
        return

    # ── Check for open session on another car ─────────────────────────────
    other = db.get_employee_open_session_any(emp_id, exclude_job_id=job["id"])
    if other:
        since   = db.fmt_time(other["start_time"])
        elapsed = db.live_dur(other["start_time"])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ Clock out of {other['car']} first",
                callback_data=f"sw_close_{other['id']}_{job['id']}_{emp_id}",
            )], [
            InlineKeyboardButton(
                "⚡ I'm working both cars",
                callback_data=f"sw_both_{job['id']}_{emp_id}",
            ),
        ]])
        await update.message.reply_text(
            f"⚠️ *Hold on, {employee['name']}!*\n\n"
            f"You're still clocked in on:\n"
            f"🚗 *{other['car']}*  ·  {other.get('plate') or other.get('job_id', '')}\n"
            f"🕐 Since {since}  ({elapsed} ago)\n\n"
            f"What do you want to do?",
            parse_mode="Markdown", reply_markup=keyboard,
        )
        return

    # ── CLOCK IN ─────────────────────────────────────────────────────────────
    start_time = db.open_session(job["id"], emp_id)
    msg = (
        f"▶️ *CLOCKED IN*\n\n"
        f"👤 {employee['name']}\n"
        f"🚗 {job['car']}  ·  {job['plate'] or '—'}\n"
        f"📋 {job['id']}\n"
        f"🔧 {job['works'] or '—'}\n\n"
        f"🕐 {db.fmt_time(start_time)}\n\n"
        f"_Scan the QR again when you're done._"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
    await notify_admins(
        ctx.bot,
        f"▶️ *Clock In*\n👤 {employee['name']}\n"
        f"🚗 {job['car']}  ·  {job['plate'] or '—'}\n"
        f"📋 {job['id']}  ·  🕐 {db.fmt_time(start_time)}",
        exclude_uid=emp_id,
    )


async def handle_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")

    if parts[1] == "close":
        old_id, new_job_id, emp_id = int(parts[2]), parts[3], int(parts[4])
        old_s = db.get_session(old_id)
        new_j = db.get_job(new_job_id)
        emp   = db.get_employee(emp_id)
        if not old_s or not new_j or not emp:
            await query.edit_message_text("❌ Data not found. Scan again.")
            return
        _, old_min = db.close_session(old_id, old_s["start_time"])
        start_time = db.open_session(new_job_id, emp_id)
        await query.edit_message_text(
            f"✅ *Done!*\n\n"
            f"⏹ Clocked out of *{old_s['job_id']}*  ({db.fmt_dur(old_min)})\n\n"
            f"▶️ *CLOCKED IN*\n"
            f"🚗 {new_j['car']}  ·  {new_j['plate'] or '—'}\n"
            f"📋 {new_job_id}  ·  🕐 {db.fmt_time(start_time)}",
            parse_mode="Markdown",
        )

    elif parts[1] == "both":
        new_job_id, emp_id = parts[2], int(parts[3])
        new_j = db.get_job(new_job_id)
        emp   = db.get_employee(emp_id)
        if not new_j or not emp:
            await query.edit_message_text("❌ Data not found. Scan again.")
            return
        start_time = db.open_session(new_job_id, emp_id)
        await query.edit_message_text(
            f"▶️ *CLOCKED IN (both cars)*\n\n"
            f"👤 {emp['name']}\n"
            f"🚗 {new_j['car']}  ·  {new_j['plate'] or '—'}\n"
            f"📋 {new_job_id}  ·  🕐 {db.fmt_time(start_time)}\n\n"
            f"_Scan each QR separately to clock out of each car._",
            parse_mode="Markdown",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Admin keyboard buttons
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Routes keyboard button taps. '📋 New Job' is handled by job_conv instead."""
    if not is_admin(update.effective_user.id):
        return
    cmd = BUTTON_COMMANDS.get(update.message.text)
    if cmd == "shop_status":
        await cmd_shop_status(update, ctx)
    elif cmd == "report_1":
        await _send_report(update, 1)
    elif cmd == "report_7":
        await _send_report(update, 7)
    elif cmd == "report_30":
        await _send_report(update, 30)
    elif cmd == "staff":
        await cmd_staff(update, ctx)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await _show_admin_menu(update)


# ═══════════════════════════════════════════════════════════════════════════════
# Add Job conversation
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_addjob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ctx.user_data.pop("new_job", None)
    await update.message.reply_text(
        "*New Repair Order*\n\nStep 1/5 — RO number:\n_(e.g. RO-1043)_\n\n"
        "_Type /cancel to stop._",
        parse_mode="Markdown",
    )
    return ADD_JOB_ID


async def addjob_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await update.message.reply_text(
            "Please type the RO number (e.g. RO-1043), or /cancel to stop.")
        return ADD_JOB_ID
    job_id = text.upper()
    if db.get_job(job_id):
        await update.message.reply_text(
            f"⚠️ *{job_id}* already exists. Enter a different RO number:", parse_mode="Markdown")
        return ADD_JOB_ID
    ctx.user_data["new_job"] = {"id": job_id}
    await update.message.reply_text(
        f"✅ {job_id}\n\nStep 2/5 — Make & model:\n_(e.g. BMW X5)_", parse_mode="Markdown")
    return ADD_JOB_CAR


async def addjob_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await update.message.reply_text("Please type the make & model, or /cancel.")
        return ADD_JOB_CAR
    ctx.user_data["new_job"]["car"] = text
    await update.message.reply_text("Step 3/5 — License plate:\n_(or /skip)_", parse_mode="Markdown")
    return ADD_JOB_PLATE


async def addjob_plate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await update.message.reply_text("Please type the license plate, or /skip.")
        return ADD_JOB_PLATE
    ctx.user_data["new_job"]["plate"] = "" if text.lower() == "/skip" else text.upper()
    await update.message.reply_text("Step 4/5 — Customer name:\n_(or /skip)_", parse_mode="Markdown")
    return ADD_JOB_CLIENT


async def addjob_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await update.message.reply_text("Please type the customer name, or /skip.")
        return ADD_JOB_CLIENT
    ctx.user_data["new_job"]["client"] = "" if text.lower() == "/skip" else text
    await update.message.reply_text(
        "Step 5/5 — Work description:\n_(e.g. Front bumper repaint, hood dent)_\n_(or /skip)_",
        parse_mode="Markdown",
    )
    return ADD_JOB_WORKS


async def addjob_works(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await update.message.reply_text("Please type the work description, or /skip.")
        return ADD_JOB_WORKS
    j = ctx.user_data.pop("new_job", {})
    j["works"] = "" if text.lower() == "/skip" else text
    db.add_job(j["id"], j["car"], j.get("plate", ""), j.get("client", ""), j.get("works", ""))
    qr_link = f"https://t.me/{BOT_USERNAME}?start={j['id']}"
    await update.message.reply_text(
        f"✅ *Job created!*\n\n"
        f"📋 *{j['id']}*\n"
        f"🚗 {j['car']}  ·  {j.get('plate') or '—'}\n"
        f"👤 {j.get('client') or '—'}\n"
        f"🔧 {j.get('works') or '—'}",
        parse_mode="Markdown", reply_markup=ADMIN_KB,
    )
    await _send_qr(update.message, j["id"], j["car"], j.get("plate", ""), qr_link)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("new_job", None)
    ctx.user_data.pop("editing_session_id", None)
    await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Shop Status — active jobs + who's in + suspicious sessions in one view
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_shop_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    la_now   = db.get_la_now()
    date_str = la_now.strftime("%-m/%-d, %-I:%M %p")
    jobs     = db.get_all_jobs("active")

    if not jobs:
        await update.message.reply_text(
            f"🚗 *SHOP STATUS — {date_str}*\n\n"
            "No active jobs. Tap 📋 New Job to create one.",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
        return

    all_open      = db.get_all_open_sessions()
    active_job_ids = {s["job_id"] for s in all_open}

    jobs_active = [j for j in jobs if j["id"] in active_job_ids]
    jobs_idle   = [j for j in jobs if j["id"] not in active_job_ids]

    lines    = [f"🚗 *SHOP STATUS — {date_str}*\n", "━━━ ACTIVE JOBS ━━━\n"]
    keyboard = []
    suspicious = []          # (session_dict, elapsed_minutes)

    for j in jobs_active + jobs_idle:
        is_active = j["id"] in active_job_ids
        badge     = "🟢" if is_active else "⬜"
        total     = db.get_job_total_minutes(j["id"])

        header = f"{badge} *{j['id']}*  ·  {j['car']}"
        if j["plate"]:
            header += f"  ·  {j['plate']}"
        lines.append(header)
        lines.append(f"👤 {j['client'] or '—'}  ·  ⏱ {db.fmt_dur(total)} total")

        if is_active:
            for s in (s for s in all_open if s["job_id"] == j["id"]):
                elapsed = db.elapsed_minutes(s["start_time"])
                lines.append(
                    f"  › {s['emp_name']}: {db.fmt_dur(elapsed)} "
                    f"(since {db.fmt_time(s['start_time'])})"
                )
                keyboard.append([InlineKeyboardButton(
                    f"✏️ Fix: {s['emp_name']} on {s['job_id']}",
                    callback_data=f"adm_edit_{s['id']}",
                )])
                if elapsed >= 8 * 60:
                    suspicious.append((s, elapsed))
        else:
            lines.append("  _(nobody working now)_")

        lines.append("")

    # ── Bottom summary ────────────────────────────────────────────────────────
    if suspicious:
        lines.append(f"━━━ ⚠️ {len(suspicious)} LONG SESSION(S) ━━━")
        for s, elapsed in suspicious:
            lines.append(
                f"  {s['emp_name']} on {s['car']} — "
                f"{db.fmt_dur(elapsed)} _(forgot to clock out?)_"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"✏️ Edit time — {s['emp_name']}",
                    callback_data=f"adm_edit_{s['id']}",
                ),
                InlineKeyboardButton(
                    f"⛔ Close now",
                    callback_data=f"adm_close_{s['id']}",
                ),
            ])
    elif not jobs_active:
        lines.append("━━━ 🏁 EVERYONE CLOCKED OUT ━━━")
        lines.append("_Great work today!_")
    else:
        lines.append("━━━ NOBODY FORGOT TO CLOCK OUT ✅ ━━━")

    text         = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ADMIN_KB
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_admin_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts      = query.data.split("_")
    action     = parts[1]
    session_id = int(parts[2])
    sess       = db.get_session(session_id)

    if not sess:
        await query.edit_message_text("Session not found.")
        return

    if action == "close":
        _, minutes = db.close_session(session_id, sess["start_time"])
        await query.edit_message_text(
            f"✅ Closed. Duration: *{db.fmt_dur(minutes)}*\n_(Use Edit time if incorrect)_",
            parse_mode="Markdown",
        )
    elif action == "edit":
        ctx.user_data["editing_session_id"] = session_id
        emp = db.get_employee(sess["employee_id"])
        job = db.get_job(sess["job_id"])
        await query.edit_message_text(
            f"✏️ *Edit clock-out time*\n\n"
            f"👤 {emp['name'] if emp else '?'}  →  {job['car'] if job else sess['job_id']}\n"
            f"🕐 Clocked in at {db.fmt_time(sess['start_time'])}\n\n"
            f"Type the correct end time (LA time):\n_(e.g.  5:30 PM  or  17:30)_",
            parse_mode="Markdown",
        )
        return EDITING_SESSION_TIME


async def receive_edited_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await update.message.reply_text(
            "Please type the end time (e.g. 5:30 PM or 17:30), or /cancel.")
        return EDITING_SESSION_TIME

    session_id = ctx.user_data.pop("editing_session_id", None)
    if not session_id:
        return ConversationHandler.END
    sess    = db.get_session(session_id)
    end_str = db.parse_time_input(text)
    if not end_str or not sess:
        await update.message.reply_text(
            "❌ Couldn't parse time. Try again:\n_(e.g.  5:30 PM  or  17:30)_",
            parse_mode="Markdown",
        )
        ctx.user_data["editing_session_id"] = session_id
        return EDITING_SESSION_TIME
    _, minutes = db.close_session(session_id, sess["start_time"], end_str=end_str)
    await update.message.reply_text(
        f"✅ *Updated!*\n⏱ Duration: *{db.fmt_dur(minutes)}*",
        parse_mode="Markdown", reply_markup=ADMIN_KB,
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Job management
# ═══════════════════════════════════════════════════════════════════════════════

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

    active = db.get_active_sessions_for_job(job_id)
    if active:
        names = ", ".join(s["emp_name"] for s in active)
        await update.message.reply_text(
            f"⚠️ *Cannot close {job_id}*\n\n"
            f"The following technician(s) are still clocked in:\n👤 {names}\n\n"
            f"Use 🚗 Shop Status to close their sessions first.",
            parse_mode="Markdown", reply_markup=ADMIN_KB,
        )
        return

    db.close_job(job_id)
    await update.message.reply_text(
        f"✅ Job *{job_id}* closed.", parse_mode="Markdown", reply_markup=ADMIN_KB)


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
    await _send_qr(update.message, job_id, job["car"], job["plate"], qr_link)


# ═══════════════════════════════════════════════════════════════════════════════
# My Stats — technician's own weekly hours
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    employee = db.get_employee(uid)
    if not employee:
        await update.message.reply_text(
            "You're not registered yet. Scan a car QR code to get started.")
        return

    sessions  = db.get_employee_week_hours(uid)
    total_min = sum(s["duration_minutes"] or 0 for s in sessions)
    open_sess = db.get_employee_open_session_any(uid)

    la_now     = db.get_la_now()
    week_start = (la_now.date().isoformat())

    text = f"📊 *Your hours this week, {employee['name']}:*\n\n"

    if sessions:
        for s in sessions:
            text += (
                f"🚗 {s['car']}  ·  📋 {s['job_id']}\n"
                f"  {db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
                f"  *({db.fmt_dur(s['duration_minutes'])})*\n\n"
            )
    else:
        text += "No completed sessions this week.\n\n"

    if open_sess:
        text += (
            f"🟢 *Currently clocked in:*\n"
            f"🚗 {open_sess['car']}  ·  ⏱ {db.live_dur(open_sess['start_time'])}\n\n"
        )

    text += f"⏱ *Total completed: {db.fmt_dur(total_min)}*"
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# Staff
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    employees = db.get_all_employees()
    if not employees:
        await update.message.reply_text(
            f"No technicians yet.\nShare the bot: t.me/{BOT_USERNAME}\n"
            "They register on first scan.", reply_markup=ADMIN_KB,
        )
        return
    text = "👥 *Registered Technicians:*\n\n"
    for emp in employees:
        text += f"· {emp['name']}\n"
    text += "\n/removestaff — remove someone"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KB)


async def cmd_removestaff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    employees = db.get_all_employees()
    if not employees:
        await update.message.reply_text("No technicians to remove.", reply_markup=ADMIN_KB)
        return
    keyboard = [
        [InlineKeyboardButton(emp["name"], callback_data=f"rem_{emp['telegram_id']}")]
        for emp in employees
    ]
    keyboard.append([InlineKeyboardButton("✖ Cancel", callback_data="rem_cancel")])
    await update.message.reply_text(
        "Who do you want to remove?", reply_markup=InlineKeyboardMarkup(keyboard))


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


# ═══════════════════════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════════════════════

def _build_report(days):
    by_job = db.get_report_data(days)
    label  = "today" if days == 1 else f"last {days} days"
    if not by_job:
        return f"No data for {label}."
    text = f"📊 *{BRAND}*\n*Time Report — {label}*\n\n"
    for jid, d in by_job.items():
        text += f"🚗 *{jid}*  {d['car']}"
        if d["plate"]:
            text += f"  ·  {d['plate']}"
        text += "\n"
        for r in d["rows"]:
            auto  = " _(auto-closed)_" if r.get("auto_closed") else ""
            text += (
                f"  · {r['emp_name']}: "
                f"{db.fmt_time(r['start_time'])} → {db.fmt_time(r['end_time'])}"
                f"  ({db.fmt_dur(r['duration_minutes'])}){auto}\n"
            )
        text += f"  ⏱ *Total: {db.fmt_dur(d['total'])}*\n\n"
    return text.strip()


async def _send_report(update: Update, days: int):
    await update.message.reply_text(
        _build_report(days), parse_mode="Markdown", reply_markup=ADMIN_KB)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    days = int(ctx.args[0]) if ctx.args else 1
    await _send_report(update, days)


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduled jobs
# ═══════════════════════════════════════════════════════════════════════════════

async def evening_reminder_job(ctx):
    sessions = db.get_all_open_sessions()
    if not sessions:
        return
    notified = set()
    for s in sessions:
        tid = s["telegram_id"]
        if tid in notified:
            continue
        notified.add(tid)
        try:
            await ctx.bot.send_message(
                tid,
                f"⏰ *Reminder — {BRAND}*\n\n"
                f"You're still clocked in on:\n"
                f"🚗 *{s['car']}*  ({s['job_id']})\n"
                f"🕐 Since {db.fmt_time(s['start_time'])}  ·  {db.live_dur(s['start_time'])}\n\n"
                f"Don't forget to scan the QR when you're done!",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    names = ", ".join(sorted(set(s["emp_name"] for s in sessions)))
    await notify_admins(
        ctx.bot,
        f"⚠️ *{len(sessions)} open session(s) at end of day*\n👤 {names}\n\n"
        f"Use 🚗 Shop Status to fix.",
    )


async def auto_close_job(ctx):
    closed = db.auto_close_all_open_sessions()
    if not closed:
        return
    for s in closed:
        try:
            await ctx.bot.send_message(
                s["telegram_id"],
                f"🔒 *Auto clock-out — {BRAND}*\n\n"
                f"Your shift on *{s['car']}* was automatically closed.\n"
                f"⏱ Recorded: *{db.fmt_dur(s['minutes'])}*\n\n"
                f"_If incorrect, let the office manager know._",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    summary = "\n".join(
        f"· {s['emp_name']} — {s['car']} — {db.fmt_dur(s['minutes'])}" for s in closed)
    await notify_admins(
        ctx.bot,
        f"🔒 *Auto clock-out — {len(closed)} session(s)*\n\n{summary}\n\n"
        f"Use 🚗 Shop Status to correct times.",
    )


async def daily_report_job(ctx):
    la_now = db.get_la_now()
    date_str = la_now.strftime("%-m/%-d/%Y")
    text = f"📊 *Daily Report — {date_str}*\n\n{_build_report(1)}"
    await notify_admins(ctx.bot, text)


async def weekly_report_job(ctx):
    """Runs daily; sends the 7-day report only on Monday mornings (LA time)."""
    la_now = db.get_la_now()
    if la_now.weekday() != 0:   # 0 = Monday
        return
    week_start = (la_now.strftime("%-m/%-d"))
    text = f"📆 *Weekly Report — week of {week_start}*\n\n{_build_report(7)}"
    await notify_admins(ctx.bot, text)


# ═══════════════════════════════════════════════════════════════════════════════
# Error handler
# ═══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=ctx.error)
    err_text = (
        f"⚠️ *Bot Error*\n"
        f"`{type(ctx.error).__name__}: {str(ctx.error)[:200]}`\n\n"
        f"_Check Railway logs for full traceback._"
    )
    await notify_admins(ctx.bot, err_text)


# ═══════════════════════════════════════════════════════════════════════════════
# App setup
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    db.init_db()
    app = Application.builder().token(TOKEN).build()

    # ── Conversation: first-time registration via /start ──────────────────────
    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={WAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: add a new job ───────────────────────────────────────────
    # Bug fix: "📋 New Job" button is handled ONLY here (not in handle_buttons).
    # allow_reentry=True lets admins restart the flow by pressing the button again.
    job_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addjob", cmd_addjob),
            MessageHandler(filters.Text(["📋 New Job"]), cmd_addjob),
        ],
        states={
            ADD_JOB_ID:     [MessageHandler(filters.TEXT, addjob_id)],
            ADD_JOB_CAR:    [MessageHandler(filters.TEXT, addjob_car)],
            ADD_JOB_PLATE:  [MessageHandler(filters.TEXT, addjob_plate)],
            ADD_JOB_CLIENT: [MessageHandler(filters.TEXT, addjob_client)],
            ADD_JOB_WORKS:  [MessageHandler(filters.TEXT, addjob_works)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: admin edits a session's clock-out time ──────────────────
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_admin_session, pattern="^adm_edit_")],
        states={
            EDITING_SESSION_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edited_time)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    # Keyboard button handler — only buttons NOT handled by ConversationHandlers
    app_button_filter = filters.Text(list(BUTTON_COMMANDS.keys()))

    app.add_handler(start_conv)
    app.add_handler(job_conv)
    app.add_handler(edit_conv)
    app.add_handler(MessageHandler(app_button_filter, handle_buttons))

    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("status",      cmd_shop_status))   # /status shortcut
    app.add_handler(CommandHandler("closejob",    cmd_closejob))
    app.add_handler(CommandHandler("qrlink",      cmd_qrlink))
    app.add_handler(CommandHandler("mystats",     cmd_mystats))
    app.add_handler(CommandHandler("staff",       cmd_staff))
    app.add_handler(CommandHandler("removestaff", cmd_removestaff))
    app.add_handler(CommandHandler("report",      cmd_report))

    app.add_handler(CallbackQueryHandler(handle_switch,       pattern="^sw_"))
    app.add_handler(CallbackQueryHandler(handle_admin_session, pattern="^adm_close_"))
    app.add_handler(CallbackQueryHandler(handle_remove_staff,  pattern="^rem_"))

    app.add_error_handler(error_handler)

    jq = app.job_queue
    jq.run_daily(evening_reminder_job, time=time(hour=REMINDER_HOUR,   minute=0))
    jq.run_daily(auto_close_job,       time=time(hour=AUTO_CLOSE_HOUR, minute=0))
    jq.run_daily(daily_report_job,     time=time(hour=REPORT_HOUR,     minute=0))
    jq.run_daily(weekly_report_job,    time=time(hour=REPORT_HOUR,     minute=30))

    print(f"✅ {BRAND} — running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
