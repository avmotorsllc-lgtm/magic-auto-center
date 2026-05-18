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
REMINDER_HOUR   = int(os.environ.get("REMINDER_HOUR",   "22"))   # UTC
AUTO_CLOSE_HOUR = int(os.environ.get("AUTO_CLOSE_HOUR", "23"))   # UTC
REPORT_HOUR     = int(os.environ.get("REPORT_HOUR",     "15"))   # UTC (≈ 8 AM PT)

BRAND = "🔧 Magic Auto Center"

(ADD_JOB_ID, ADD_JOB_CAR, ADD_JOB_PLATE,
 ADD_JOB_CLIENT, ADD_JOB_WORKS, ADD_JOB_COLOR, ADD_JOB_DUE,
 WAITING_NAME, EDITING_SESSION_TIME) = range(9)

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

# ── Technician keyboard (non-admin) ───────────────────────────────────────────
TECH_KB = ReplyKeyboardMarkup([
    [KeyboardButton("⏱ My Today"), KeyboardButton("📋 My History")],
], resize_keyboard=True, input_field_placeholder="Choose...")

TECH_BUTTONS = {"⏱ My Today", "📋 My History"}

JOBS_PER_PAGE = 10


def is_admin(uid): return uid in ADMIN_IDS


def is_private(update: Update) -> bool:
    """True if the message/callback is from a private chat."""
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


_ESC_CHARS = re.compile(r'([_*`\[])')

def escape_md(text) -> str:
    """Escape Markdown v1 special characters in user-supplied text."""
    return _ESC_CHARS.sub(r'\\\1', str(text or ""))


async def send_safe(send_fn, text: str, **kwargs):
    """Send text, splitting into ≤3500-char chunks on newline boundaries."""
    MAX = 3500
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
        if not employee:
            ctx.user_data["pending_job"] = job_id
            await update.message.reply_text(
                f"{BRAND}\n\n👋 Welcome! You're not registered yet.\n\nWhat's your name?"
            )
            return WAITING_NAME
        job = db.get_job(job_id)
        if not job:
            await update.message.reply_text(
                f"❌ Job *{escape_md(job_id)}* not found.\n\n"
                "This QR sticker may be outdated. Ask the office manager for the current job.",
                parse_mode="Markdown")
            return ConversationHandler.END
        if job.get("status") == "closed":
            await update.message.reply_text(
                f"❌ Job *{escape_md(job_id)}* is closed.\n\n"
                "Ask the office manager if this car still needs work.",
                parse_mode="Markdown")
            return ConversationHandler.END
        if job.get("completed_at"):
            await update.message.reply_text(
                f"❌ Job *{escape_md(job_id)}* is already marked as done.\n\n"
                "Ask the office manager for details.",
                parse_mode="Markdown")
            return ConversationHandler.END
        await _process_scan(update, ctx, employee, job)
        return ConversationHandler.END

    employee = db.get_employee(uid)
    if employee:
        if is_admin(uid):
            await _show_admin_menu(update)
        else:
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
    uid  = update.effective_user.id
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
            "📱 Ask your manager for the QR sticker for your car,\n"
            "or scan any QR code already attached to a windshield.",
            parse_mode="Markdown", reply_markup=TECH_KB,
        )
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
            f"✅ *DONE FOR THIS CAR!*\n\n"
            f"⏱ You worked *{db.fmt_dur(minutes)}* on {escape_md(job['car'])}\n"
            f"🕐 {db.fmt_time(open_s['start_time'])} → {db.fmt_time(end_time)}\n\n"
            f"Great work, {escape_md(employee['name'])}! 🔧"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        await notify_admins(
            ctx.bot,
            f"⏹ *Clock Out*\n👤 {escape_md(employee['name'])}\n"
            f"🚗 {escape_md(job['car'])}  ·  {escape_md(job['plate'] or '—')}\n"
            f"📋 {escape_md(job['id'])}  ·  ⏱ {db.fmt_dur(minutes)}",
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
            f"⚠️ *Hold on, {escape_md(employee['name'])}!*\n\n"
            f"You're still clocked in on:\n"
            f"🚗 *{escape_md(other['car'])}*  ·  {other.get('plate') or other.get('job_id', '')}\n"
            f"🕐 Since {since}  ({elapsed} ago)\n\n"
            f"What do you want to do?",
            parse_mode="Markdown", reply_markup=keyboard,
        )
        return

    # ── CLOCK IN ─────────────────────────────────────────────────────────────
    start_time = db.open_session(job["id"], emp_id)
    msg = (
        f"✅ *YOU'RE ON THE CLOCK!*\n\n"
        f"🚗 {escape_md(job['car'])}  ·  {escape_md(job['id'])}\n"
        f"🕐 Started: {db.fmt_time(start_time)}\n"
        f"🔧 {escape_md(job['works'] or '—')}\n\n"
        f"Scan this QR again when you're done. Good luck! 💪"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
    await notify_admins(
        ctx.bot,
        f"▶️ *Clock In*\n👤 {escape_md(employee['name'])}\n"
        f"🚗 {escape_md(job['car'])}  ·  {escape_md(job['plate'] or '—')}\n"
        f"📋 {escape_md(job['id'])}  ·  🕐 {db.fmt_time(start_time)}",
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
            f"⏹ Clocked out of *{escape_md(old_s['job_id'])}*  ({db.fmt_dur(old_min)})\n\n"
            f"▶️ *CLOCKED IN*\n"
            f"🚗 {escape_md(new_j['car'])}  ·  {escape_md(new_j['plate'] or '—')}\n"
            f"📋 {escape_md(new_job_id)}  ·  🕐 {db.fmt_time(start_time)}",
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
            f"👤 {escape_md(emp['name'])}\n"
            f"🚗 {escape_md(new_j['car'])}  ·  {escape_md(new_j['plate'] or '—')}\n"
            f"📋 {escape_md(new_job_id)}  ·  🕐 {db.fmt_time(start_time)}\n\n"
            f"_Scan each QR separately to clock out of each car._",
            parse_mode="Markdown",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Admin keyboard buttons
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Routes keyboard button taps. '📋 New Job' is handled by job_conv instead."""
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
# Add Job conversation
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_addjob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ctx.user_data.pop("new_job", None)
    await update.message.reply_text(
        "*New Repair Order*\n\nStep 1/7 — RO number:\n_(e.g. RO-1043)_\n\n"
        "_Type /cancel to stop._",
        parse_mode="Markdown",
    )
    return ADD_JOB_ID


async def addjob_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    # BUG 1: /cancel check before treating as RO number
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
        return ConversationHandler.END
    # BUG 16: sanitize RO number
    job_id = re.sub(r'[^A-Z0-9\-]', '', text.upper().replace(' ', '-'))
    if not job_id:
        await update.message.reply_text(
            "❌ Invalid RO. Use letters, numbers, and dashes (e.g. RO-1043).")
        return ADD_JOB_ID
    if len(job_id) > 30:
        await update.message.reply_text("❌ Too long. Max 30 characters.")
        return ADD_JOB_ID
    if db.get_job(job_id):
        await update.message.reply_text(
            f"⚠️ *{escape_md(job_id)}* already exists. Enter a different RO number:",
            parse_mode="Markdown")
        return ADD_JOB_ID
    ctx.user_data["new_job"] = {"id": job_id}
    await update.message.reply_text(
        f"✅ {escape_md(job_id)}\n\nStep 2/7 — Make & model:\n_(e.g. BMW X5)_",
        parse_mode="Markdown")
    return ADD_JOB_CAR


async def addjob_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if not text or text.lower() == "/skip":
        await update.message.reply_text("❌ Car make & model cannot be empty. Please type it.")
        return ADD_JOB_CAR
    if len(text) > 100:
        await update.message.reply_text("❌ Too long. Max 100 characters.")
        return ADD_JOB_CAR
    ctx.user_data["new_job"]["car"] = text
    await update.message.reply_text(
        "Step 3/7 — License plate:\n_(or /skip)_", parse_mode="Markdown")
    return ADD_JOB_PLATE


async def addjob_plate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if len(text) > 20:
        await update.message.reply_text("❌ Too long. Max 20 characters.")
        return ADD_JOB_PLATE
    ctx.user_data["new_job"]["plate"] = "" if text.lower() == "/skip" else text.upper()
    await update.message.reply_text(
        "Step 4/7 — Customer name:\n_(or /skip)_", parse_mode="Markdown")
    return ADD_JOB_CLIENT


async def addjob_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if len(text) > 100:
        await update.message.reply_text("❌ Too long. Max 100 characters.")
        return ADD_JOB_CLIENT
    ctx.user_data["new_job"]["client"] = "" if text.lower() == "/skip" else text
    await update.message.reply_text(
        "Step 5/7 — Work description:\n_(e.g. Front bumper repaint, hood dent)_\n_(or /skip)_",
        parse_mode="Markdown")
    return ADD_JOB_WORKS


async def addjob_works(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if len(text) > 300:
        await update.message.reply_text("❌ Too long. Max 300 characters.")
        return ADD_JOB_WORKS
    ctx.user_data["new_job"]["works"] = "" if text.lower() == "/skip" else text
    await update.message.reply_text(
        "Step 6/7 — Car color:\n_(e.g. Red, White)  (or /skip)_", parse_mode="Markdown")
    return ADD_JOB_COLOR


async def addjob_color(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if len(text) > 30:
        await update.message.reply_text("❌ Too long. Max 30 characters.")
        return ADD_JOB_COLOR
    ctx.user_data["new_job"]["color"] = "" if text.lower() == "/skip" else text
    await update.message.reply_text(
        "Step 7/7 — Estimated completion date:\n_(e.g. May 20)  (or /skip)_",
        parse_mode="Markdown")
    return ADD_JOB_DUE


async def addjob_due(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in BUTTON_LABELS:
        await _dispatch_button(update, ctx, text)
        return ConversationHandler.END
    if len(text) > 50:
        await update.message.reply_text("❌ Too long. Max 50 characters.")
        return ADD_JOB_DUE
    j = ctx.user_data.pop("new_job", {})
    j["due_date"] = "" if text.lower() == "/skip" else text
    db.add_job(j["id"], j["car"], j.get("plate",""), j.get("client",""),
               j.get("works",""), j.get("color",""), j.get("due_date",""))
    qr_link = f"https://t.me/{BOT_USERNAME}?start={j['id']}"
    lines = [f"✅ *Job created!*\n",
             f"📋 *{escape_md(j['id'])}*",
             f"🚗 {escape_md(j['car'])}  ·  {escape_md(j.get('plate') or '—')}"]
    if j.get("color"):    lines.append(f"🎨 {escape_md(j['color'])}")
    if j.get("client"):   lines.append(f"👤 {escape_md(j['client'])}")
    if j.get("works"):    lines.append(f"🔧 {escape_md(j['works'])}")
    if j.get("due_date"): lines.append(f"📅 Due: {escape_md(j['due_date'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=ADMIN_KB)
    await _send_qr(update.message, j["id"], j["car"], j.get("plate",""), qr_link)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("new_job", None)
    ctx.user_data.pop("editing_session_id", None)
    ctx.user_data.pop("editing_date", None)
    await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# Shop Status
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
    today_closed   = [s for s in today_sessions if s.get("end_time")]

    open_job_ids      = {s["job_id"] for s in all_open}
    completed_job_ids = {s["job_id"] for s in today_closed if s["job_id"] not in open_job_ids}

    in_progress     = [j for j in jobs if j["id"] in open_job_ids]
    completed_today = [j for j in jobs if j["id"] in completed_job_ids]
    idle            = [j for j in jobs if j["id"] not in open_job_ids and j["id"] not in completed_job_ids]

    lines      = [f"🚗 *SHOP STATUS — {date_str}*\n"]
    keyboard   = []
    suspicious = []

    # ── Section 1: IN PROGRESS ────────────────────────────────────────────────
    if in_progress:
        lines.append("━━━ 🟢 IN PROGRESS ━━━\n")
        for j in in_progress:
            hdr = f"*{escape_md(j['id'])}*  ·  {escape_md(j['car'])}"
            if j["plate"]:
                hdr += f"  ·  {escape_md(j['plate'])}"
            lines.append(hdr)
            for s in (s for s in all_open if s["job_id"] == j["id"]):
                elapsed = db.elapsed_minutes(s["start_time"])
                lines.append(
                    f"  › {escape_md(s['emp_name'])}: {db.fmt_dur(elapsed)}"
                    f" (since {db.fmt_time(s['start_time'])})"
                )
                if elapsed >= 8 * 60:
                    suspicious.append((s, elapsed))
                keyboard.append([
                    InlineKeyboardButton(
                        f"✏️ Check-in: {s['emp_name']}",
                        callback_data=f"adm_edit_start_{s['id']}",
                    ),
                    InlineKeyboardButton(
                        "✏️ Check-out",
                        callback_data=f"adm_edit_{s['id']}",
                    ),
                ])
            lines.append("")

    # ── Section 2: COMPLETED TODAY ────────────────────────────────────────────
    if completed_today:
        lines.append("━━━ ✅ COMPLETED TODAY ━━━\n")
        for j in completed_today:
            hdr = f"*{escape_md(j['id'])}*  ·  {escape_md(j['car'])}"
            if j["plate"]:
                hdr += f"  ·  {escape_md(j['plate'])}"
            lines.append(hdr)
            for s in (s for s in today_closed if s["job_id"] == j["id"]):
                lines.append(
                    f"  {escape_md(s['emp_name'])}: "
                    f"{db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
                    f"  ({db.fmt_dur(s['duration_minutes'])})"
                )
                keyboard.append([
                    InlineKeyboardButton(
                        f"✏️ Start — {s['emp_name']}",
                        callback_data=f"adm_edit_start_{s['id']}",
                    ),
                    InlineKeyboardButton(
                        f"✏️ End — {s['emp_name']}",
                        callback_data=f"adm_edit_{s['id']}",
                    ),
                ])
            lines.append("")

    # ── Section 3: IDLE ───────────────────────────────────────────────────────
    if idle:
        lines.append("━━━ ⬜ IDLE (no work today) ━━━\n")
        for j in idle:
            total = db.get_job_total_minutes(j["id"])
            line  = f"  {escape_md(j['id'])}  ·  {escape_md(j['car'])}"
            if j.get("client"):
                line += f"  ·  {escape_md(j['client'])}"
            if total:
                line += f"  ·  ⏱ {db.fmt_dur(total)} all-time"
            lines.append(line)
        lines.append("")

    # ── Suspicious sessions ───────────────────────────────────────────────────
    if suspicious:
        lines.append(f"━━━ ⚠️ {len(suspicious)} LONG SESSION(S) ━━━")
        for s, elapsed in suspicious:
            lines.append(
                f"  {escape_md(s['emp_name'])} on {escape_md(s['car'])} — "
                f"{db.fmt_dur(elapsed)} _(forgot to clock out?)_"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"⛔ Close: {s['emp_name']}",
                    callback_data=f"adm_close_{s['id']}",
                ),
            ])

    text         = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ADMIN_KB
    await send_safe(update.message.reply_text, text, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_admin_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    parts  = query.data.split("_")
    action = parts[1]   # "close" or "edit"

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
        _, minutes = db.close_session(session_id, sess["start_time"])
        await query.edit_message_text(
            f"✅ Closed. Duration: *{db.fmt_dur(minutes)}*\n_(Use ✏️ buttons to fix times)_",
            parse_mode="Markdown",
        )
    elif action == "edit":
        emp      = db.get_employee(sess["employee_id"])
        job      = db.get_job(sess["job_id"])
        emp_name = emp["name"] if emp else "?"
        car_name = job["car"] if job else sess["job_id"]

        sess_date = db.la_date(sess["start_time"])
        date_str  = sess_date.strftime("%b %-d") if sess_date else "today"

        ctx.user_data["editing_session_id"] = session_id
        ctx.user_data["editing_mode"]       = "start" if editing_start else "end"
        ctx.user_data["editing_date"]       = sess_date  # BUG 9 fix

        cancel_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel edit", callback_data="adm_cancel_edit")
        ]])

        if editing_start:
            msg = (
                f"✏️ *Edit check-in time*\n\n"
                f"👤 {escape_md(emp_name)}  →  {escape_md(car_name)}\n"
                f"📅 Session date: *{date_str}*\n"
                f"🕐 Currently: *{db.fmt_time(sess['start_time'])}*\n"
            )
            if sess.get("end_time"):
                msg += f"🕑 Checked out: {db.fmt_time(sess['end_time'])}\n"
            msg += f"\nType the correct *start* time for {date_str} (LA time):\n_(e.g.  9:30 AM  or  09:30)_"
        else:
            msg = (
                f"✏️ *Edit check-out time*\n\n"
                f"👤 {escape_md(emp_name)}  →  {escape_md(car_name)}\n"
                f"📅 Session date: *{date_str}*\n"
                f"🕐 Clocked in at: {db.fmt_time(sess['start_time'])}\n\n"
                f"Type the correct *end* time for {date_str} (LA time):\n_(e.g.  5:30 PM  or  17:30)_"
            )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=cancel_markup)
        return EDITING_SESSION_TIME


async def cancel_edit_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """[❌ Cancel edit] inline button inside the edit-time conversation."""
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
    new_str = db.parse_time_input(text, date_la=editing_date)  # BUG 9: use session date
    if not new_str or not sess:
        await update.message.reply_text(
            "❌ Couldn't parse time. Try again:\n_(e.g.  5:30 PM  or  17:30)_",
            parse_mode="Markdown")
        ctx.user_data["editing_session_id"] = session_id
        ctx.user_data["editing_mode"]       = mode
        ctx.user_data["editing_date"]       = editing_date
        return EDITING_SESSION_TIME

    # BUG 10: reject future times
    new_dt_utc = datetime.strptime(new_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    la_now     = db.get_la_now()
    if new_dt_utc > la_now.astimezone(timezone.utc):
        current_time = la_now.strftime("%-I:%M %p")
        await update.message.reply_text(
            f"❌ That time is in the future. Current LA time is *{current_time}*.",
            parse_mode="Markdown")
        ctx.user_data["editing_session_id"] = session_id
        ctx.user_data["editing_mode"]       = mode
        ctx.user_data["editing_date"]       = editing_date
        return EDITING_SESSION_TIME

    # BUG 8: reject end time ≤ start time
    if mode == "end":
        start_dt_utc = datetime.strptime(
            str(sess["start_time"])[:19], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        if new_dt_utc <= start_dt_utc:
            await update.message.reply_text(
                f"❌ End time must be *after* start time (started at *{db.fmt_time(sess['start_time'])}*).",
                parse_mode="Markdown")
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
        _, minutes = db.close_session(session_id, sess["start_time"], end_str=new_str)
        await update.message.reply_text(
            f"✅ *Check-out updated!*\n"
            f"🕑 New check-out: *{db.fmt_time(new_str)}*\n"
            f"⏱ Duration: *{db.fmt_dur(minutes)}*",
            parse_mode="Markdown", reply_markup=ADMIN_KB)
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
        await update.message.reply_text("Usage: `/closejob RO-1043`", parse_mode="Markdown")
        return
    job_id = ctx.args[0].upper()
    if not db.get_job(job_id):
        await update.message.reply_text(f"Job *{escape_md(job_id)}* not found.", parse_mode="Markdown")
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
        await update.message.reply_text("Usage: `/qrlink RO-1043`", parse_mode="Markdown")
        return
    job_id = ctx.args[0].upper()
    job    = db.get_job(job_id)
    if not job:
        await update.message.reply_text(f"Job *{escape_md(job_id)}* not found.", parse_mode="Markdown")
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

    text = f"📊 *Your hours this week, {escape_md(employee['name'])}:*\n\n"

    if sessions:
        for s in sessions:
            text += (
                f"🚗 {escape_md(s['car'])}  ·  📋 {escape_md(s['job_id'])}\n"
                f"  {db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
                f"  *({db.fmt_dur(s['duration_minutes'])})*\n\n"
            )
    else:
        text += "No completed sessions this week.\n\n"

    if open_sess:
        text += (
            f"🟢 *Currently clocked in:*\n"
            f"🚗 {escape_md(open_sess['car'])}  ·  ⏱ {db.live_dur(open_sess['start_time'])}\n\n"
        )

    text += f"⏱ *Total completed: {db.fmt_dur(total_min)}*"
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# Staff
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
    text = "👥 *Registered Technicians:*\n\n"
    for emp in employees:
        text += f"· {escape_md(emp['name'])}\n"
    text += "\n/removestaff — remove someone"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KB)


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
    emp_id = int(query.data.replace("rem_", ""))
    emp    = db.get_employee(emp_id)
    if not emp:
        await query.edit_message_text("Not found.")
        return
    # BUG 17: check for active session, auto-close it
    open_s = db.get_employee_open_session_any(emp_id)
    if open_s:
        db.auto_close_sessions_for_job(open_s["job_id"])
    # BUG 18: soft-delete so historical reports still show the name
    db.deactivate_employee(emp_id)
    extra = "\n⚠️ Their open session was auto-closed." if open_s else ""
    await query.edit_message_text(
        f"✅ *{escape_md(emp['name'])}* removed.{extra}", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════════════════════

def _build_report(days):
    by_job = db.get_report_data(days)
    label  = "today" if days == 1 else f"last {days} days"
    text   = f"📊 *{BRAND}*\n*Time Report — {label}*\n\n"

    if by_job:
        for jid, d in by_job.items():
            text += f"🚗 *{escape_md(jid)}*  {escape_md(d['car'])}"
            if d["plate"]:
                text += f"  ·  {escape_md(d['plate'])}"
            text += "\n"
            for r in d["rows"]:
                auto  = " _(auto-closed)_" if r.get("auto_closed") else ""
                text += (
                    f"  · {escape_md(r['emp_name'])}: "
                    f"{db.fmt_time(r['start_time'])} → {db.fmt_time(r['end_time'])}"
                    f"  ({db.fmt_dur(r['duration_minutes'])}){auto}\n"
                )
            text += f"  ⏱ *Total: {db.fmt_dur(d['total'])}*\n\n"
    else:
        text += f"No data for {label}.\n\n"

    # BUG 23: show in-progress sessions
    open_sessions = db.get_all_open_sessions()
    if open_sessions:
        text += "⏳ *Still in progress (not counted):*\n"
        for s in open_sessions:
            text += f"  · {escape_md(s['emp_name'])} on {escape_md(s['car'])} — {db.live_dur(s['start_time'])}\n"

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
# Technician: My Today / My History
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_tech_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    employee = db.get_employee(uid)
    if not employee:
        return

    la_now   = db.get_la_now()
    date_str = la_now.strftime("%B %-d")

    all_today  = db.get_sessions_today()
    my_today   = [s for s in all_today if s["telegram_id"] == uid]
    open_sess  = [s for s in my_today if not s.get("end_time")]
    done_sess  = [s for s in my_today if s.get("end_time")]

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
            lines.append(
                f"🚗 {escape_md(s['car'])} ({escape_md(s['job_id'])})  ·  "
                f"since {db.fmt_time(s['start_time'])}  ·  {db.fmt_dur(elapsed)}"
            )
        lines.append("")

    if done_sess:
        lines.append("✅ *DONE TODAY:*")
        for s in done_sess:
            total_min += s["duration_minutes"] or 0
            lines.append(
                f"🚗 {escape_md(s['car'])} ({escape_md(s['job_id'])})  ·  "
                f"{db.fmt_time(s['start_time'])} → {db.fmt_time(s['end_time'])}"
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


# ═══════════════════════════════════════════════════════════════════════════════
# All Jobs manager
# ═══════════════════════════════════════════════════════════════════════════════

def _job_card(job):
    """Returns (text, markup) for a single self-contained job message."""
    jid       = job["id"]
    status    = "🟢" if job["status"] == "active" else "🔴"
    total_min = db.get_job_total_minutes(jid)
    sess_cnt  = db.get_job_session_count(jid)

    car_line = f"🚗 {escape_md(job['car'])}"
    if job.get("plate"):
        car_line += f"  ·  {escape_md(job['plate'])}"

    lines = [
        "──────────────────",
        f"{status} *{escape_md(jid)}*",
        car_line,
        f"👤 {escape_md(job.get('client') or 'No client')}",
    ]
    if job.get("color"):
        lines.append(f"🎨 {escape_md(job['color'])}")
    if job.get("due_date"):
        lines.append(f"📅 Due: {escape_md(job['due_date'])}")

    stats = []
    if total_min:
        stats.append(f"⏱ {db.fmt_dur(total_min)} total")
    if sess_cnt:
        stats.append(f"{sess_cnt} session{'s' if sess_cnt != 1 else ''}")
    if stats:
        lines.append("  ·  ".join(stats))
    lines.append("──────────────────")

    row1 = [InlineKeyboardButton("🔗 QR Code", callback_data=f"aj_qr_{jid}")]
    if job["status"] == "active" and not job.get("completed_at"):
        row1.append(InlineKeyboardButton("✅ Close Job", callback_data=f"aj_close_{jid}"))
    markup = InlineKeyboardMarkup([
        row1,
        [InlineKeyboardButton("🗑️ Delete Job", callback_data=f"aj_del_{jid}")],
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
        parse_mode="Markdown")
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
        await _send_qr(query.message, job_id, job["car"], job.get("plate", ""), qr_link)
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
        car    = escape_md(job["car"]) if job else escape_md(job_id)
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
        car_str  = escape_md(job["car"]) if job else escape_md(job_id)
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
                f"🚗 *{escape_md(s['car'])}*  ({escape_md(s['job_id'])})\n"
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
        try:
            await ctx.bot.send_message(
                s["telegram_id"],
                f"🔒 *Auto clock-out — {BRAND}*\n\n"
                f"Your shift on *{escape_md(s['car'])}* was automatically closed.\n"
                f"⏱ Recorded: *{db.fmt_dur(s['minutes'])}*\n\n"
                f"_If incorrect, let the office manager know._",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    summary = "\n".join(
        f"· {escape_md(s['emp_name'])} — {escape_md(s['car'])} — {db.fmt_dur(s['minutes'])}"
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
    """Runs daily; sends the 7-day report only on Monday mornings (LA time)."""
    la_now = db.get_la_now()
    if la_now.weekday() != 0:
        return
    week_start = la_now.strftime("%-m/%-d")
    text = f"📆 *Weekly Report — week of {week_start}*\n\n{_build_report(7)}"
    await notify_admins(ctx.bot, text)


# ═══════════════════════════════════════════════════════════════════════════════
# UX: non-text message handler
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_non_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        return
    await update.message.reply_text(
        "👋 I only understand text and QR codes.\n\n"
        "Scan a QR sticker on a car to clock in or out. 📱"
    )


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
            ADD_JOB_COLOR:  [MessageHandler(filters.TEXT, addjob_color)],
            ADD_JOB_DUE:    [MessageHandler(filters.TEXT, addjob_due)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )

    # ── Conversation: admin edits a session time ──────────────────────────────
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_admin_session, pattern="^adm_edit_")],
        states={
            EDITING_SESSION_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edited_time),
                CallbackQueryHandler(cancel_edit_callback, pattern="^adm_cancel_edit$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    app_button_filter  = filters.Text(list(BUTTON_COMMANDS.keys()))
    tech_button_filter = filters.Text(list(TECH_BUTTONS))

    app.add_handler(start_conv)
    app.add_handler(job_conv)
    app.add_handler(edit_conv)
    app.add_handler(MessageHandler(app_button_filter,  handle_buttons))
    app.add_handler(MessageHandler(tech_button_filter, handle_tech_buttons))

    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("status",      cmd_shop_status))
    app.add_handler(CommandHandler("closejob",    cmd_closejob))
    app.add_handler(CommandHandler("qrlink",      cmd_qrlink))
    app.add_handler(CommandHandler("mystats",     cmd_mystats))
    app.add_handler(CommandHandler("staff",       cmd_staff))
    app.add_handler(CommandHandler("removestaff", cmd_removestaff))
    app.add_handler(CommandHandler("report",      cmd_report))
    app.add_handler(CommandHandler("alljobs",     cmd_alljobs))

    app.add_handler(CallbackQueryHandler(handle_switch,        pattern="^sw_"))
    app.add_handler(CallbackQueryHandler(handle_admin_session, pattern="^adm_close_"))
    app.add_handler(CallbackQueryHandler(handle_remove_staff,  pattern="^rem_"))
    app.add_handler(CallbackQueryHandler(handle_alljobs,       pattern="^aj_"))

    # UX: catch non-text messages (photos, stickers, voice, etc.)
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_non_text))

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
