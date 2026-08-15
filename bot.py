#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
COURSE BOT - Complete Telegram Bot
----------------------------------
Features:
- Clean UTF-8 Hindi + English messages
- Only OWNER PANEL + DAILY REPORT on seller home
- Owner panel with working admin/report/channel controls
- Automatic channel detection when bot becomes admin
- Inline course search: @YourBotUsername course-name
- Demo access: one-use invite link + 5-minute auto remove
- Permanent access: invite link + course material/view link
- Join activity logging: member, username, ID, admin, course, type, time
- Daily reports: Last 1 Hour and Last 24 Hours
- SQLite database (bot.db) auto-created
- Duplicate-message protection
- Duplicate report protection
- No mojibake / broken emoji text

Requirements:
    pip install -U python-telegram-bot

Environment:
    BOT_TOKEN=123456:ABC...
    OWNER_ID=123456789

Optional:
    TIMEZONE=Asia/Kolkata

Run:
    python bot.py
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberUpdated,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Kolkata")

DB_FILE = "bot.db"
DEMO_MINUTES = 5

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID environment variable is missing.")

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("course_bot")


# =========================
# DATABASE
# =========================

db_lock = asyncio.Lock()


def db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init_sync():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            added_by INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            invite_link TEXT NOT NULL DEFAULT '',
            view_link TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            course TEXT NOT NULL DEFAULT '',
            channel_id INTEGER NOT NULL DEFAULT 0,
            channel_title TEXT NOT NULL DEFAULT '',
            access_type TEXT NOT NULL DEFAULT 'DEMO',
            added_by INTEGER NOT NULL DEFAULT 0,
            added_by_name TEXT NOT NULL DEFAULT '',
            joined_at TEXT NOT NULL,
            expires_at TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            invite_link TEXT NOT NULL DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 0,
            user_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            admin_id INTEGER NOT NULL DEFAULT 0,
            admin_name TEXT NOT NULL DEFAULT '',
            course TEXT NOT NULL DEFAULT '',
            channel_id INTEGER NOT NULL DEFAULT 0,
            channel_title TEXT NOT NULL DEFAULT '',
            access_type TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_activity_created
        ON activity(created_at)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_members_joined
        ON members(joined_at)
    """)

    conn.commit()
    conn.close()


async def db_init():
    async with db_lock:
        await asyncio.to_thread(db_init_sync)


def now_local():
    return datetime.now(TZ)


def iso_now():
    return now_local().isoformat(timespec="seconds")


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


async def db_execute(sql, params=(), fetch=False, fetchone=False, commit=True):
    async with db_lock:
        def work():
            conn = db_connect()
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall() if fetch else None
            row = cur.fetchone() if fetchone else None
            if commit:
                conn.commit()
            conn.close()
            return row if fetchone else rows
        return await asyncio.to_thread(work)


# =========================
# HELPERS
# =========================

def user_display(user):
    if not user:
        return "Unknown"
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip()
    return name or (f"@{user.username}" if user.username else str(user.id))


def user_label(user_id, name="", username=""):
    if username:
        return f"{name or username} (@{username.lstrip('@')})"
    return name or str(user_id)


def fmt_dt(value):
    dt = parse_dt(value)
    if not dt:
        return "-"
    return dt.astimezone(TZ).strftime("%d/%m/%Y %H:%M:%S")


def html_link(url, text):
    if not url:
        return escape(text)
    return f'<a href="{escape(url, quote=True)}">{escape(text)}</a>'


async def is_owner(user_id):
    return int(user_id) == OWNER_ID


async def is_admin(user_id):
    if int(user_id) == OWNER_ID:
        return True
    row = await db_execute(
        "SELECT user_id FROM admins WHERE user_id=?",
        (int(user_id),),
        fetchone=True,
    )
    return bool(row)


async def actor_name(context, user_id):
    try:
        chat = await context.bot.get_chat(int(user_id))
        return user_display(chat)
    except Exception:
        return str(user_id)


async def log_activity(
    event_type,
    *,
    user_id=0,
    user_name="",
    username="",
    admin_id=0,
    admin_name="",
    course="",
    channel_id=0,
    channel_title="",
    access_type="",
    details="",
):
    await db_execute(
        """
        INSERT INTO activity
        (event_type,user_id,user_name,username,admin_id,admin_name,course,
         channel_id,channel_title,access_type,created_at,details)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_type,
            int(user_id or 0),
            user_name,
            username or "",
            int(admin_id or 0),
            admin_name,
            course,
            int(channel_id or 0),
            channel_title,
            access_type,
            iso_now(),
            details,
        ),
    )


def home_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("âï¸ OWNER PANEL", callback_data="owner_panel"),
            InlineKeyboardButton("ð DAILY REPORT", callback_data="daily_report"),
        ]
    ])


def owner_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ð¥ ADMINS", callback_data="admins"),
            InlineKeyboardButton("â±ï¸ DEMO TIME", callback_data="demo_time"),
        ],
        [
            InlineKeyboardButton("ð DAILY REPORT", callback_data="daily_report"),
            InlineKeyboardButton("ð¡ CHANNELS", callback_data="channels"),
        ],
        [
            InlineKeyboardButton("ð  HOME", callback_data="home"),
        ],
    ])


def report_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ð LAST 1 HOUR", callback_data="report_1h"),
            InlineKeyboardButton("ð LAST 24 HOURS", callback_data="report_24h"),
        ],
        [InlineKeyboardButton("â¬ï¸ OWNER PANEL", callback_data="owner_panel")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("â¬ï¸ BACK", callback_data="owner_panel")]
    ])


# =========================
# CHANNEL AUTO-DETECT
# =========================

async def save_channel_from_chat(context, chat_id):
    try:
        chat = await context.bot.get_chat(chat_id)
    except TelegramError as e:
        logger.warning("Cannot get channel %s: %s", chat_id, e)
        return None

    if chat.type != ChatType.CHANNEL:
        return None

    title = chat.title or str(chat_id)
    username = chat.username or ""

    # Try to create a reusable permanent invite link.
    invite = ""
    try:
        invite_obj = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            name="Course Bot Access",
            creates_join_request=False,
        )
        invite = invite_obj.invite_link or ""
    except TelegramError as e:
        logger.warning("Invite creation failed for %s: %s", chat_id, e)

    # For public channels, /1 is normally the first channel post.
    view_link = ""
    if username:
        view_link = f"https://t.me/{username}/1"

    await db_execute(
        """
        INSERT INTO channels
        (chat_id,title,username,invite_link,view_link,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            invite_link=CASE
                WHEN excluded.invite_link <> '' THEN excluded.invite_link
                ELSE channels.invite_link
            END,
            view_link=CASE
                WHEN excluded.view_link <> '' THEN excluded.view_link
                ELSE channels.view_link
            END,
            updated_at=excluded.updated_at
        """,
        (
            chat_id,
            title,
            username,
            invite,
            view_link,
            iso_now(),
            iso_now(),
        ),
    )

    await log_activity(
        "CHANNEL_DETECTED",
        channel_id=chat_id,
        channel_title=title,
        details="Bot detected/updated channel automatically",
    )

    return {
        "chat_id": chat_id,
        "title": title,
        "username": username,
        "invite_link": invite,
        "view_link": view_link,
    }


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm:
        return

    chat = cm.chat
    if chat.type != ChatType.CHANNEL:
        return

    new_status = cm.new_chat_member.status

    if new_status in ("administrator", "creator"):
        info = await save_channel_from_chat(context, chat.id)
        if info:
            try:
                await context.bot.send_message(
                    OWNER_ID,
                    (
                        "ð¡ <b>Channel Auto-Detected</b>\n\n"
                        f"ð¢ <b>Channel:</b> {escape(info['title'])}\n"
                        f"ð <code>{info['chat_id']}</code>\n\n"
                        "â Bot administrator detected.\n"
                        "Channel is now available in Course Search."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass


# =========================
# HOME / START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    # Ignore duplicate /start commands arriving almost simultaneously.
    key = f"start_lock:{user.id}"
    if context.user_data.get(key):
        return
    context.user_data[key] = True

    try:
        text = (
            "ð¸ <b>HELLO SELLER FAMILY, KESE HO?</b>\n\n"
            "â¨ <b>I AM WIZARD</b> ð¸ ð\n\n"
            "ð <b>COURSE SEARCH</b>\n"
            "Telegram ke kisi bhi chat me likho:\n"
            "<code>@YourBotUsername course-name</code>\n\n"
            "Search result me apna course select karo.\n\n"
            "ð¡ <b>CHANNEL AUTO-DETECT</b>\n"
            "Bot ko channel me administrator banao â "
            "channel automatically detect ho jayega.\n\n"
            "â¡ <i>Fast â¢ Clean â¢ Secure Access System</i>"
        )

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
            disable_web_page_preview=True,
        )
    finally:
        # Keep only a short duplicate guard.
        await asyncio.sleep(1)
        context.user_data.pop(key, None)


# =========================
# OWNER PANEL
# =========================

async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
        send = query.edit_message_text
    else:
        user_id = update.effective_user.id
        send = update.message.reply_text

    if not await is_owner(user_id):
        if query:
            await query.answer("â Owner access only.", show_alert=True)
        else:
            await send("â Owner access only.")
        return

    text = (
        "ð <b>OWNER PANEL</b>\n\n"
        "ð ï¸ <b>Control Center</b>\n"
        "Yahan se bot ka admin, demo aur channel system manage karo.\n\n"
        "ð¡ Bot ko channel me administrator banate hi "
        "channel automatically detect hoga."
    )

    await send(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=owner_keyboard(),
    )


# =========================
# ADMINS
# =========================

async def admins_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_owner(q.from_user.id):
        await q.answer("â Owner only.", show_alert=True)
        return

    rows = await db_execute(
        "SELECT * FROM admins ORDER BY created_at DESC",
        fetch=True,
    )

    lines = ["ð¥ <b>ADMINS</b>\n"]

    if not rows:
        lines.append("No additional admins added.")
    else:
        for i, r in enumerate(rows, 1):
            lines.append(
                f"<b>{i}.</b> {escape(user_label(r['user_id'], r['name'], r['username']))}\n"
                f"ð <code>{r['user_id']}</code>\n"
                f"ð Added: {escape(fmt_dt(r['created_at']))}\n"
            )

    lines.append("\nâ Add with: <code>/addadmin USER_ID</code>")
    lines.append("â Remove with: <code>/deladmin USER_ID</code>")

    await q.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/addadmin USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("â Invalid user ID.")
        return

    try:
        u = await context.bot.get_chat(uid)
        name = user_display(u)
        username = u.username or ""
    except TelegramError:
        name = str(uid)
        username = ""

    await db_execute(
        """
        INSERT INTO admins(user_id,name,username,added_by,created_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,
            username=excluded.username
        """,
        (uid, name, username, OWNER_ID, iso_now()),
    )

    await log_activity(
        "ADMIN_ADDED",
        user_id=uid,
        user_name=name,
        username=username,
        admin_id=OWNER_ID,
        admin_name="OWNER",
    )

    await update.message.reply_text(
        f"â <b>Admin added</b>\n\nð¤ {escape(name)}\nð <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
    )


async def del_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/deladmin USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("â Invalid user ID.")
        return

    await db_execute("DELETE FROM admins WHERE user_id=?", (uid,))
    await update.message.reply_text(
        f"â Admin <code>{uid}</code> removed.",
        parse_mode=ParseMode.HTML,
    )


# =========================
# DEMO TIME
# =========================

async def demo_time_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_owner(q.from_user.id):
        await q.answer("â Owner only.", show_alert=True)
        return

    await q.edit_message_text(
        (
            "â±ï¸ <b>DEMO TIME</b>\n\n"
            f"Current Demo duration: <b>{DEMO_MINUTES} minutes</b>\n\n"
            "Demo member ko channel join karne ke baad "
            "5 minutes me automatically remove kiya jayega."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


# =========================
# CHANNELS
# =========================

async def channels_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_owner(q.from_user.id):
        await q.answer("â Owner only.", show_alert=True)
        return

    rows = await db_execute(
        "SELECT * FROM channels ORDER BY title COLLATE NOCASE",
        fetch=True,
    )

    lines = ["ð¡ <b>DETECTED CHANNELS</b>\n"]

    if not rows:
        lines.append("No channels detected yet.\n")
        lines.append("Bot ko kisi channel me administrator banao.")
    else:
        for i, r in enumerate(rows, 1):
            lines.append(
                f"<b>{i}. {escape(r['title'])}</b>\n"
                f"ð <code>{r['chat_id']}</code>\n"
                f"ð {html_link(r['invite_link'], 'Invite Link') if r['invite_link'] else 'Invite unavailable'}\n"
                f"ð {html_link(r['view_link'], 'View Link') if r['view_link'] else 'View link unavailable'}\n"
            )

    await q.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=back_keyboard(),
    )


# =========================
# ACCESS MESSAGE
# =========================

def access_message(
    *,
    access_type,
    course,
    invite_link,
    view_link,
):
    if access_type == "DEMO":
        heading = "ð <b>Access Granted: Demo Pass</b> â³"
        instruction = (
            "â ï¸ à¤¯à¤¹ <b>Demo Joining Link à¤à¥à¤µà¤² 1 à¤¬à¤¾à¤° à¤à¤¾à¤® à¤à¤°à¥à¤à¤¾à¥¤</b>\n"
            f"â±ï¸ à¤¸à¤¿à¤¸à¥à¤à¤® à¤à¤ªà¤à¥ à¤à¥à¤¨à¤² à¤®à¥à¤ à¤à¥à¤¡à¤¼à¤¨à¥ à¤à¥ <b>{DEMO_MINUTES} à¤®à¤¿à¤¨à¤ à¤¬à¤¾à¤¦ automatically remove</b> à¤à¤° à¤¦à¥à¤à¤¾à¥¤"
        )
    else:
        heading = "ð <b>Access Granted: Permanent Pass</b> ð"
        instruction = (
            "ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶ (Important):</b>\n"
            "ð« à¤¯à¤¹ Permanent Joining Link à¤à¥à¤µà¤² à¤à¤ à¤¬à¤¾à¤° à¤à¥ à¤²à¤¿à¤ à¤¹à¥à¥¤ "
            "à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¤¤à¥ à¤¹à¥ à¤¯à¤¹ expire à¤¹à¥ à¤à¤¾à¤à¤à¤¾à¥¤\n"
            "â à¤à¥à¤ªà¤¯à¤¾ à¤à¤à¥ à¤¸à¥ à¤à¥à¤²à¤¾à¤¸ à¤¦à¥à¤à¤¨à¥ à¤à¥ à¤²à¤¿à¤ à¤¹à¤®à¥à¤¶à¤¾ à¤à¤ªà¤° à¤¦à¤¿à¤ à¤à¤ "
            "<b>Course Material Link</b> à¤à¤¾ à¤¹à¥ à¤à¤ªà¤¯à¥à¤ à¤à¤°à¥à¤à¥¤"
        )

    return (
        f"{heading}\n"
        "âââââââââââââââââââââ\n\n"
        f"ð¢ <b>à¤à¥à¤¨à¤² / à¤à¥à¤°à¥à¤¸ à¤à¤¾ à¤¨à¤¾à¤®:</b>\n"
        f"âªï¸ {escape(course)}\n\n"
        "ð¥ <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
        f"ð {html_link(invite_link, invite_link or 'Link unavailable')}\n\n"
        "ð¥ï¸ <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤¯à¤¹à¤¾à¤ à¤¦à¥à¤à¥à¤ (View Link):</b>\n"
        f"ð {html_link(view_link, view_link or 'View link unavailable')}\n\n"
        "ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶ (Important):</b>\n"
        f"{instruction}"
    )


# =========================
# INVITE / ACCESS
# =========================

async def create_single_use_invite(context, channel_id, user_id, access_type):
    """
    Telegram invite links cannot universally be restricted to one exact user.
    We use member_limit=1 for a single-use link and log the intended recipient.
    """
    expire_dt = datetime.now(timezone.utc) + timedelta(minutes=30)

    try:
        inv = await context.bot.create_chat_invite_link(
            chat_id=channel_id,
            name=f"{access_type}-{user_id}",
            expire_date=expire_dt,
            member_limit=1,
            creates_join_request=False,
        )
        return inv.invite_link
    except TelegramError as e:
        logger.error("Invite creation failed: %s", e)
        return ""


async def grant_access(
    context,
    *,
    admin_id,
    member_user,
    channel_id,
    access_type,
):
    channel = await db_execute(
        "SELECT * FROM channels WHERE chat_id=?",
        (channel_id,),
        fetchone=True,
    )

    if not channel:
        return None, "Channel not found. Bot must be admin in the channel."

    course = channel["title"]
    invite = await create_single_use_invite(
        context,
        channel_id,
        member_user.id,
        access_type,
    )

    if not invite:
        return None, "Unable to create invite link. Check bot admin permissions."

    view_link = channel["view_link"]

    admin_name = await actor_name(context, admin_id)
    member_name = user_display(member_user)
    username = member_user.username or ""

    joined = now_local()
    expires = joined + timedelta(minutes=DEMO_MINUTES) if access_type == "DEMO" else None

    await db_execute(
        """
        INSERT INTO members
        (user_id,name,username,course,channel_id,channel_title,access_type,
         added_by,added_by_name,joined_at,expires_at,status,invite_link)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            member_user.id,
            member_name,
            username,
            course,
            channel_id,
            course,
            access_type,
            admin_id,
            admin_name,
            joined.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds") if expires else None,
            "PENDING",
            invite,
        ),
    )

    await log_activity(
        "ACCESS_GRANTED",
        user_id=member_user.id,
        user_name=member_name,
        username=username,
        admin_id=admin_id,
        admin_name=admin_name,
        course=course,
        channel_id=channel_id,
        channel_title=course,
        access_type=access_type,
        details="Access link generated",
    )

    if access_type == "DEMO":
        context.application.create_task(
            demo_expiry_task(
                context.application,
                channel_id,
                member_user.id,
                course,
            )
        )

    return (
        access_message(
            access_type=access_type,
            course=course,
            invite_link=invite,
            view_link=view_link,
        ),
        None,
    )


async def demo_expiry_task(application, channel_id, user_id, course):
    await asyncio.sleep(DEMO_MINUTES * 60)

    try:
        # Check if still present.
        member = await application.bot.get_chat_member(channel_id, user_id)

        if member.status in ("member", "restricted"):
            try:
                await application.bot.ban_chat_member(
                    channel_id,
                    user_id,
                    revoke_messages=False,
                )
                await application.bot.unban_chat_member(
                    channel_id,
                    user_id,
                    only_if_banned=True,
                )
            except TelegramError as e:
                logger.warning("Demo removal failed for %s: %s", user_id, e)

        await db_execute(
            """
            UPDATE members
            SET status='EXPIRED'
            WHERE user_id=? AND channel_id=? AND access_type='DEMO'
              AND status IN ('PENDING','ACTIVE')
            """,
            (user_id, channel_id),
        )

        await log_activity(
            "DEMO_EXPIRED",
            user_id=user_id,
            course=course,
            channel_id=channel_id,
            channel_title=course,
            access_type="DEMO",
            details="Demo expired after 5 minutes",
        )
    except TelegramError:
        pass
    except Exception:
        logger.exception("Demo expiry task failed")


# =========================
# MEMBER JOIN TRACKING
# =========================

async def member_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm:
        return

    chat = cm.chat
    if chat.type != ChatType.CHANNEL:
        return

    new_member = cm.new_chat_member
    old_member = cm.old_chat_member

    joined = (
        new_member.status in ("member", "restricted")
        and old_member.status in ("left", "kicked")
    )
    if not joined:
        return

    user = new_member.user
    row = await db_execute(
        """
        SELECT * FROM members
        WHERE user_id=? AND channel_id=? AND status='PENDING'
        ORDER BY id DESC LIMIT 1
        """,
        (user.id, chat.id),
        fetchone=True,
    )

    if not row:
        await log_activity(
            "MEMBER_JOIN",
            user_id=user.id,
            user_name=user_display(user),
            username=user.username or "",
            channel_id=chat.id,
            channel_title=chat.title or "",
            details="Member joined without pending access record",
        )
        return

    await db_execute(
        "UPDATE members SET status='ACTIVE', joined_at=? WHERE id=?",
        (iso_now(), row["id"]),
    )

    await log_activity(
        "MEMBER_JOIN",
        user_id=user.id,
        user_name=user_display(user),
        username=user.username or "",
        admin_id=row["added_by"],
        admin_name=row["added_by_name"],
        course=row["course"],
        channel_id=chat.id,
        channel_title=chat.title or "",
        access_type=row["access_type"],
        details="Member joined channel",
    )


# =========================
# REPORTS
# =========================

async def make_report(hours):
    end = now_local()
    start = end - timedelta(hours=hours)

    rows = await db_execute(
        """
        SELECT * FROM activity
        WHERE created_at >= ? AND created_at <= ?
        ORDER BY id ASC
        """,
        (
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
        ),
        fetch=True,
    )

    label = "LAST 1 HOUR" if hours == 1 else "LAST 24 HOURS"

    lines = [
        f"ð <b>REPORT CENTER</b>",
        f"ð <b>{label}</b>",
        f"ð {start.strftime('%d/%m/%Y %H:%M:%S')} â {end.strftime('%d/%m/%Y %H:%M:%S')} IST",
        "",
    ]

    if not rows:
        lines.append("â¹ï¸ Is period me koi activity record nahi hai.")
        return "\n".join(lines)

    # Aggregate cleanly without repeating the header for every row.
    counts = {}
    for r in rows:
        counts[r["event_type"]] = counts.get(r["event_type"], 0) + 1

    lines.append("ð <b>SUMMARY</b>")
    for event, count in counts.items():
        lines.append(f"â¢ {escape(event.replace('_', ' ').title())}: <b>{count}</b>")

    lines.append("")
    lines.append("ð¥ <b>ACTIVITY DETAILS</b>")
    lines.append("ââââââââââââââââââââ")

    for i, r in enumerate(rows, 1):
        event = r["event_type"].replace("_", " ").title()

        lines.append(f"<b>#{i} â¢ {escape(event)}</b>")

        if r["user_id"]:
            lines.append(
                f"ð¤ Member: <b>{escape(r['user_name'] or str(r['user_id']))}</b>"
            )
            lines.append(f"ð ID: <code>{r['user_id']}</code>")

        if r["admin_id"]:
            lines.append(
                f"ð§âð¼ Added by: <b>{escape(r['admin_name'] or str(r['admin_id']))}</b>"
            )

        if r["course"]:
            lines.append(f"ð Course: <b>{escape(r['course'])}</b>")

        if r["access_type"]:
            lines.append(f"ðï¸ Type: <b>{escape(r['access_type'])}</b>")

        if r["channel_title"]:
            lines.append(f"ð¡ Channel: <b>{escape(r['channel_title'])}</b>")

        lines.append(f"ð Time: <b>{escape(fmt_dt(r['created_at']))} IST</b>")

        if r["details"]:
            lines.append(f"â¹ï¸ {escape(r['details'])}")

        lines.append("")

    text = "\n".join(lines)

    # Telegram message limit protection.
    if len(text) > 3800:
        text = text[:3750] + "\n\nâ¦ <i>Report truncated due to Telegram message limit.</i>"

    return text


async def daily_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_admin(q.from_user.id):
        await q.answer("â Access denied.", show_alert=True)
        return

    text = (
        "ð <b>REPORT CENTER</b>\n\n"
        "ð <b>LAST 1 HOUR</b>\n"
        "<i>Last 60 minutes ki complete activity.</i>\n\n"
        "ð <b>LAST 24 HOURS</b>\n"
        "<i>Last 24 hours ki detailed activity.</i>\n\n"
        "Neeche jis report ko dekhna hai us par click karo."
    )

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=report_keyboard(),
    )


async def report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_admin(q.from_user.id):
        await q.answer("â Access denied.", show_alert=True)
        return

    hours = 1 if q.data == "report_1h" else 24

    try:
        text = await make_report(hours)
        await q.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=report_keyboard(),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.exception("Report error")
        await q.edit_message_text(
            "â <b>Report Error</b>\n\n"
            f"<code>{escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )


# =========================
# INLINE COURSE SEARCH
# =========================

async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query
    if not query:
        return

    search = (query.query or "").strip()

    rows = await db_execute(
        "SELECT * FROM channels ORDER BY title COLLATE NOCASE",
        fetch=True,
    )

    if search:
        terms = search.lower().split()
        filtered = []
        for r in rows:
            hay = f"{r['title']} {r['username']} {r['chat_id']}".lower()
            if all(t in hay for t in terms):
                filtered.append(r)
        rows = filtered

    from telegram import InlineQueryResultArticle, InputTextMessageContent

    results = []
    for r in rows[:30]:
        description = (
            f"Channel: {r['title']}\n"
            f"ID: {r['chat_id']}\n"
            "Auto-detected channel"
        )

        content = InputTextMessageContent(
            (
                f"ð¡ <b>{escape(r['title'])}</b>\n\n"
                f"ð <code>{r['chat_id']}</code>\n"
                "â Channel detected successfully."
            ),
            parse_mode=ParseMode.HTML,
        )

        results.append(
            InlineQueryResultArticle(
                id=f"channel_{r['chat_id']}",
                title=f"ð {r['title']}",
                description=description,
                input_message_content=content,
            )
        )

    await query.answer(
        results=results,
        cache_time=0,
        is_personal=True,
    )


# =========================
# CALLBACK ROUTER
# =========================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return

    data = q.data or ""

    if data == "home":
        if not await is_admin(q.from_user.id):
            await q.answer("â Access denied.", show_alert=True)
            return

        await q.answer()
        await q.edit_message_text(
            (
                "ð¸ <b>HELLO SELLER FAMILY, KESE HO?</b>\n\n"
                "â¨ <b>I AM WIZARD</b> ð¸ ð\n\n"
                "ð <b>COURSE SEARCH</b>\n"
                "Telegram ke kisi bhi chat me likho:\n"
                "<code>@YourBotUsername course-name</code>\n\n"
                "ð¡ <b>CHANNEL AUTO-DETECT</b>\n"
                "Bot ko channel me administrator banao â channel automatically detect hoga."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
        )
        return

    if data == "owner_panel":
        await owner_panel(update, context)
    elif data == "admins":
        await admins_view(update, context)
    elif data == "demo_time":
        await demo_time_view(update, context)
    elif data == "channels":
        await channels_view(update, context)
    elif data == "daily_report":
        await daily_report_menu(update, context)
    elif data in ("report_1h", "report_24h"):
        await report_button(update, context)
    else:
        await q.answer("Unknown button.", show_alert=True)


# =========================
# ACCESS COMMANDS
# =========================

async def give_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Commands:
      /demo USER_ID CHANNEL_ID
      /permanent USER_ID CHANNEL_ID
    """
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("â Access denied.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/demo USER_ID CHANNEL_ID</code>\n"
            "<code>/permanent USER_ID CHANNEL_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        user_id = int(context.args[0])
        channel_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("â USER_ID and CHANNEL_ID must be numbers.")
        return

    try:
        member_user = await context.bot.get_chat(user_id)
    except TelegramError:
        await update.message.reply_text(
            "â User not found. User must have started the bot before access is generated."
        )
        return

    access_type = "DEMO" if update.message.text.startswith("/demo") else "PERMANENT"

    msg, error = await grant_access(
        context,
        admin_id=update.effective_user.id,
        member_user=member_user,
        channel_id=channel_id,
        access_type=access_type,
    )

    if error:
        await update.message.reply_text(f"â {escape(error)}", parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


# =========================
# ERROR HANDLER
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception", exc_info=context.error)


# =========================
# MAIN
# =========================

async def post_init(application: Application):
    await db_init()
    logger.info("Database initialized: %s", DB_FILE)


def main():
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("deladmin", del_admin))
    application.add_handler(CommandHandler("demo", give_access_command))
    application.add_handler(CommandHandler("permanent", give_access_command))

    # Inline search
    application.add_handler(InlineQueryHandler(inline_search))

    # Channel auto-detection
    application.add_handler(
        ChatMemberHandler(
            my_chat_member_handler,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # Member join tracking
    application.add_handler(
        ChatMemberHandler(
            member_update_handler,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # All inline buttons use one router; this prevents duplicate callback handlers.
    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    application.add_error_handler(error_handler)

    logger.info("Bot starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
