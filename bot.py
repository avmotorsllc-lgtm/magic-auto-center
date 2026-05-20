"""
bot.py — Magic Auto Center | Telegram Time Tracker
Technicians scan QR codes on cars to clock in/out automatically.
"""
import asyncio
import logging
import os
import re
import urllib.parse
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

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
REMINDER_HOUR   = int(os.environ.get("REMINDER_HOUR",   "22"))
AUTO_CLOSE_HOUR = int(os.environ.get("AUTO_CLOSE_HOUR", "23"))
REPORT_HOUR     = int(os.environ.get("REPORT_HOUR",     "15"))

BRAND = "🔧 Magic Auto Center"

# Conversation states
(ADD_JOB_ID, ADD_JOB_YEAR, ADD_JOB_CAR, ADD_JOB_PLATE, ADD_JOB_CLIENT,
 WAITING_NAME, CONFIRM_NAME, EDITING_SESSION_TIME,
 EDIT_JOB_FIELD, RENAME_TECH, MY_NAME_STATE) = range(11)

# ── Admin keyboard ─────────────────────────────────────────────────────────────
ADMIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📋 New Job"),       KeyboardButton("🚗 Shop Status")],
    [KeyboardButton("📊 Today Report"),  KeyboardButton("📆 Report 7 Days")],
    [KeyboardButton("👥 Staff"),         KeyboardButton("📁 All Jobs")],
], resize_keyboard=True, input_field_placeholder="Choose an action...")

BUTTON_COMMANDS = {
    "🚗 Shop Status":  "shop_status",
    "📊 Today Report": "report_1",
    "📆 Report 7 Days":"report_7",
    "📁 All Jobs":     "alljobs",
    "👥 Staff":        "staff",
}

BUTTON_LABELS = {"📋 New Job"} | set(BUTTON_COMMANDS.keys())

# ── Technician keyboard ───────────────────────────────────────────────────────
TECH_KB = ReplyKeyboardMarkup([
    [KeyboardButton("⏱ My Today"), KeyboardButton("📋 My History")],
], resize_keyboard=True, input_field_placeholder="Choose...")

TECH_BUTTONS = {"⏱ My Today", "📋 My History"}

JOBS_PER_PAGE = 10


def is_admin(uid): return uid in ADMIN_IDS


def is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


_ESC_CHARS = re.compile(r'([_*`\[])')

def escape_md(text) -> str:
    return _ESC_CHARS.sub(r'\\\1', str(text or ""))


def _car_display(job) -> str:
    """Return 'Year Make/Model' or just 'Make/Model' if no year."""
    year = (job.get("year") or "").strip()
    car  = (job.get("car")  or "").strip()
    return f"{year} {car}".strip() if year else car


async def send_safe(send_fn, text: str, **kwargs):
    """Send text, splitting into ≤3800-char chunks on newline boundaries."""
    MAX = 3800
    while len(text) > MAX:
        split_at = text.rfind("\n", 0, MAX)
        if split_at == -1:
            split_at = MAX
        chunk_kwargs = dict(kwargs)
        chunk_kwargs.pop("reply_markup", None)
        await send_fn(text[:split_at], **chunk_kwargs)
        text = text[split_at:].lstrip("\n")
    await send_fn(text, **kwargs)


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
        f"🖨 *QR Sticker — {escape_md(job_id)}*\n"
        f"🚗 {escape_md(car)}  ·  {escape_md(plate or '—')}\n\n"
        f"Screenshot & print this. Attach to the windshield.\n\n"
        f"🔗 `{tg_link}`"
    )
    try:
        await msg.reply_photo(photo=img, caption=cap, parse_mode="Markdown")
    except Exception:
        await msg.reply_text(f"🔗 *QR — {escape_md(job_id)}*\n`{tg_link}`", parse_mode="Markdown")


async def _show_admin_menu(update: Update):
    await update.message.reply_text(
        f"*{BRAND}*\n\nUse the buttons below or type commands:",
        parse_mode="Markdown", reply_markup=ADMIN_KB,
    )


async def _dispatch_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Cancel any active conversation and execute the keyboard button pressed."""
    ctx.user_data.pop("new_job", None)
    ctx.user_data.pop("editing_session_id", None)
    ctx.user_data.pop("editing_mode", None)
    ctx.user_data.pop("editing_date", None)
    ctx.user_data.pop("editing_job_id", None)
    ctx.user_data.pop("editing_field", None)
    ctx.user_data.pop("renaming_emp_id", None)
    cmd = BUTTON_COMMANDS.get(text)
    if cmd == "shop_status":   await cmd_shop_status(update, ctx)
    elif cmd == "report_1":    await _send_report(update, 1)
    elif cmd == "report_7":    await _send_report(update, 7)
    elif cmd == "alljobs":     await cmd_alljobs(update, ctx)
    elif cmd == "staff":       await cmd_staff(update, ctx)
    elif text == "📋 New Job": await cmd_addjob(update, ctx)


# ═══════════════════════════════════════════════════════════════════════════════
# /start — QR scans + first-time registration
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("This bot only works in private messages.")
        return ConversationHandler.END

    uid  = update.effective_user.id
    args = ctx.args

    if args:
        job_id   = args[0].upper()
        employee = db.get_employee(uid)
        if not is_admin(uid) and (not employee or employee.get("status") == "inactive"):
            ctx.user_data["pending_job"] = job_id
            await update.message.reply_text(
                f"{BRAND}\n\n👋 Welcome! You're not registered yet.\n\nWhat's your name?"
            )
            return WAITING_NAME
        job = db.get_job(job_id)
        if not job:
            await update.message.reply_text(
                f"❌ Job *{escape_md(job_id)}* not found.\n\n"
                "This QR sticker may be outdated. Ask the office manager.",
                parse_mode="Markdown")
            return ConversationHandler.END
        if job.get("status") == "closed":
            await update.message.reply_text(
                f"❌ Job *{escape_md(job_id)}* is closed.\n\n"
                "Ask the office manager if this car still needs work.",
                parse_mode="Markdown")
            return ConversationHandler.END
        if job.get("completed_at") and job.get("status") != "active":
            await update.message.reply_text(
                f"❌ Job *{escape_md(job_id)}* is already marked as done.",
                parse_mode="Markdown")
            return ConversationHandler.END
        await _process_scan(update, ctx, employee, job)
        return ConversationHandler.END

    # Admins always get the admin menu
    if is_admin(uid):
        await _show_admin_menu(update)
        return ConversationHandler.END

    employee = db.get_employee(uid)
    if employee and employee.get("status") != "inactive":
        await update.message.reply_text(
            f"{BRAND}\n\n👋 Hey, *{escape_md(employee['name'])}*!\n\n"
            "Scan the QR sticker on a car to clock in or out. Everything is automatic ✅",
            parse_mode="Markdown", reply_markup=TECH_KB,
        )
    else:
        await update.message.reply_text(f"{BRAND}\n\n👋 Welcome!\n\nWhat's your name?")
        return WAITING_NAME

    return ConversationHandler.END


async def receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Please type your name.")
        return WAITING_NAME
    if len(name) > 50:
        await update.message.reply_text("❌ Too long. Max 50 characters.")
        return WAITING_NAME
    # Reject keyboard button labels as names
    if name in db.BUTTON_NAME_BLACKLIST:
        await update.message.reply_text("❌ Please type your real name.")
        return WAITING_NAME

    # Check for similar existing employee (P4-UX-3)
    similar = db.get_similar_employee(name)
    uid = update.effective_user.id
    if similar and similar["telegram_id"] != uid:
        ctx.user_data["pending_name"] = name
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, add me", callback_data="confirm_name_yes"),
            InlineKeyboardButton("❌ Cancel",       callback_data="confirm_name_no"),
        ]])
        await update.message.reply_text(
            f"⚠️ Similar name already exists: *{escape_md(similar['name'])}*\n\n"
            f"Still register as *{escape_md(name)}*?",
            parse_mode="Markdown", reply_markup=markup,
        )
        return CONFIRM_NAME

    return await _do_register(update, ctx, name)


async def confirm_name_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_name_no":
        await query.edit_message_text("❌ Cancelled. Type your name again.")
        ctx.user_data.pop("pending_name", None)
        # Re-enter WAITING_NAME by editing, but we can't return state from callback
        # So just end — user can /start again
        return ConversationHandler.END
    name = ctx.user_data.pop("pending_name", None)
    if not name:
        await query.edit_message_text("Something went wrong. Please /start again.")
        return ConversationHandler.END
    # Build a fake update to reuse _do_register
    await query.edit_message_text(f"✅ Registering as *{escape_md(name)}*…", parse_mode="Markdown")

    uid = update.effective_user.id
    db.register_employee(uid, name)
    pending = ctx.user_data.pop("pending_job", None)
    if pending:
        job = db.get_job(pending)
        emp = db.get_employee(uid)
        if job and emp:
            await query.message.reply_text(
                f"✅ Registered as *{escape_md(name)}*!", parse_mode="Markdown")
            await _process_scan_msg(query.message, ctx, emp, job)
    else:
        await query.message.reply_text(
            f"✅ Registered as *{escape_md(name)}*!\n\n"
            "📱 Scan a QR code on a windshield to start.",
            parse_mode="Markdown", reply_markup=TECH_KB,
        )
    return ConversationHandler.END


async def _do_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE, name: str):
    uid = update.effective_user.id
    db.register_employee(uid, name)
    pending = ctx.user_data.pop("pending_job", None)
    if pending:
        job = db.get_job(pending)
        emp = db.get_employee(uid)
        if job and emp:
            await update.message.reply_text(
                f"✅ Registered as *{escape_md(name)}*!", parse_mode="Markdown")
            await _process_scan(update, ctx, emp, job)
    else:
        await update.message.reply_text(
            f"✅ Registered as *{escape_md(name)}*!\n\n"
            "📱 Ask your manager for the QR sticker, or scan one on a windshield.",
            parse_mode="Markdown", reply_markup=TECH_KB,
        )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Clock in / Clock out
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE, employee, job):
    await _process_scan_msg(update.message, ctx, employee, job)


async def _process_scan_msg(msg, ctx, employee, job):
    """Core scan logic — works with both Message and reply context."""
    if not employee:
        await msg.reply_text("❌ You're not registered. Please use /start first.")
        return
    emp_id  = employee["telegram_id"]
    open_s  = db.get_open_session(job["id"], emp_id)
    car     = _car_display(job)

    if open_s:
        # ── CLOCK OUT ────────────────────────────────────────────────────────
        result = db.close_session(open_s["id"], open_s["start_time"])
        if result is None:
            # Session < 1 min — deleted silently
            await msg.reply_text(
                "⚡ Session too short to record (less than 1 minute). "
                "Scan the QR again if you meant to clock in.",
                reply_markup=TECH_KB,
            )
            return
        end_time, minutes = result
        await msg.reply_text(
            f"✅ *DONE!*\n\n"
            f"🚗 {escape_md(car)}\n"
            f"⏱ {db.fmt_dur(minutes)}  ·  "
            f"{db.fmt_time(open_s['start_time'])} → {db.fmt_time(end_time)}\n\n"
            f"Great work, {escape_md(employee['name'])}! 🔧",
            parse_mode="Markdown", reply_markup=TECH_KB,
        )
        await notify_admins(
            ctx.bot,
            f"⏹ *Clock Out*\n👤 {escape_md(employee['name'])}\n"
            f"🚗 {escape_md(car)}  ·  {escape_md(job.get('plate') or '—')}\n"
            f"📋 {escape_md(job['id'])}  ·  ⏱ {db.fmt_dur(minutes)}",
            exclude_uid=emp_id,
        )
        return

    # ── Check for open session on another car ─────────────────────────────
    other = db.get_employee_open_session_any(emp_id, exclude_job_id=job["id"])
    if other:
        other_car     = _car_display(other) if other.get("car") else other.get("job_id", "")
        since         = db.fmt_time(other["start_time"])
        elapsed       = db.live_dur(other["start_time"])
        keyboard      = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ Clock out of {other_car} first",
                callback_data=f"sw_close_{other['id']}_{job['id']}_{emp_id}",
            )], [
            InlineKeyboardButton(
                "⚡ I'm working both cars",
                callback_data=f"sw_both_{job['id']}_{emp_id}",
            ),
        ]])
        await msg.reply_text(
            f"⚠️ *Hold on, {escape_md(employee['name'])}!*\n\n"
            f"You're still clocked in on:\n"
            f"🚗 *{escape_md(other_car)}*\n"
            f"🕐 Since {since}  ({elapsed} ago)\n\n"
            f"What do you want to do?",
            parse_mode="Markdown", reply_markup=keyboard,
        )
        return

    # ── CLOCK IN ─────────────────────────────────────────────────────────────
    # First-clock-in greeting (P4-UX-1)
    existing_today = db.get_employee_sessions_today(emp_id)
    greeting = ""
    if not existing_today:
        la_now = db.get_la_now()
        hour = la_now.hour
        if hour < 12:
            greeting = f"Good morning, {escape_md(employee['name'])}! ☀️\n\n"
        elif hour < 17:
            greeting = f"Good afternoon, {escape_md(employee['name'])}! 👋\n\n"
        else:
            greeting = f"Good evening, {escape_md(employee['name'])}! 🌆\n\n"

    session_id, start_time = db.open_session(job["id"], emp_id)

    undo_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Wrong car? Undo", callback_data=f"undo_{session_id}")
    ]]) if session_id else None

    await msg.reply_text(
        f"{greeting}▶️ *ON THE CLOCK!*\n\n"
        f"🚗 {escape_md(car)}\n"
        f"🕐 Started: {db.fmt_time(start_time)}\n\n"
        f"Scan QR again when done 👍",
        parse_mode="Markdown", reply_markup=undo_markup,
    )
    await notify_admins(
        ctx.bot,
        f"▶️ *Clock In*\n👤 {escape_md(employee['name'])}\n"
        f"🚗 {escape_md(car)}  ·  {escape_md(job.get('plate') or '—')}\n"
        f"📋 {escape_md(job['id'])}  ·  🕐 {db.fmt_time(start_time)}",
        exclude_uid=emp_id,
    )


async def handle_undo_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        session_id = int(query.data[len("undo_"):])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid undo request.")
        return
    sess = db.get_session(session_id)
    if not sess:
        await query.edit_message_text("Session not found or already removed.")
        return
    if sess.get("end_time"):
        await query.edit_message_text("⏰ Already clocked out. Cannot undo.")
        return
    elapsed = db.elapsed_minutes(sess["start_time"])
    if elapsed >= 3:
        await query.edit_message_text(
            f"⏰ Too late to undo ({elapsed} min have passed). Session is recorded.")
        return
    db.delete_session(session_id)
    await query.edit_message_text("↩️ Undone. No session recorded.")


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
        result = db.close_session(old_id, old_s["start_time"])
        old_min = result[1] if result else 0
        session_id, start_time = db.open_session(new_job_id, emp_id)
        car = _car_display(new_j)
        await query.edit_message_text(
            f"✅ *Done!*\n\n"
            f"⏹ Clocked out of previous car  ({db.fmt_dur(old_min)})\n\n"
            f"▶️ *ON THE CLOCK*\n"
            f"🚗 {escape_md(car)}\n"
            f"🕐 Started: {db.fmt_time(start_time)}",
            parse_mode="Markdown",
        )

    elif parts[1] == "both":
        new_job_id, emp_id = parts[2], int(parts[3])
        new_j = db.get_job(new_job_id)
        emp   = db.get_employee(emp_id)
        if not new_j or not emp:
            await query.edit_message_text("❌ Data not found. Scan again.")
            return
        session_id, start_time = db.open_session(new_job_id, emp_id)
        car = _car_display(new_j)
        await query.edit_message_text(
            f"▶️ *ON THE CLOCK (both cars)*\n\n"
            f"🚗 {escape_md(car)}\n"
            f"🕐 Started: {db.fmt_time(start_time)}\n\n"
            f"_Scan each QR separately to clock out._",
            parse_mode="Markdown",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Admin keyboard buttons + help
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        return
    if not is_admin(update.effective_user.id):
        return
    cmd = BUTTON_COMMANDS.get(update.message.text)
    if cmd == "shop_status":
        await cmd_shop_status(update, ctx)
    elif cmd == "report_1":
        await _send_report(update, 1)
    elif cmd == "report_7":
        await _send_report(update, 7)
    elif cmd == "alljobs":
        await cmd_alljobs(update, ctx)
    elif cmd == "staff":
        await cmd_staff(update, ctx)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        return
    if is_admin(update.effective_user.id):
        await _show_admin_menu(update)


# ═══════════════════════════════════════════════════════════════════════════════
# Add Job conversation — 5 steps: RO → Year → Car → Plate → Client
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_addjob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ctx.user_data.pop("new_job", None)
    await update.message.reply_text(
        "*New Repair Order*\n\nStep 1/5 — RO number:\n_(e.g. RO-1043)_\n\n"
        "_Type /cancel to stop._",
        parse_mode="Markdown", reply_markup=ADMIN_KB,
    )
    return ADD_JOB_ID


def _is_cancel(text: str) -> bool:
    return text.lower() in ("/cancel", "cancel")


async def addjob_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if _is_cancel(text):
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
        return ConversationHandler.END
    job_id = re.sub(r'[^A-Z0-9\-]', '', text.upper().replace(' ', '-'))
    if not job_id:
        await update.message.reply_text(
            "❌ Invalid RO. Use letters, numbers, dashes (e.g. RO-1043).",
            reply_markup=ADMIN_KB)
        return ADD_JOB_ID
    if len(job_id) > 30:
        await update.message.reply_text("❌ Too long. Max 30 characters.", reply_markup=ADMIN_KB)
        return ADD_JOB_ID
    if db.get_job(job_id):
        await update.message.reply_text(
            f"⚠️ *{escape_md(job_id)}* already exists. Enter a different RO number:",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
        return ADD_JOB_ID
    ctx.user_data["new_job"] = {"id": job_id}
    la_now = db.get_la_now()
    await update.message.reply_text(
        f"✅ {escape_md(job_id)}\n\n"
        f"Step 2/5 — Vehicle year:\n_(e.g. {la_now.year})_\n_(Required — cannot skip)_",
        parse_mode="Markdown", reply_markup=ADMIN_KB)
    return ADD_JOB_YEAR


async def addjob_year(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if _is_cancel(text):
        ctx.user_data.pop("new_job", None)
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
        return ConversationHandler.END
    if text.lower() == "/skip":
        await update.message.reply_text(
            "❌ Year is required. Please enter the vehicle year (e.g. 2023).",
            reply_markup=ADMIN_KB)
        return ADD_JOB_YEAR
    la_now = db.get_la_now()
    if not re.match(r'^\d{4}$', text) or not (1990 <= int(text) <= la_now.year + 1):
        await update.message.reply_text(
            f"❌ Enter a valid year between 1990 and {la_now.year + 1}.",
            reply_markup=ADMIN_KB)
        return ADD_JOB_YEAR
    ctx.user_data["new_job"]["year"] = text
    await update.message.reply_text(
        "Step 3/5 — Make & model:\n_(e.g. Mazda CX-30)_\n_(Required — cannot skip)_",
        parse_mode="Markdown", reply_markup=ADMIN_KB)
    return ADD_JOB_CAR


async def addjob_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if _is_cancel(text):
        ctx.user_data.pop("new_job", None)
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
        return ConversationHandler.END
    if not text or text.lower() == "/skip":
        await update.message.reply_text(
            "❌ Make & model is required. Please type it.",
            reply_markup=ADMIN_KB)
        return ADD_JOB_CAR
    if len(text) > 100:
        await update.message.reply_text("❌ Too long. Max 100 characters.", reply_markup=ADMIN_KB)
        return ADD_JOB_CAR
    ctx.user_data["new_job"]["car"] = text
    await update.message.reply_text(
        "Step 4/5 — License plate:\n_(or /skip)_",
        parse_mode="Markdown", reply_markup=ADMIN_KB)
    return ADD_JOB_PLATE


async def addjob_plate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if _is_cancel(text):
        ctx.user_data.pop("new_job", None)
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
        return ConversationHandler.END
    if len(text) > 20:
        await update.message.reply_text("❌ Too long. Max 20 characters.", reply_markup=ADMIN_KB)
        return ADD_JOB_PLATE
    ctx.user_data["new_job"]["plate"] = "" if text.lower() == "/skip" else text.upper()
    await update.message.reply_text(
        "Step 5/5 — Customer name:\n_(or /skip)_",
        parse_mode="Markdown", reply_markup=ADMIN_KB)
    return ADD_JOB_CLIENT


async def addjob_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if _is_cancel(text):
        ctx.user_data.pop("new_job", None)
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
        return ConversationHandler.END
    if len(text) > 100:
        await update.message.reply_text("❌ Too long. Max 100 characters.", reply_markup=ADMIN_KB)
        return ADD_JOB_CLIENT
    j = ctx.user_data.pop("new_job", {})
    j["client"] = "" if text.lower() == "/skip" else text
    db.add_job(j["id"], j["car"], j.get("plate", ""), j.get("client", ""),
               year=j.get("year", ""))
    car_display = f"{j.get('year', '')} {j['car']}".strip()
    qr_link = f"https://t.me/{BOT_USERNAME}?start={j['id']}"
    lines = [
        f"✅ *Job created!*\n",
        f"📋 *{escape_md(j['id'])}*",
        f"🚗 {escape_md(car_display)}  ·  {escape_md(j.get('plate') or '—')}",
    ]
    if j.get("client"):
        lines.append(f"👤 {escape_md(j['client'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=ADMIN_KB)
    await _send_qr(update.message, j["id"], car_display, j.get("plate", ""), qr_link)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("new_job", None)
    ctx.user_data.pop("editing_session_id", None)
    ctx.user_data.pop("editing_date", None)
    ctx.user_data.pop("editing_mode", None)
    ctx.user_data.pop("editing_job_id", None)
    ctx.user_data.pop("editing_field", None)
    ctx.user_data.pop("renaming_emp_id", None)
    await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
    return ConversationHandler.END


async def conv_cmd_escape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fallback: admin sent a slash command inside a conversation. Dispatch it."""
    ctx.user_data.pop("new_job", None)
    _cmd_map = {
        "closejob":    cmd_closejob,
        "qrlink":      cmd_qrlink,
        "status":      cmd_shop_status,
        "report":      cmd_report,
        "staff":       cmd_staff,
        "removestaff": cmd_removestaff,
        "alljobs":     cmd_alljobs,
        "help":        cmd_help,
        "mystats":     cmd_mystats,
        "clockout":    cmd_clockout,
        "myname":      cmd_myname,
    }
    raw   = update.message.text or ""
    parts = raw.lstrip("/").split(None, 1)
    cmd   = parts[0].lower() if parts else ""
    ctx.args = parts[1].split() if len(parts) > 1 else []
    fn = _cmd_map.get(cmd)
    if fn:
        await fn(update, ctx)
    else:
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Shop Status (P1-BUG-6: one Fix button per open session)
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
            "🏁 Nothing in progress right now.\nTap 📋 New Job to create a repair order.",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
        return

    all_open       = db.get_all_open_sessions()
    today_sessions = db.get_sessions_today()
    today_closed   = [s for s in today_sessions if s.get("end_time") and (s.get("duration_minutes") or 0) > 0]

    open_job_ids      = {s["job_id"] for s in all_open}
    completed_job_ids = {s["job_id"] for s in today_closed if s["job_id"] not in open_job_ids}

    in_progress     = [j for j in jobs if j["id"] in open_job_ids]
    completed_today = [j for j in jobs if j["id"] in completed_job_ids]
    idle            = [j for j in jobs if j["id"] not in open_job_ids and j["id"] not in completed_job_ids]

    lines      = [f"🚗 *SHOP STATUS — {date_str}*\n"]
    fix_buttons = []   # [InlineKeyboardButton] per open session

    # ── IN PROGRESS ───────────────────────────────────────────────────────────
    if in_progress:
        lines.append("━━━ 🟢 IN PROGRESS ━━━\n")
        for j in in_progress:
            car = _car_display(j)
            hdr = f"*{escape_md(j['id'])}*  ·  {escape_md(car)}"
            if j["plate"]:
                hdr += f"  ·  {escape_md(j['plate'])}"
            # Job start date
            first = db.get_job_first_session(j["id"])
            if first:
                hdr += f"\n  📅 Started: {db.fmt_date(first)}"
            lines.append(hdr)
            for s in (s for s in all_open if s["job_id"] == j["id"]):
                elapsed = db.elapsed_minutes(s["start_time"])
                warn    = "  ⚠️ _Long session_" if elapsed >= 8 * 60 else ""
                lines.append(
                    f"  › {escape_md(s['emp_name'])}: {db.fmt_dur(elapsed)}"
                    f" (since {db.fmt_time(s['start_time'])}){warn}"
                )
                # One Fix button per open session
                fix_buttons.append([InlineKeyboardButton(
                    f"✏️ Fix: {s['emp_name']} on {car}",
                    callback_data=f"fix_session_{s['id']}",
                )])
            lines.append("")

    # ── COMPLETED TODAY ───────────────────────────────────────────────────────
    if completed_today:
        lines.append("━━━ ✅ COMPLETED TODAY ━━━\n")
        for j in completed_today:
            car = _car_display(j)
            hdr = f"*{escape_md(j['id'])}*  ·  {escape_md(car)}"
            if j["plate"]:
                hdr += f"  ·  {escape_md(j['plate'])}"
            lines.append(hdr)
            for s in (s for s in today_closed if s["job_id"] == j["id"]):
                lines.append(
                    f"  {escape_md(s['emp_name'])}: "
                    f"{db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
                    f"  ({db.fmt_dur(s['duration_minutes'])})"
                )
            lines.append("")

    # ── IDLE ──────────────────────────────────────────────────────────────────
    if idle:
        lines.append("━━━ ⬜ IDLE (no work today) ━━━\n")
        for j in idle:
            total = db.get_job_total_minutes(j["id"])
            car   = _car_display(j)
            line  = f"  {escape_md(j['id'])}  ·  {escape_md(car)}"
            if j.get("client"):
                line += f"  ·  {escape_md(j['client'])}"
            if total:
                line += f"  ·  ⏱ {db.fmt_dur(total)} all-time"
            lines.append(line)
        lines.append("")

    text = "\n".join(lines)
    # Always send text with ADMIN_KB, then Fix buttons separately if any
    await send_safe(update.message.reply_text, text, parse_mode="Markdown", reply_markup=ADMIN_KB)
    if fix_buttons:
        await update.message.reply_text(
            "⚡ *Quick actions* — tap to edit a session:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(fix_buttons),
        )


async def handle_fix_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show sub-menu to choose what to fix for a specific session."""
    query = update.callback_query
    await query.answer()
    try:
        session_id = int(query.data[len("fix_session_"):])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid session.")
        return

    sess = db.get_session(session_id)
    if not sess:
        await query.edit_message_text("Session not found.")
        return
    emp  = db.get_employee(sess["employee_id"])
    job  = db.get_job(sess["job_id"])
    emp_name = emp["name"] if emp else "?"
    car_name = _car_display(job) if job else sess["job_id"]

    is_open = not sess.get("end_time")
    if is_open:
        status_str = f"🟢 Started {db.fmt_time(sess['start_time'])} · {db.live_dur(sess['start_time'])}"
    else:
        status_str = f"{db.fmt_time(sess['start_time'])} → {db.fmt_time(sess['end_time'])} ({db.fmt_dur(sess['duration_minutes'])})"

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Fix Check-in",  callback_data=f"adm_edit_start_{session_id}"),
            InlineKeyboardButton("✏️ Fix Check-out", callback_data=f"adm_edit_{session_id}"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="fix_cancel")],
    ])
    await query.message.reply_text(
        f"✏️ *Edit session*\n"
        f"👤 {escape_md(emp_name)} → {escape_md(car_name)}\n"
        f"🕐 {escape_md(status_str)}\n\n"
        f"What do you want to fix?",
        parse_mode="Markdown",
        reply_markup=markup,
    )


async def handle_fix_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Cancelled.", reply_markup=None)


# ═══════════════════════════════════════════════════════════════════════════════
# Edit session time conversation
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_admin_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    parts  = query.data.split("_")
    action = parts[1]  # "close" or "edit"

    editing_start = False
    if action == "edit":
        if len(parts) > 3 and parts[2] == "start":
            editing_start = True
            session_id    = int(parts[3])
        else:
            session_id = int(parts[2])
    else:
        session_id = int(parts[2])

    sess = db.get_session(session_id)
    if not sess:
        await query.edit_message_text("Session not found.")
        return

    if action == "close":
        result = db.close_session(session_id, sess["start_time"])
        if result:
            _, minutes = result
            await query.edit_message_text(
                f"✅ Closed. Duration: *{db.fmt_dur(minutes)}*",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("Session was < 1 min, deleted.")
        return

    # action == "edit"
    emp      = db.get_employee(sess["employee_id"])
    job      = db.get_job(sess["job_id"])
    emp_name = emp["name"] if emp else "?"
    car_name = _car_display(job) if job else sess["job_id"]

    sess_date = db.la_date(sess["start_time"])
    date_str  = sess_date.strftime("%b %-d") if sess_date else "today"

    ctx.user_data["editing_session_id"] = session_id
    ctx.user_data["editing_mode"]       = "start" if editing_start else "end"
    ctx.user_data["editing_date"]       = sess_date

    cancel_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel editing", callback_data="adm_cancel_edit")
    ]])

    if editing_start:
        msg = (
            f"✏️ *Edit check-in time*\n\n"
            f"👤 {escape_md(emp_name)}  →  {escape_md(car_name)}\n"
            f"📅 Session: *{date_str}*\n"
            f"🕐 Currently: *{db.fmt_time(sess['start_time'])}*\n"
        )
        if sess.get("end_time"):
            msg += f"🕑 Check-out: {db.fmt_time(sess['end_time'])}\n"
        msg += f"\nType the correct *check-in* time for {date_str} (LA time):\n_(e.g.  9:30 AM  or  09:30)_"
    else:
        msg = (
            f"✏️ *Edit check-out time*\n\n"
            f"👤 {escape_md(emp_name)}  →  {escape_md(car_name)}\n"
            f"📅 Session: *{date_str}*\n"
            f"🕐 Clocked in at: {db.fmt_time(sess['start_time'])}\n\n"
            f"Type the correct *check-out* time for {date_str} (LA time):\n_(e.g.  5:30 PM  or  17:30)_"
        )
    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_markup)
    return EDITING_SESSION_TIME


async def cancel_edit_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Edit cancelled.")
    await query.edit_message_text("❌ Edit cancelled.")
    ctx.user_data.pop("editing_session_id", None)
    ctx.user_data.pop("editing_mode", None)
    ctx.user_data.pop("editing_date", None)
    return ConversationHandler.END


async def receive_edited_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END

    session_id   = ctx.user_data.pop("editing_session_id", None)
    mode         = ctx.user_data.pop("editing_mode", "end")
    editing_date = ctx.user_data.pop("editing_date", None)
    if not session_id:
        return ConversationHandler.END

    sess    = db.get_session(session_id)
    new_str = db.parse_time_input(text, date_la=editing_date)
    if not new_str or not sess:
        await update.message.reply_text(
            "❌ Couldn't parse time. Try again:\n_(e.g.  5:30 PM  or  17:30)_",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
        ctx.user_data["editing_session_id"] = session_id
        ctx.user_data["editing_mode"]       = mode
        ctx.user_data["editing_date"]       = editing_date
        return EDITING_SESSION_TIME

    new_dt_utc = datetime.strptime(new_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    la_now     = db.get_la_now()
    if new_dt_utc > la_now.astimezone(timezone.utc):
        await update.message.reply_text(
            f"❌ That time is in the future. Current LA time is *{la_now.strftime('%-I:%M %p')}*.",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
        ctx.user_data["editing_session_id"] = session_id
        ctx.user_data["editing_mode"]       = mode
        ctx.user_data["editing_date"]       = editing_date
        return EDITING_SESSION_TIME

    if mode == "end":
        start_dt_utc = datetime.strptime(
            str(sess["start_time"])[:19], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        if new_dt_utc <= start_dt_utc:
            await update.message.reply_text(
                f"❌ End time must be *after* start time (started at *{db.fmt_time(sess['start_time'])}*).",
                parse_mode="Markdown", reply_markup=ADMIN_KB)
            ctx.user_data["editing_session_id"] = session_id
            ctx.user_data["editing_mode"]       = mode
            ctx.user_data["editing_date"]       = editing_date
            return EDITING_SESSION_TIME

    if mode == "start":
        db.update_session_start(session_id, new_str)
        updated = db.get_session(session_id)
        dur_str = db.fmt_dur(updated["duration_minutes"]) if updated and updated.get("duration_minutes") else "—"
        await update.message.reply_text(
            f"✅ *Check-in updated!*\n"
            f"🕐 New check-in: *{db.fmt_time(new_str)}*\n"
            f"⏱ Duration: *{dur_str}*",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
    else:
        result = db.close_session(session_id, sess["start_time"], end_str=new_str)
        if result:
            _, minutes = result
            await update.message.reply_text(
                f"✅ *Check-out updated!*\n"
                f"🕑 New check-out: *{db.fmt_time(new_str)}*\n"
                f"⏱ Duration: *{db.fmt_dur(minutes)}*",
                parse_mode="Markdown", reply_markup=ADMIN_KB)
        else:
            await update.message.reply_text(
                "✅ Updated (session was too short to save, deleted).",
                reply_markup=ADMIN_KB)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Job management
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_closejob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not is_private(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/closejob RO-1043`",
                                        parse_mode="Markdown", reply_markup=ADMIN_KB)
        return
    job_id = ctx.args[0].upper()
    if not db.get_job(job_id):
        await update.message.reply_text(f"Job *{escape_md(job_id)}* not found.",
                                        parse_mode="Markdown", reply_markup=ADMIN_KB)
        return
    active = db.get_active_sessions_for_job(job_id)
    if active:
        names = ", ".join(s["emp_name"] for s in active)
        db.auto_close_sessions_for_job(job_id)
        db.close_job(job_id)
        await update.message.reply_text(
            f"⚠️ {escape_md(names)} was still clocked in — session auto-closed.\n"
            f"✅ Job *{escape_md(job_id)}* closed.",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
        return
    db.close_job(job_id)
    await update.message.reply_text(
        f"✅ Job *{escape_md(job_id)}* closed.", parse_mode="Markdown", reply_markup=ADMIN_KB)


async def cmd_qrlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not is_private(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/qrlink RO-1043`",
                                        parse_mode="Markdown", reply_markup=ADMIN_KB)
        return
    job_id = ctx.args[0].upper()
    job    = db.get_job(job_id)
    if not job:
        await update.message.reply_text(f"Job *{escape_md(job_id)}* not found.",
                                        parse_mode="Markdown", reply_markup=ADMIN_KB)
        return
    qr_link = f"https://t.me/{BOT_USERNAME}?start={job_id}"
    car     = _car_display(job)
    await _send_qr(update.message, job_id, car, job.get("plate", ""), qr_link)


# ═══════════════════════════════════════════════════════════════════════════════
# All Jobs manager
# ═══════════════════════════════════════════════════════════════════════════════

def _job_card(job):
    """Returns (text, markup) for a single self-contained job message."""
    jid       = job["id"]
    status    = "🟢" if job["status"] == "active" else "🔴"
    total_min = db.get_job_total_minutes(jid)
    sess_cnt  = db.get_job_session_count(jid)
    car       = _car_display(job)

    car_line = f"🚗 {escape_md(car)}"
    if job.get("plate"):
        car_line += f"  ·  {escape_md(job['plate'])}"

    lines = [
        "──────────────────",
        f"{status} *{escape_md(jid)}*",
        car_line,
    ]
    if job.get("client"):
        lines.append(f"👤 {escape_md(job['client'])}")
    if job.get("works"):
        lines.append(f"🔧 {escape_md(job['works'])}")

    # Job start date (P2-FEAT-8)
    first_sess = db.get_job_first_session(jid)
    if first_sess:
        lines.append(f"📅 Started: {db.fmt_date(first_sess)}")

    # Closed date (TECH-5)
    if job.get("completed_at"):
        lines.append(f"🔒 Closed: {db.fmt_date(job['completed_at'])}")

    stats = []
    if total_min:
        stats.append(f"⏱ {db.fmt_dur(total_min)} total")
    if sess_cnt:
        stats.append(f"{sess_cnt} session{'s' if sess_cnt != 1 else ''}")
    if stats:
        lines.append("  ·  ".join(stats))
    lines.append("──────────────────")

    # Buttons
    row1 = [InlineKeyboardButton("🔗 QR Code", callback_data=f"aj_qr_{jid}"),
            InlineKeyboardButton("📊 Report",  callback_data=f"aj_rpt_{jid}")]
    if job["status"] == "active" and not job.get("completed_at"):
        row1.append(InlineKeyboardButton("✅ Close", callback_data=f"aj_close_{jid}"))
    markup = InlineKeyboardMarkup([
        row1,
        [InlineKeyboardButton("✏️ Edit Details", callback_data=f"aj_edit_{jid}")],
        [InlineKeyboardButton("🗑️ Delete",        callback_data=f"aj_del_{jid}")],
    ])
    return "\n".join(lines), markup


async def cmd_alljobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not is_private(update):
        return
    jobs = db.get_all_jobs_all()
    if not jobs:
        await update.message.reply_text("No jobs found.", reply_markup=ADMIN_KB)
        return
    total_pages = (len(jobs) + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE
    ctx.user_data["alljobs_page"] = 0
    await update.message.reply_text(
        f"📋 *ALL JOBS — {len(jobs)} total*"
        + (f"  (page 1/{total_pages})" if total_pages > 1 else ""),
        parse_mode="Markdown", reply_markup=ADMIN_KB)
    for job in jobs[:JOBS_PER_PAGE]:
        text, markup = _job_card(job)
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        await asyncio.sleep(0.05)
    if total_pages > 1:
        nav = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Next page", callback_data="aj_page_1")
        ]])
        await update.message.reply_text(
            f"Page 1/{total_pages} — tap Next for more.", reply_markup=nav)


async def handle_alljobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("aj_page_"):
        page = int(data[len("aj_page_"):])
        jobs = db.get_all_jobs_all()
        ctx.user_data["alljobs_page"] = page
        total_pages = (len(jobs) + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE
        start = page * JOBS_PER_PAGE
        chunk = jobs[start:start + JOBS_PER_PAGE]
        await query.edit_message_text(
            f"📋 *ALL JOBS — {len(jobs)} total*  (page {page+1}/{total_pages})",
            parse_mode="Markdown")
        for job in chunk:
            text, markup = _job_card(job)
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
            await asyncio.sleep(0.05)
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"aj_page_{page-1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("▶️ Next", callback_data=f"aj_page_{page+1}"))
        if nav_row:
            await query.message.reply_text(
                f"Page {page+1}/{total_pages}", reply_markup=InlineKeyboardMarkup([nav_row]))
        return

    if data.startswith("aj_qr_"):
        job_id = data[len("aj_qr_"):]
        job    = db.get_job(job_id)
        if not job:
            await query.answer("Job not found.", show_alert=True)
            return
        qr_link = f"https://t.me/{BOT_USERNAME}?start={job_id}"
        car     = _car_display(job)
        await _send_qr(query.message, job_id, car, job.get("plate", ""), qr_link)
        return

    if data.startswith("aj_rpt_"):
        job_id = data[len("aj_rpt_"):]
        await _send_job_report(query.message, job_id)
        return

    if data.startswith("aj_edit_") and not data.startswith("aj_editfield_"):
        job_id = data[len("aj_edit_"):]
        job    = db.get_job(job_id)
        if not job:
            await query.answer("Job not found.", show_alert=True)
            return
        car = _car_display(job)
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 Year",    callback_data=f"aj_editfield_{job_id}_year"),
                InlineKeyboardButton("🚗 Car",     callback_data=f"aj_editfield_{job_id}_car"),
            ],
            [
                InlineKeyboardButton("🔢 Plate",   callback_data=f"aj_editfield_{job_id}_plate"),
                InlineKeyboardButton("👤 Client",  callback_data=f"aj_editfield_{job_id}_client"),
            ],
            [
                InlineKeyboardButton("🔧 Works",   callback_data=f"aj_editfield_{job_id}_works"),
            ],
            [InlineKeyboardButton("❌ Cancel",     callback_data=f"aj_editcancel_{job_id}")],
        ])
        await query.edit_message_text(
            f"✏️ *Edit: {escape_md(job_id)}*\n🚗 {escape_md(car)}\n\nWhat do you want to change?",
            parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("aj_editcancel_"):
        job_id = data[len("aj_editcancel_"):]
        job    = db.get_job(job_id)
        if not job:
            await query.edit_message_text("Job not found.")
            return
        text, markup = _job_card(job)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("aj_close_"):
        job_id = data[len("aj_close_"):]
        job    = db.get_job(job_id)
        if not job:
            await query.answer("Job not found.", show_alert=True)
            return
        if job["status"] != "active":
            await query.answer("Already closed.", show_alert=True)
            return
        active = db.get_active_sessions_for_job(job_id)
        if active:
            names = ", ".join(s["emp_name"] for s in active)
            db.auto_close_sessions_for_job(job_id)
            await query.answer(f"⚠️ Auto-closed {names}'s session first.", show_alert=True)
        db.close_job(job_id)
        updated = db.get_job(job_id)
        text, markup = _job_card(updated)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("aj_del_ok_"):
        job_id = data[len("aj_del_ok_"):]
        job    = db.get_job(job_id)
        car    = escape_md(_car_display(job)) if job else escape_md(job_id)
        db.auto_close_sessions_for_job(job_id)
        db.delete_job(job_id)
        await query.edit_message_text(
            f"🗑️ *{escape_md(job_id)} ({car}) deleted.*", parse_mode="Markdown")
        return

    if data.startswith("aj_del_cancel_"):
        job_id = data[len("aj_del_cancel_"):]
        job    = db.get_job(job_id)
        if not job:
            await query.edit_message_text("Job no longer exists.")
            return
        text, markup = _job_card(job)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("aj_del_"):
        job_id   = data[len("aj_del_"):]
        job      = db.get_job(job_id)
        car_str  = escape_md(_car_display(job)) if job else escape_md(job_id)
        sess_cnt = db.get_job_session_count(job_id)
        warn     = f"\n⚠️ {sess_cnt} time record(s) will also be deleted." if sess_cnt else ""
        markup   = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"aj_del_ok_{job_id}"),
            InlineKeyboardButton("❌ Cancel",       callback_data=f"aj_del_cancel_{job_id}"),
        ]])
        await query.edit_message_text(
            f"⚠️ *Delete {escape_md(job_id)} ({car_str})?*\n\n"
            f"All time records will be lost forever.{warn}",
            parse_mode="Markdown", reply_markup=markup)
        return


# ═══════════════════════════════════════════════════════════════════════════════
# Edit job field conversation (P2-FEAT-2)
# ═══════════════════════════════════════════════════════════════════════════════

_FIELD_LABELS = {
    "year":   ("📅 Year",   "Enter the vehicle year (e.g. 2023):"),
    "car":    ("🚗 Car",    "Enter make & model (e.g. Mazda CX-30):"),
    "plate":  ("🔢 Plate",  "Enter license plate (or /skip to clear):"),
    "client": ("👤 Client", "Enter customer name (or /skip to clear):"),
    "works":  ("🔧 Works",  "Enter work description (or /skip to clear):"),
}


async def start_edit_job_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point for edit job conversation — triggered by aj_editfield_ callback."""
    query = update.callback_query
    await query.answer()
    # "aj_editfield_{job_id}_{field}" — field names never contain underscores
    rest   = query.data[len("aj_editfield_"):]   # "{job_id}_{field}"
    field  = rest.rsplit("_", 1)[1]
    job_id = rest.rsplit("_", 1)[0]

    if field not in _FIELD_LABELS:
        await query.edit_message_text("Unknown field.")
        return ConversationHandler.END

    job = db.get_job(job_id)
    if not job:
        await query.edit_message_text("Job not found.")
        return ConversationHandler.END

    label, prompt = _FIELD_LABELS[field]
    ctx.user_data["editing_job_id"] = job_id
    ctx.user_data["editing_field"]  = field

    cancel_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"aj_editcancel_{job_id}")
    ]])
    await query.message.reply_text(
        f"✏️ *Edit {label} for {escape_md(job_id)}*\n\n{prompt}",
        parse_mode="Markdown", reply_markup=cancel_markup,
    )
    return EDIT_JOB_FIELD


async def receive_edited_job_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    job_id = ctx.user_data.pop("editing_job_id", None)
    field  = ctx.user_data.pop("editing_field", None)

    if not job_id or not field:
        return ConversationHandler.END

    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END

    skipped = text.lower() == "/skip"
    value   = "" if skipped else text

    # Validate year
    if field == "year":
        la_now = db.get_la_now()
        if skipped:
            await update.message.reply_text("❌ Year is required and cannot be cleared.", reply_markup=ADMIN_KB)
            ctx.user_data["editing_job_id"] = job_id
            ctx.user_data["editing_field"]  = field
            return EDIT_JOB_FIELD
        if not re.match(r'^\d{4}$', value) or not (1990 <= int(value) <= la_now.year + 1):
            await update.message.reply_text(
                f"❌ Enter a valid year between 1990 and {la_now.year + 1}.",
                reply_markup=ADMIN_KB)
            ctx.user_data["editing_job_id"] = job_id
            ctx.user_data["editing_field"]  = field
            return EDIT_JOB_FIELD

    if field == "car" and (not value or skipped):
        await update.message.reply_text("❌ Car make & model cannot be empty.", reply_markup=ADMIN_KB)
        ctx.user_data["editing_job_id"] = job_id
        ctx.user_data["editing_field"]  = field
        return EDIT_JOB_FIELD

    try:
        db.update_job_field(job_id, field, value)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=ADMIN_KB)
        return ConversationHandler.END

    label = _FIELD_LABELS[field][0]
    job   = db.get_job(job_id)
    await update.message.reply_text(
        f"✅ *{label} updated* for {escape_md(job_id)}!\n"
        f"New value: {escape_md(value) if value else '_(cleared)_'}",
        parse_mode="Markdown", reply_markup=ADMIN_KB)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Job Report (P3-RPT-3)
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_job_report(msg, job_id):
    job = db.get_job(job_id)
    if not job:
        await msg.reply_text("Job not found.", reply_markup=ADMIN_KB)
        return

    car      = _car_display(job)
    sessions = db.get_sessions_for_job(job_id)
    status   = "🟢 Active" if job["status"] == "active" else "🔴 Closed"

    lines = [
        f"📊 *JOB REPORT · {escape_md(job_id)}*",
        f"🚗 {escape_md(car)}" + (f"  ·  {escape_md(job['plate'])}" if job.get("plate") else ""),
    ]
    if job.get("client"):
        lines.append(f"👤 {escape_md(job['client'])}")
    lines.append(f"📅 Created: {db.fmt_date_only(job.get('created_at'))}")

    first = db.get_job_first_session(job_id)
    if first:
        lines.append(f"📅 First session: {db.fmt_date(first)}")
    else:
        lines.append("📅 First session: Not started yet")
    lines.append(f"Status: {status}")

    # Group sessions by day, then by employee
    by_day = defaultdict(lambda: defaultdict(list))
    valid_sessions = [s for s in sessions if s.get("end_time") and (s.get("duration_minutes") or 0) > 0]
    for s in valid_sessions:
        d   = db.la_date(s["start_time"])
        emp = s["emp_name"]
        if d:
            by_day[d][emp].append(s)

    # By technician summary
    emp_totals = defaultdict(int)
    for s in valid_sessions:
        emp_totals[s["emp_name"]] += s["duration_minutes"] or 0

    if emp_totals:
        lines.append("\n━━ BY TECHNICIAN ━━")
        for emp, total in sorted(emp_totals.items(), key=lambda x: -x[1]):
            cnt = sum(1 for s in valid_sessions if s["emp_name"] == emp)
            lines.append(f"  👤 {escape_md(emp)}: {db.fmt_dur(total)} ({cnt} session{'s' if cnt != 1 else ''})")

    # Sessions by date
    if by_day:
        lines.append("\n━━ SESSIONS BY DATE ━━")
        for day in sorted(by_day.keys(), reverse=True):
            lines.append(f"\n{day.strftime('%b %-d')}:")
            for emp, emp_sessions in by_day[day].items():
                for s in emp_sessions:
                    dur = s.get("duration_minutes") or 0
                    lines.append(
                        f"  {escape_md(s['emp_name'])}: "
                        f"{db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
                        f"  ({db.fmt_dur(dur)})"
                        + ("  ⚠️ _verify_" if dur >= 8 * 60 else "")
                    )

    total_all = sum(emp_totals.values())
    lines.append(f"\n⏱ *TOTAL: {db.fmt_dur(total_all)}*")

    # Same plate in other jobs (P4-UX-2)
    if job.get("plate"):
        others = db.get_jobs_by_plate(job["plate"], exclude_job_id=job_id)
        if others:
            lines.append(f"\n🔍 *Same plate {escape_md(job['plate'])} in other jobs:*")
            for o in others[:5]:
                o_car    = _car_display(o)
                o_status = "active" if o["status"] == "active" else "closed"
                o_date   = db.fmt_date_only(o.get("created_at"))
                lines.append(f"  {escape_md(o['id'])} · {escape_md(o_car)} · {o_date} ({o_status})")

    text = "\n".join(lines)
    await send_safe(msg.reply_text, text, parse_mode="Markdown", reply_markup=ADMIN_KB)


# ═══════════════════════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════════════════════

def _build_report(days):
    by_job       = db.get_report_data(days)
    open_sessions = db.get_all_open_sessions()
    la_now        = db.get_la_now()

    if days == 1:
        # TODAY REPORT — summary at top (P3-RPT-4)
        date_str = la_now.strftime("%b %-d")
        text = f"📊 *MAGIC AUTO CENTER — {date_str}*\n"

        # Gather summary data
        all_emp_names = set()
        emp_totals    = defaultdict(int)
        for jid, d in by_job.items():
            for emp, ed in d["by_emp"].items():
                all_emp_names.add(emp)
                emp_totals[emp] += ed["total"]
        for s in open_sessions:
            all_emp_names.add(s["emp_name"])

        total_all = sum(v["total"] for v in by_job.values())
        n_jobs    = len(by_job)
        n_techs   = len(all_emp_names)

        text += "━━━━━━━━━━━━━━━━━━━━\n"
        text += f"🚗 {n_jobs} job(s)  ·  👥 {n_techs} tech(s)\n"
        text += f"⏱ Total: *{db.fmt_dur(total_all)}*\n\n"
        for emp, total in sorted(emp_totals.items(), key=lambda x: -x[1]):
            dashes = "─" * max(1, 20 - len(emp))
            text += f"{escape_md(emp)} {dashes} {db.fmt_dur(total)}\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"

        # Job details (P3-RPT-1: per-tech breakdown)
        if by_job:
            for jid, d in by_job.items():
                text += f"🚗 *{escape_md(jid)}*  {escape_md(d['car'])}"
                if d["plate"]:
                    text += f"  ·  {escape_md(d['plate'])}"
                text += "\n"
                for emp, ed in d["by_emp"].items():
                    cnt = len(ed["rows"])
                    text += f"  👤 {escape_md(emp)}: {db.fmt_dur(ed['total'])} ({cnt} session{'s' if cnt != 1 else ''})\n"
                    # Flag suspicious sessions (P3-RPT-5)
                    for r in ed["rows"]:
                        dur = r.get("duration_minutes") or 0
                        if dur >= 8 * 60:
                            text += (
                                f"  ⚠️ {escape_md(emp)}: "
                                f"{db.fmt_time(r['start_time'])} → {db.fmt_time(r['end_time'])}"
                                f" ({db.fmt_dur(dur)} — verify this)\n"
                            )
                text += f"  ⏱ *Total: {db.fmt_dur(d['total'])}*\n\n"
        else:
            text += f"No completed sessions today.\n\n"

    elif days <= 7:
        # 7-DAY REPORT — broken by day (P3-RPT-2)
        since_date = (la_now - timedelta(days=days - 1)).date()
        end_date   = la_now.date()
        text = (
            f"📆 *7-DAY REPORT*\n"
            f"{since_date.strftime('%b %-d')} — {end_date.strftime('%b %-d')}\n\n"
        )
        by_day     = db.get_report_data_by_day(days)
        grand_total = 0

        # Days in reverse order, skip empty days
        for i in range(days - 1, -1, -1):
            day = (la_now - timedelta(days=i)).date()
            if day not in by_day:
                continue
            day_data  = by_day[day]
            day_total = day_data["total"]
            grand_total += day_total
            text += f"📅 *{day.strftime('%a %b %-d')}:*\n"
            for jid, jd in day_data["jobs"].items():
                text += f"  🚗 {escape_md(jd['car'])}"
                if jd["plate"]:
                    text += f"  ·  {escape_md(jd['plate'])}"
                text += "\n"
                parts_str = []
                for emp, ed in jd["by_emp"].items():
                    parts_str.append(f"{escape_md(emp)}: {db.fmt_dur(ed['total'])}")
                text += "    " + "  ·  ".join(parts_str) + "\n"
            text += f"  📊 Day total: *{db.fmt_dur(day_total)}*\n\n"

        text += f"━━ *7-day total: {db.fmt_dur(grand_total)}* ━━\n"

    else:
        # 30-DAY REPORT — grouped by week (P4-UX-4)
        text = f"📅 *30-DAY REPORT*\n\n"
        by_day = db.get_report_data_by_day(days)
        grand_total = 0

        # Group into weeks
        weeks = defaultdict(lambda: {"total": 0, "jobs": set(), "techs": set()})
        for day, dd in by_day.items():
            # Week starts on Monday
            week_start = day - timedelta(days=day.weekday())
            weeks[week_start]["total"] += dd["total"]
            for jid, jd in dd["jobs"].items():
                weeks[week_start]["jobs"].add(jid)
                for emp in jd["by_emp"]:
                    weeks[week_start]["techs"].add(emp)
            grand_total += dd["total"]

        for week_start in sorted(weeks.keys(), reverse=True):
            week_end  = week_start + timedelta(days=6)
            wd        = weeks[week_start]
            text += (
                f"*Week {week_start.strftime('%b %-d')}–{week_end.strftime('%-d')}:*  "
                f"{db.fmt_dur(wd['total'])}  "
                f"({len(wd['jobs'])} job{'s' if len(wd['jobs']) != 1 else ''}, "
                f"{len(wd['techs'])} tech{'s' if len(wd['techs']) != 1 else ''})\n"
            )

        text += f"\n━━ *Month total: {db.fmt_dur(grand_total)}* ━━\n"

    # In-progress sessions (all day counts)
    if open_sessions and days == 1:
        text += "\n⏳ *Still in progress (not counted):*\n"
        for s in open_sessions:
            car = _car_display(s) if s.get("car") else s.get("job_id", "")
            text += f"  · {escape_md(s['emp_name'])} on {escape_md(car)} — {db.live_dur(s['start_time'])}\n"

    return text.strip()


async def _send_report(update: Update, days: int):
    text = _build_report(days)
    await send_safe(update.message.reply_text, text, parse_mode="Markdown", reply_markup=ADMIN_KB)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    days = int(ctx.args[0]) if ctx.args else 1
    await _send_report(update, days)


# ═══════════════════════════════════════════════════════════════════════════════
# Technician: My Today / My History / /clockout / /mystats / /myname
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_tech_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    employee = db.get_employee(uid)
    if not employee:
        return

    la_now   = db.get_la_now()
    date_str = la_now.strftime("%B %-d")

    all_today = db.get_sessions_today()
    my_today  = [s for s in all_today if s["telegram_id"] == uid]
    open_sess = [s for s in my_today if not s.get("end_time")]
    done_sess = [s for s in my_today if s.get("end_time") and (s.get("duration_minutes") or 0) > 0]

    header = f"👤 *{escape_md(employee['name'])}* — Today {date_str}\n"

    if not my_today:
        await update.message.reply_text(
            header + "\nNo work logged yet today. Scan a QR to start! 📱",
            parse_mode="Markdown", reply_markup=TECH_KB)
        return

    lines     = [header]
    total_min = 0

    if open_sess:
        lines.append("🟢 *NOW WORKING:*")
        for s in open_sess:
            elapsed = db.elapsed_minutes(s["start_time"])
            total_min += elapsed
            car = _car_display(s) if s.get("car") else s.get("job_id", "")
            lines.append(f"🚗 {escape_md(car)}  ·  since {db.fmt_time(s['start_time'])}  ·  {db.fmt_dur(elapsed)}")
        lines.append("")

    if done_sess:
        lines.append("✅ *DONE TODAY:*")
        for s in done_sess:
            total_min += s["duration_minutes"] or 0
            car = _car_display(s) if s.get("car") else s.get("job_id", "")
            lines.append(
                f"🚗 {escape_md(car)}"
                f"  ·  {db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
                f"  ·  {db.fmt_dur(s['duration_minutes'])}"
            )
        lines.append("")

    lines.append(f"⏱ *Total: {db.fmt_dur(total_min)}*")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=TECH_KB)


async def cmd_tech_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    employee = db.get_employee(uid)
    if not employee:
        return

    sessions = db.get_employee_sessions_last_days(uid, days=7)
    header   = f"👤 *{escape_md(employee['name'])}* — Last 7 days\n"

    if not sessions:
        await update.message.reply_text(
            header + "\nNo sessions in the last 7 days.",
            parse_mode="Markdown", reply_markup=TECH_KB)
        return

    by_date = defaultdict(list)
    for s in sessions:
        d = db.la_date(s["start_time"])
        if d:
            by_date[d].append(s)

    la_now    = db.get_la_now()
    lines     = [header]
    total_all = 0

    for i in range(6, -1, -1):
        day      = (la_now - timedelta(days=i)).date()
        day_sess = by_date.get(day, [])
        day_min  = sum(
            (db.elapsed_minutes(s["start_time"]) if not s.get("end_time")
             else s["duration_minutes"] or 0)
            for s in day_sess
            if not s.get("end_time") or (s.get("duration_minutes") or 0) > 0
        )
        total_all += day_min
        date_str   = day.strftime("%b %-d")
        if day_sess:
            job_cnt = len({s["job_id"] for s in day_sess})
            lines.append(
                f"📅 {date_str}: *{db.fmt_dur(day_min)}*"
                f"  ({job_cnt} job{'s' if job_cnt != 1 else ''})"
            )
        else:
            lines.append(f"📅 {date_str}: —")

    lines.append(f"\n⏱ *Total: {db.fmt_dur(total_all)}*")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=TECH_KB)


async def handle_tech_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        return
    if is_admin(update.effective_user.id):
        return
    text = update.message.text
    if text == "⏱ My Today":
        await cmd_tech_today(update, ctx)
    elif text == "📋 My History":
        await cmd_tech_history(update, ctx)


async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    employee = db.get_employee(uid)
    if not employee:
        await update.message.reply_text("You're not registered yet. Scan a car QR to get started.")
        return

    sessions  = db.get_employee_week_hours(uid)
    total_min = sum(s["duration_minutes"] or 0 for s in sessions)
    open_sess = db.get_employee_open_session_any(uid)

    text = f"📊 *Your hours this week, {escape_md(employee['name'])}:*\n\n"
    if sessions:
        for s in sessions:
            car = _car_display(s) if s.get("car") else s.get("job_id", "")
            text += (
                f"🚗 {escape_md(car)}\n"
                f"  {db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
                f"  *({db.fmt_dur(s['duration_minutes'])})*\n\n"
            )
    else:
        text += "No completed sessions this week.\n\n"

    if open_sess:
        car = _car_display(open_sess) if open_sess.get("car") else open_sess.get("job_id", "")
        text += (
            f"🟢 *Currently clocked in:*\n"
            f"🚗 {escape_md(car)}  ·  ⏱ {db.live_dur(open_sess['start_time'])}\n\n"
        )

    text += f"⏱ *Total completed: {db.fmt_dur(total_min)}*"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=TECH_KB)


async def cmd_clockout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Technician command to clock out of one or all open sessions."""
    uid = update.effective_user.id
    if is_admin(uid):
        return
    employee = db.get_employee(uid)
    if not employee:
        await update.message.reply_text("You're not registered yet.")
        return

    open_sessions = db.get_employee_all_open_sessions(uid)
    if not open_sessions:
        await update.message.reply_text("✅ You're not clocked in anywhere.", reply_markup=TECH_KB)
        return

    if len(open_sessions) == 1:
        s      = open_sessions[0]
        job    = db.get_job(s["job_id"])
        car    = _car_display(job) if job else _car_display(s)
        result = db.close_session(s["id"], s["start_time"])
        if result is None:
            await update.message.reply_text("⚡ Session too short to record.", reply_markup=TECH_KB)
        else:
            _, minutes = result
            await update.message.reply_text(
                f"✅ *DONE!*\n\n🚗 {escape_md(car)}\n⏱ {db.fmt_dur(minutes)}",
                parse_mode="Markdown", reply_markup=TECH_KB)
        return

    # Multiple open sessions
    keyboard = []
    for s in open_sessions:
        job     = db.get_job(s["job_id"])
        car     = _car_display(job) if job else _car_display(s)
        elapsed = db.elapsed_minutes(s["start_time"])
        keyboard.append([InlineKeyboardButton(
            f"⏹ {car} — {db.fmt_dur(elapsed)}",
            callback_data=f"co_{s['id']}",
        )])
    await update.message.reply_text(
        "You're clocked in on multiple cars. Which do you want to clock out of?",
        reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_clockout_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        session_id = int(query.data[len("co_"):])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return
    sess = db.get_session(session_id)
    if not sess:
        await query.edit_message_text("Session not found.")
        return
    job    = db.get_job(sess["job_id"])
    car    = _car_display(job) if job else sess["job_id"]
    result = db.close_session(session_id, sess["start_time"])
    if result is None:
        await query.edit_message_text("⚡ Session too short to record.")
    else:
        _, minutes = result
        await query.edit_message_text(
            f"✅ Clocked out of {escape_md(car)} — {db.fmt_dur(minutes)}")


async def cmd_myname(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Technician updates their own name."""
    uid      = update.effective_user.id
    employee = db.get_employee(uid)
    if not employee:
        await update.message.reply_text("You're not registered yet.")
        return ConversationHandler.END
    await update.message.reply_text(
        f"Current name: *{escape_md(employee['name'])}*\n\n"
        "Type your new name (or /cancel):",
        parse_mode="Markdown", reply_markup=TECH_KB,
    )
    return MY_NAME_STATE


async def receive_my_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) > 50:
        await update.message.reply_text("❌ Name must be 1–50 characters.", reply_markup=TECH_KB)
        return MY_NAME_STATE
    if name in db.BUTTON_NAME_BLACKLIST:
        await update.message.reply_text("❌ Please type your real name.", reply_markup=TECH_KB)
        return MY_NAME_STATE
    uid = update.effective_user.id
    db.rename_employee(uid, name)
    await update.message.reply_text(
        f"✅ Name updated to: *{escape_md(name)}*",
        parse_mode="Markdown", reply_markup=TECH_KB)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Staff management
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not is_private(update):
        return
    employees = db.get_all_employees()
    if not employees:
        await update.message.reply_text(
            f"No technicians yet.\nShare the bot: t.me/{BOT_USERNAME}\n"
            "They register on first scan.", reply_markup=ADMIN_KB,
        )
        return
    text = f"👥 *Registered Technicians ({len(employees)}):*\n"
    for emp in employees:
        text += f"\n· {escape_md(emp['name'])}"

    # Send text with ADMIN_KB, then inline management buttons separately
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KB)

    keyboard = []
    for emp in employees:
        keyboard.append([
            InlineKeyboardButton(f"✏️ {emp['name']}", callback_data=f"rename_{emp['telegram_id']}"),
            InlineKeyboardButton("❌ Remove",           callback_data=f"rem_{emp['telegram_id']}"),
        ])
    keyboard.append([InlineKeyboardButton("✖ Cancel", callback_data="rem_cancel")])
    await update.message.reply_text(
        "Manage staff:", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_removestaff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not is_private(update):
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
    try:
        emp_id = int(query.data.replace("rem_", ""))
    except ValueError:
        await query.edit_message_text("Invalid request.")
        return
    emp = db.get_employee(emp_id)
    if not emp:
        await query.edit_message_text("Not found.")
        return
    open_s = db.get_employee_open_session_any(emp_id)
    if open_s:
        db.auto_close_sessions_for_job(open_s["job_id"])
    db.deactivate_employee(emp_id)
    extra = "\n⚠️ Their open session was auto-closed." if open_s else ""
    await query.edit_message_text(
        f"✅ *{escape_md(emp['name'])}* removed.{extra}", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# Rename technician conversation (P2-FEAT-3)
# ═══════════════════════════════════════════════════════════════════════════════

async def start_rename_tech(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        emp_id = int(query.data[len("rename_"):])
    except (ValueError, IndexError):
        await query.edit_message_text("Invalid request.")
        return ConversationHandler.END

    emp = db.get_employee(emp_id)
    if not emp:
        await query.edit_message_text("Technician not found.")
        return ConversationHandler.END

    ctx.user_data["renaming_emp_id"] = emp_id
    cancel_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="rename_cancel")
    ]])
    await query.message.reply_text(
        f"✏️ *Renaming: {escape_md(emp['name'])}*\n\nType the new name:",
        parse_mode="Markdown", reply_markup=cancel_markup,
    )
    return RENAME_TECH


async def handle_rename_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text("❌ Rename cancelled.")
    ctx.user_data.pop("renaming_emp_id", None)
    return ConversationHandler.END


async def receive_renamed_tech(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name   = update.message.text.strip()
    emp_id = ctx.user_data.pop("renaming_emp_id", None)

    if not emp_id:
        return ConversationHandler.END
    if not name or len(name) > 50:
        await update.message.reply_text("❌ Name must be 1–50 characters.", reply_markup=ADMIN_KB)
        ctx.user_data["renaming_emp_id"] = emp_id
        return RENAME_TECH
    if name in db.BUTTON_NAME_BLACKLIST:
        await update.message.reply_text("❌ That's a button label, not a name.", reply_markup=ADMIN_KB)
        ctx.user_data["renaming_emp_id"] = emp_id
        return RENAME_TECH

    emp = db.get_employee(emp_id)
    old_name = emp["name"] if emp else "?"
    db.rename_employee(emp_id, name)
    await update.message.reply_text(
        f"✅ *{escape_md(old_name)}* renamed to *{escape_md(name)}*.",
        parse_mode="Markdown", reply_markup=ADMIN_KB)
    return ConversationHandler.END


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
        car = _car_display(s) if s.get("car") else s.get("job_id", "")
        try:
            await ctx.bot.send_message(
                tid,
                f"⏰ *Reminder — {BRAND}*\n\n"
                f"You're still clocked in on:\n"
                f"🚗 *{escape_md(car)}*\n"
                f"🕐 Since {db.fmt_time(s['start_time'])}  ·  {db.live_dur(s['start_time'])}\n\n"
                f"Don't forget to scan the QR when you're done!",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    names = ", ".join(sorted(set(s["emp_name"] for s in sessions)))
    await notify_admins(
        ctx.bot,
        f"⚠️ *{len(sessions)} open session(s) at end of day*\n👤 {escape_md(names)}\n\n"
        f"Use 🚗 Shop Status to fix.",
    )


async def auto_close_job(ctx):
    closed = db.auto_close_all_open_sessions()
    if not closed:
        return
    for s in closed:
        car = _car_display(s) if s.get("car") else s.get("job_id", "")
        try:
            await ctx.bot.send_message(
                s["telegram_id"],
                f"🔒 *Auto clock-out — {BRAND}*\n\n"
                f"Your shift on *{escape_md(car)}* was automatically closed.\n"
                f"⏱ Recorded: *{db.fmt_dur(s['minutes'])}*\n\n"
                f"_If incorrect, let the office manager know._",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    summary = "\n".join(
        f"· {escape_md(s['emp_name'])} — {escape_md(_car_display(s) if s.get('car') else s.get('job_id',''))} — {db.fmt_dur(s['minutes'])}"
        for s in closed)
    await notify_admins(
        ctx.bot,
        f"🔒 *Auto clock-out — {len(closed)} session(s)*\n\n{summary}\n\n"
        f"Use 🚗 Shop Status to correct times.",
    )


async def daily_report_job(ctx):
    la_now   = db.get_la_now()
    date_str = la_now.strftime("%-m/%-d/%Y")
    text     = f"📊 *Daily Report — {date_str}*\n\n{_build_report(1)}"
    await notify_admins(ctx.bot, text)


async def weekly_report_job(ctx):
    la_now = db.get_la_now()
    if la_now.weekday() != 0:
        return
    week_start = la_now.strftime("%-m/%-d")
    text = f"📆 *Weekly Report — week of {week_start}*\n\n{_build_report(7)}"
    await notify_admins(ctx.bot, text)


# ═══════════════════════════════════════════════════════════════════════════════
# Catch-alls
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_non_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        return
    await update.message.reply_text(
        "👋 I only understand text and QR codes.\n\nScan a QR sticker on a car to clock in or out. 📱"
    )


async def handle_unknown_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Catch-all for text messages that no other handler matched.
    Handles the case where the bot restarted mid-conversation (TECH-4)."""
    if not is_private(update):
        return
    uid = update.effective_user.id
    if is_admin(uid):
        await update.message.reply_text(
            "Something went wrong. Please try again.",
            reply_markup=ADMIN_KB)
    else:
        emp = db.get_employee(uid)
        if emp and emp.get("status") != "inactive":
            await update.message.reply_text(
                "Something went wrong. Please try again.\nScan a QR to clock in or out. 📱",
                reply_markup=TECH_KB)


async def _cancel_tech(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.", reply_markup=TECH_KB)
    return ConversationHandler.END


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

    _no_cmd = filters.TEXT & ~filters.COMMAND

    # ── Conversation: first-time registration via /start ──────────────────────
    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            WAITING_NAME: [MessageHandler(_no_cmd, receive_name)],
            CONFIRM_NAME: [CallbackQueryHandler(confirm_name_callback, pattern="^confirm_name_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: add a new job (5 steps) ─────────────────────────────────
    job_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addjob", cmd_addjob),
            MessageHandler(filters.Text(["📋 New Job"]), cmd_addjob),
        ],
        states={
            ADD_JOB_ID:     [MessageHandler(_no_cmd, addjob_id),     CommandHandler("skip", addjob_id)],
            ADD_JOB_YEAR:   [MessageHandler(_no_cmd, addjob_year),   CommandHandler("skip", addjob_year)],
            ADD_JOB_CAR:    [MessageHandler(_no_cmd, addjob_car),    CommandHandler("skip", addjob_car)],
            ADD_JOB_PLATE:  [MessageHandler(_no_cmd, addjob_plate),  CommandHandler("skip", addjob_plate)],
            ADD_JOB_CLIENT: [MessageHandler(_no_cmd, addjob_client), CommandHandler("skip", addjob_client)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.COMMAND, conv_cmd_escape),
        ],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: admin edits a session time ──────────────────────────────
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_admin_session, pattern="^adm_edit_")],
        states={
            EDITING_SESSION_TIME: [
                MessageHandler(_no_cmd, receive_edited_time),
                CallbackQueryHandler(cancel_edit_callback, pattern="^adm_cancel_edit$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: edit a job field ───────────────────────────────────────
    edit_job_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_edit_job_field, pattern="^aj_editfield_")],
        states={
            EDIT_JOB_FIELD: [
                MessageHandler(_no_cmd, receive_edited_job_field),
                CommandHandler("skip", receive_edited_job_field),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: rename technician ──────────────────────────────────────
    rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_rename_tech, pattern="^rename_\\d+$")],
        states={
            RENAME_TECH: [
                MessageHandler(_no_cmd, receive_renamed_tech),
                CallbackQueryHandler(handle_rename_cancel, pattern="^rename_cancel$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: technician updates own name ─────────────────────────────
    myname_conv = ConversationHandler(
        entry_points=[CommandHandler("myname", cmd_myname)],
        states={
            MY_NAME_STATE: [MessageHandler(_no_cmd, receive_my_name)]
        },
        fallbacks=[CommandHandler("cancel", _cancel_tech)],
        per_user=True, allow_reentry=True,
    )

    app_button_filter  = filters.Text(list(BUTTON_COMMANDS.keys()))
    tech_button_filter = filters.Text(list(TECH_BUTTONS))

    # Conversations first (highest priority)
    app.add_handler(start_conv)
    app.add_handler(job_conv)
    app.add_handler(edit_conv)
    app.add_handler(edit_job_conv)
    app.add_handler(rename_conv)
    app.add_handler(myname_conv)

    # Message handlers
    app.add_handler(MessageHandler(app_button_filter,  handle_buttons))
    app.add_handler(MessageHandler(tech_button_filter, handle_tech_buttons))

    # Command handlers
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("status",      cmd_shop_status))
    app.add_handler(CommandHandler("closejob",    cmd_closejob))
    app.add_handler(CommandHandler("qrlink",      cmd_qrlink))
    app.add_handler(CommandHandler("mystats",     cmd_mystats))
    app.add_handler(CommandHandler("staff",       cmd_staff))
    app.add_handler(CommandHandler("removestaff", cmd_removestaff))
    app.add_handler(CommandHandler("report",      cmd_report))
    app.add_handler(CommandHandler("alljobs",     cmd_alljobs))
    app.add_handler(CommandHandler("clockout",    cmd_clockout))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(handle_undo_checkin,  pattern="^undo_"))
    app.add_handler(CallbackQueryHandler(handle_switch,        pattern="^sw_"))
    app.add_handler(CallbackQueryHandler(handle_admin_session, pattern="^adm_close_"))
    app.add_handler(CallbackQueryHandler(handle_fix_session,   pattern="^fix_session_"))
    app.add_handler(CallbackQueryHandler(handle_fix_cancel,    pattern="^fix_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_remove_staff,  pattern="^rem_"))
    app.add_handler(CallbackQueryHandler(handle_alljobs,       pattern="^aj_"))
    app.add_handler(CallbackQueryHandler(handle_clockout_btn,  pattern="^co_"))

    # Catch non-text (photos, stickers, voice, etc.)
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_non_text))
    # Catch-all for unhandled text (bot restart mid-conversation — TECH-4)
    app.add_handler(MessageHandler(filters.TEXT, handle_unknown_text))

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
