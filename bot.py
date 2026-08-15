# -*- coding: utf-8 -*-

import os
import asyncio
import html
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatMemberUpdated,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

DB_FILE = "bot.db"
IST = ZoneInfo("Asia/Kolkata")

if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("BOT_TOKEN set karo.")

if not OWNER_IDS:
    raise RuntimeError(
        "OWNER_IDS set karo. Example: OWNER_IDS=123456789"
    )

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("COURSE-BOT")

# =========================================================
# TELEGRAM
# =========================================================

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row

db.execute("PRAGMA journal_mode=WAL")

db.executescript("""
CREATE TABLE IF NOT EXISTS channels (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    material_message_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    user_id INTEGER,
    username TEXT,
    name TEXT,
    channel_id INTEGER,
    channel_name TEXT,
    course_name TEXT,
    access_type TEXT,
    created_at TEXT NOT NULL,
    details TEXT
);

CREATE TABLE IF NOT EXISTS access_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    channel_id INTEGER NOT NULL,
    course_name TEXT,
    access_type TEXT NOT NULL,
    invite_link TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
""")

db.commit()

# =========================================================
# HELPERS
# =========================================================

def now():
    return datetime.now(IST)


def iso_now():
    return now().isoformat(timespec="seconds")


def esc(value):
    return html.escape(str(value or ""))


def is_owner(user_id):
    return user_id in OWNER_IDS


def is_admin(user_id):
    if is_owner(user_id):
        return True

    row = db.execute(
        "SELECT 1 FROM admins WHERE user_id=?",
        (user_id,)
    ).fetchone()

    return row is not None


def log_activity(
    event_type,
    user_id=None,
    username=None,
    name=None,
    channel_id=None,
    channel_name=None,
    course_name=None,
    access_type=None,
    details=None,
):
    db.execute("""
        INSERT INTO activity (
            event_type,
            user_id,
            username,
            name,
            channel_id,
            channel_name,
            course_name,
            access_type,
            created_at,
            details
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_type,
        user_id,
        username,
        name,
        channel_id,
        channel_name,
        course_name,
        access_type,
        iso_now(),
        details,
    ))

    db.commit()


# =========================================================
# HOME BUTTONS
# ONLY TWO BUTTONS ON HOME
# =========================================================

def home_keyboard(user_id):

    rows = []

    if is_admin(user_id):

        rows.append([
            InlineKeyboardButton(
                text="⚙️  OWNER PANEL",
                callback_data="OWNER"
            ),
            InlineKeyboardButton(
                text="📊  DAILY REPORT",
                callback_data="REPORT"
            )
        ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# =========================================================
# OWNER PANEL BUTTONS
# =========================================================

def owner_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="⏱️  DEMO TIME",
                    callback_data="DEMO_TIME"
                ),
                InlineKeyboardButton(
                    text="📊  REPORTS",
                    callback_data="REPORT"
                )
            ],

            [
                InlineKeyboardButton(
                    text="➕  ADD ADMIN",
                    callback_data="ADD_ADMIN"
                ),
                InlineKeyboardButton(
                    text="👥  VIEW ADMINS",
                    callback_data="VIEW_ADMINS"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📡  CHANNELS",
                    callback_data="CHANNELS"
                ),
                InlineKeyboardButton(
                    text="📚  COURSES",
                    callback_data="COURSES"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🏠  HOME",
                    callback_data="HOME"
                )
            ]
        ]
    )


def report_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🕐  LAST 1 HOUR",
                    callback_data="REPORT_1H"
                ),

                InlineKeyboardButton(
                    text="📅  LAST 24 HOURS",
                    callback_data="REPORT_24H"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🏠  HOME",
                    callback_data="HOME"
                )
            ]
        ]
    )


def back_keyboard(target="OWNER"):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️  BACK",
                    callback_data=target
                )
            ]
        ]
    )


# =========================================================
# HOME
# =========================================================

async def show_home(message, edit=False):

    me = await bot.get_me()

    text = (
        "<b>🌸 HELLO SELLER FAMILY, KESE HO?</b>\n\n"

        "<b>✨ I AM WIZARD 🌸💗</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "<b>🔎 COURSE SEARCH</b>\n"
        "Telegram ke kisi bhi chat me likho:\n"
        f"<code>@{esc(me.username)} course-name</code>\n\n"

        "<b>📡 CHANNEL AUTO-DETECT</b>\n"
        "Bot ko channel me <b>Administrator</b> banao "
        "→ channel automatically detect ho jayega.\n\n"

        "<i>✨ Fast • Clean • Secure Access System</i>"
    )

    keyboard = home_keyboard(
        message.from_user.id
    )

    if edit:
        await message.edit_text(
            text,
            reply_markup=keyboard
        )
    else:
        await message.answer(
            text,
            reply_markup=keyboard
        )


@dp.message(CommandStart())
async def start_handler(message: Message):

    log_activity(
        "START",
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )

    await show_home(message)


# =========================================================
# HOME CALLBACK
# =========================================================

@dp.callback_query(F.data == "HOME")
async def home_callback(callback: CallbackQuery):

    await callback.answer()

    await show_home(
        callback.message,
        edit=True
    )


# =========================================================
# OWNER PANEL
# =========================================================

@dp.callback_query(F.data == "OWNER")
async def owner_callback(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.edit_text(
        "<b>⚙️ OWNER PANEL</b>\n\n"
        "<b>💎 PREMIUM MANAGEMENT CENTER</b>\n\n"
        "👇 Required option select karo:",
        reply_markup=owner_keyboard()
    )


# =========================================================
# DAILY REPORT
# =========================================================

@dp.callback_query(F.data == "REPORT")
async def report_callback(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.edit_text(
        "<b>📊 REPORT CENTER</b>\n\n"

        "<b>🕐 LAST 1 HOUR</b>\n"
        "<i>Last 60 minutes ki complete activity.</i>\n\n"

        "<b>📅 LAST 24 HOURS</b>\n"
        "<i>Last 24 hours ki detailed activity.</i>",

        reply_markup=report_keyboard()
    )


# =========================================================
# REPORT DATA
# =========================================================

@dp.callback_query(F.data.in_({
    "REPORT_1H",
    "REPORT_24H"
}))
async def report_data(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    hours = (
        1
        if callback.data == "REPORT_1H"
        else 24
    )

    end = now()
    start = end - timedelta(
        hours=hours
    )

    rows = db.execute("""
        SELECT *
        FROM activity
        WHERE datetime(created_at)
        >= datetime(?)
        ORDER BY id DESC
        LIMIT 200
    """, (
        start.isoformat(timespec="seconds"),
    )).fetchall()

    if hours == 1:
        title = "🕐 LAST 1 HOUR REPORT"
    else:
        title = "📅 LAST 24 HOURS / DAILY REPORT"

    output = [

        f"<b>📊 {title}</b>",

        (
            f"<b>🕒 "
            f"{start:%d/%m/%Y %H:%M:%S}"
            f" → "
            f"{end:%d/%m/%Y %H:%M:%S}"
            f" IST</b>"
        ),

        "━━━━━━━━━━━━━━━━━━━━",
        ""
    ]

    if not rows:

        output.append(
            "ℹ️ <b>Is period me koi activity nahi mili.</b>"
        )

    for index, row in enumerate(rows, 1):

        username = (
            f"@{row['username']}"
            if row["username"]
            else "No username"
        )

        output.extend([

            f"<b>#{index} • "
            f"{esc(row['event_type'])}</b>",

            f"👤 <b>User:</b> "
            f"{esc(row['name'] or 'Unknown')}",

            f"🔗 <b>Username:</b> "
            f"{esc(username)}",

            f"📡 <b>Channel:</b> "
            f"{esc(row['channel_name'] or '—')}",

            f"📚 <b>Course:</b> "
            f"{esc(row['course_name'] or '—')}",

            f"🎟️ <b>Access:</b> "
            f"{esc(row['access_type'] or '—')}",

            f"🕒 <b>Time:</b> "
            f"{esc(row['created_at'].replace('T', ' '))} IST",

            ""
        ])

    # SAME MESSAGE EDIT
    # Duplicate report messages nahi banenge.

    await callback.answer()

    await callback.message.edit_text(
        "\n".join(output),
        reply_markup=report_keyboard()
    )


# =========================================================
# CHANNEL AUTO DETECT
# =========================================================

@dp.my_chat_member()
async def channel_detect(update: ChatMemberUpdated):

    chat = update.chat

    if chat.type != ChatType.CHANNEL:
        return

    status = update.new_chat_member.status

    if status not in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER
    }:
        return

    title = chat.title or "Untitled Channel"

    username = getattr(
        chat,
        "username",
        None
    )

    db.execute("""
        INSERT INTO channels (
            chat_id,
            title,
            username,
            detected_at
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(chat_id)
        DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            detected_at=excluded.detected_at
    """, (
        chat.id,
        title,
        username,
        iso_now()
    ))

    # Channel ke liye course automatically create.
    exists = db.execute(
        "SELECT 1 FROM courses WHERE channel_id=?",
        (chat.id,)
    ).fetchone()

    if not exists:

        db.execute("""
            INSERT INTO courses (
                channel_id,
                name,
                material_message_id,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            chat.id,
            title,
            None,
            iso_now()
        ))

    db.commit()

    log_activity(
        "CHANNEL_DETECTED",
        channel_id=chat.id,
        channel_name=title,
        details="Bot added/promoted as administrator."
    )


# =========================================================
# CHANNEL POST
# First channel post becomes material link
# =========================================================

@dp.channel_post()
async def channel_post(message: Message):

    chat = message.chat

    if chat.type != ChatType.CHANNEL:
        return

    title = chat.title or "Untitled Channel"

    username = getattr(
        chat,
        "username",
        None
    )

    db.execute("""
        INSERT INTO channels (
            chat_id,
            title,
            username,
            detected_at
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(chat_id)
        DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            detected_at=excluded.detected_at
    """, (
        chat.id,
        title,
        username,
        iso_now()
    ))

    course = db.execute("""
        SELECT id, material_message_id
        FROM courses
        WHERE channel_id=?
        ORDER BY id
        LIMIT 1
    """, (
        chat.id,
    )).fetchone()

    if not course:

        db.execute("""
            INSERT INTO courses (
                channel_id,
                name,
                material_message_id,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            chat.id,
            title,
            message.message_id,
            iso_now()
        ))

    elif not course["material_message_id"]:

        db.execute("""
            UPDATE courses
            SET material_message_id=?
            WHERE id=?
        """, (
            message.message_id,
            course["id"]
        ))

    db.commit()

    log_activity(
        "CHANNEL_POST",
        channel_id=chat.id,
        channel_name=title,
        details=f"message_id={message.message_id}"
    )


# =========================================================
# CHANNEL LIST
# =========================================================

@dp.callback_query(F.data == "CHANNELS")
async def channels_callback(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    rows = db.execute("""
        SELECT *
        FROM channels
        ORDER BY detected_at DESC
    """).fetchall()

    output = [
        "<b>📡 DETECTED CHANNELS</b>",
        ""
    ]

    if not rows:

        output.extend([
            "❌ <b>No channel detected yet.</b>",
            "",
            "Bot ko channel me "
            "<b>Administrator</b> banao.",
            "",
            "Channel me post/update aate hi "
            "channel database me save ho jayega."
        ])

    else:

        for row in rows:

            output.extend([
                f"📡 <b>{esc(row['title'])}</b>",
                f"🆔 <code>{row['chat_id']}</code>",
                f"🕒 {esc(row['detected_at'])} IST",
                ""
            ])

    await callback.answer()

    await callback.message.edit_text(
        "\n".join(output),
        reply_markup=back_keyboard()
    )


# =========================================================
# COURSES
# =========================================================

@dp.callback_query(F.data == "COURSES")
async def courses_callback(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    rows = db.execute("""
        SELECT
            c.name,
            c.channel_id
        FROM courses c
        ORDER BY c.id DESC
    """).fetchall()

    if not rows:

        await callback.answer(
            "No courses found.",
            show_alert=True
        )

        return

    keyboard = []

    for row in rows:

        keyboard.append([
            InlineKeyboardButton(
                text=f"📚  {row['name'][:40]}",
                callback_data=f"COURSE:{row['channel_id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="↩️  BACK",
            callback_data="OWNER"
        )
    ])

    await callback.answer()

    await callback.message.edit_text(
        "<b>📚 COURSE SELECT</b>\n\n"
        "Course select karo:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )
    )


# =========================================================
# COURSE ACCESS MENU
# =========================================================

@dp.callback_query(F.data.startswith("COURSE:"))
async def course_callback(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    channel_id = int(
        callback.data.split(":", 1)[1]
    )

    row = db.execute("""
        SELECT
            c.name,
            ch.title
        FROM courses c
        JOIN channels ch
        ON ch.chat_id=c.channel_id
        WHERE c.channel_id=?
        ORDER BY c.id
        LIMIT 1
    """, (
        channel_id,
    )).fetchone()

    if not row:

        await callback.answer(
            "Course not found.",
            show_alert=True
        )

        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🎓  DEMO PASS",
                    callback_data=f"DEMO:{channel_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="💎  PERMANENT PASS",
                    callback_data=f"PERMANENT:{channel_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="↩️  BACK",
                    callback_data="COURSES"
                )
            ]
        ]
    )

    await callback.answer()

    await callback.message.edit_text(
        f"<b>📚 {esc(row['name'])}</b>\n\n"
        "<b>🎟️ ACCESS TYPE SELECT KARO:</b>",
        reply_markup=keyboard
    )


# =========================================================
# MATERIAL LINK
# =========================================================

def material_link(
    channel_id,
    message_id,
    username=None
):

    if not message_id:
        return None

    # Public channel
    if username:
        return (
            f"https://t.me/"
            f"{username}/"
            f"{message_id}"
        )

    # Private channel
    internal_id = str(channel_id)

    if internal_id.startswith("-100"):
        internal_id = internal_id[4:]

    return (
        f"https://t.me/c/"
        f"{internal_id}/"
        f"{message_id}"
    )


# =========================================================
# CREATE INVITE
# =========================================================

async def create_invite(
    channel_id,
    demo=False
):

    expire = None

    if demo:

        expire = (
            datetime.now(timezone.utc)
            + timedelta(minutes=5)
        )

    invite = await bot.create_chat_invite_link(

        chat_id=channel_id,

        name=(
            "Demo Access"
            if demo
            else
            "Permanent Access"
        ),

        expire_date=expire,

        member_limit=1
    )

    return invite


# =========================================================
# ACCESS MESSAGE
# =========================================================

async def create_access(
    callback,
    demo
):

    if not is_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    channel_id = int(
        callback.data.split(":", 1)[1]
    )

    channel = await bot.get_chat(
        channel_id
    )

    course = db.execute("""
        SELECT
            name,
            material_message_id
        FROM courses
        WHERE channel_id=?
        ORDER BY id
        LIMIT 1
    """, (
        channel_id,
    )).fetchone()

    course_name = (
        course["name"]
        if course
        else
        channel.title or "Course"
    )

    try:

        invite = await create_invite(
            channel_id,
            demo
        )

    except Exception:

        log.exception(
            "Invite creation failed"
        )

        await callback.answer(
            "❌ Bot ko channel me invite permission do.",
            show_alert=True
        )

        return

    material = material_link(

        channel_id,

        (
            course["material_message_id"]
            if course
            else None
        ),

        getattr(
            channel,
            "username",
            None
        )
    )

    if demo:

        access_type = "DEMO"

        title = (
            "🎓 <b>ACCESS GRANTED: "
            "DEMO PASS</b> ⏳"
        )

        important = (
            "⚠️ <b>यह Demo Joining Link "
            "केवल 1 बार काम करेगा।</b>\n"
            "⏱️ Join करने के <b>5 मिनट बाद</b> "
            "demo member automatically remove होगा।"
        )

    else:

        access_type = "PERMANENT"

        title = (
            "💎 <b>ACCESS GRANTED: "
            "PERMANENT PASS</b> 💎"
        )

        important = (
            "⚠️ <b>यह Permanent Joining Link "
            "केवल 1 बार काम करेगा।</b>\n"
            "✅ आगे से class देखने के लिए "
            "हमेशा <b>Course Material Link</b> "
            "का ही उपयोग करें।"
        )

    text = (

        f"{title}\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "🏢 <b>चैनल / कोर्स का नाम:</b>\n"

        f"↪️ <b>{esc(channel.title or course_name)}</b>\n\n"

        "📌 <b>महत्वपूर्ण निर्देश (Important):</b>\n"

        f"{important}\n\n"

        "<i>✨ Premium • Clean • Secure Access</i>"
    )

    buttons = [

        [
            InlineKeyboardButton(
                text="📥  JOIN / ACCESS",
                url=invite.invite_link
            )
        ]
    ]

    if material:

        buttons.append([
            InlineKeyboardButton(
                text="🖥️  COURSE MATERIAL",
                url=material
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="↩️  BACK",
            callback_data="COURSES"
        )
    ])

    expires_at = None

    if demo:

        expires_at = (
            now()
            + timedelta(minutes=5)
        ).isoformat(
            timespec="seconds"
        )

    db.execute("""
        INSERT INTO access_links (
            user_id,
            channel_id,
            course_name,
            access_type,
            invite_link,
            created_at,
            expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (

        callback.from_user.id,

        channel_id,

        course_name,

        access_type,

        invite.invite_link,

        iso_now(),

        expires_at
    ))

    db.commit()

    log_activity(

        "ACCESS_LINK_CREATED",

        callback.from_user.id,

        callback.from_user.username,

        callback.from_user.full_name,

        channel_id,

        channel.title,

        course_name,

        access_type
    )

    await callback.answer(
        "✅ Access link created."
    )

    # SAME MESSAGE EDIT
    # Duplicate messages nahi.

    await callback.message.edit_text(

        text,

        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )


@dp.callback_query(F.data.startswith("DEMO:"))
async def demo_callback(
    callback: CallbackQuery
):

    await create_access(
        callback,
        True
    )


@dp.callback_query(F.data.startswith("PERMANENT:"))
async def permanent_callback(
    callback: CallbackQuery
):

    await create_access(
        callback,
        False
    )


# =========================================================
# MEMBER JOIN TRACKING
# =========================================================

@dp.chat_member()
async def member_join(
    update: ChatMemberUpdated
):

    if update.chat.type != ChatType.CHANNEL:
        return

    old_status = (
        update.old_chat_member.status
    )

    new_status = (
        update.new_chat_member.status
    )

    joined = (

        new_status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR
        }

        and

        old_status in {
            ChatMemberStatus.LEFT,
            ChatMemberStatus.KICKED
        }
    )

    if not joined:
        return

    user = update.new_chat_member.user

    invite = getattr(
        update,
        "invite_link",
        None
    )

    invite_url = getattr(
        invite,
        "invite_link",
        None
    )

    access_type = "UNKNOWN"
    course_name = (
        update.chat.title
        or "Course"
    )

    if invite_url:

        row = db.execute("""
            SELECT
                access_type,
                course_name
            FROM access_links
            WHERE invite_link=?
            ORDER BY id DESC
            LIMIT 1
        """, (
            invite_url,
        )).fetchone()

        if row:

            access_type = (
                row["access_type"]
            )

            course_name = (
                row["course_name"]
                or course_name
            )

    log_activity(

        "MEMBER_JOINED",

        user.id,

        user.username,

        user.full_name,

        update.chat.id,

        update.chat.title,

        course_name,

        access_type,

        f"invite={invite_url or 'unknown'}"
    )


# =========================================================
# SEARCH
# =========================================================

@dp.message(Command("search"))
async def search_handler(
    message: Message
):

    query = (
        message.text
        .partition(" ")[2]
        .strip()
    )

    if not query:

        await message.answer(
            "<b>🔎 COURSE SEARCH</b>\n\n"
            "Use:\n"
            "<code>/search course-name</code>"
        )

        return

    rows = db.execute("""
        SELECT
            c.name,
            c.channel_id
        FROM courses c
        JOIN channels ch
        ON ch.chat_id=c.channel_id
        WHERE
            lower(c.name)
            LIKE lower(?)

            OR

            lower(ch.title)
            LIKE lower(?)

        ORDER BY c.id DESC

        LIMIT 10
    """, (
        f"%{query}%",
        f"%{query}%"
    )).fetchall()

    if not rows:

        await message.answer(
            "❌ <b>No course found.</b>"
        )

        return

    keyboard = []

    for row in rows:

        keyboard.append([

            InlineKeyboardButton(
                text=f"📚  {row['name'][:35]}",
                callback_data=(
                    f"COURSE:{row['channel_id']}"
                )
            )

        ])

    await message.answer(

        f"<b>🔎 SEARCH RESULTS:</b> "
        f"{esc(query)}",

        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )
    )


# =========================================================
# ADD ADMIN
# =========================================================

@dp.callback_query(F.data == "ADD_ADMIN")
async def add_admin_button(
    callback: CallbackQuery
):

    if not is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.edit_text(

        "<b>➕ ADD ADMIN</b>\n\n"
        "User ID bhejo:\n\n"
        "<code>/addadmin 123456789</code>",

        reply_markup=back_keyboard()
    )


@dp.message(Command("addadmin"))
async def add_admin_command(
    message: Message
):

    if not is_owner(
        message.from_user.id
    ):

        await message.answer(
            "❌ <b>Owner only.</b>"
        )

        return

    parts = message.text.split(
        maxsplit=1
    )

    if (
        len(parts) != 2
        or
        not parts[1].lstrip("-").isdigit()
    ):

        await message.answer(
            "Use:\n"
            "<code>/addadmin 123456789</code>"
        )

        return

    user_id = int(parts[1])

    db.execute(
        """
        INSERT OR IGNORE INTO admins(
            user_id,
            added_at
        )
        VALUES (?, ?)
        """,
        (
            user_id,
            iso_now()
        )
    )

    db.commit()

    await message.answer(
        f"✅ <b>Admin added:</b> "
        f"<code>{user_id}</code>"
    )


# =========================================================
# VIEW ADMINS
# =========================================================

@dp.callback_query(F.data == "VIEW_ADMINS")
async def view_admins(
    callback: CallbackQuery
):

    if not is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    rows = db.execute("""
        SELECT *
        FROM admins
        ORDER BY added_at DESC
    """).fetchall()

    output = [
        "<b>👥 VIEW ADMINS</b>",
        ""
    ]

    if not rows:

        output.append(
            "ℹ️ No additional admins."
        )

    else:

        for index, row in enumerate(
            rows,
            1
        ):

            output.append(
                f"<b>#{index}</b> • "
                f"<code>{row['user_id']}</code> • "
                f"{esc(row['added_at'])}"
            )

    await callback.answer()

    await callback.message.edit_text(
        "\n".join(output),
        reply_markup=back_keyboard()
    )


# =========================================================
# DEMO TIME
# =========================================================

@dp.callback_query(F.data == "DEMO_TIME")
async def demo_time(
    callback: CallbackQuery
):

    if not is_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.edit_text(

        "<b>⏱️ DEMO TIME</b>\n\n"

        "🎓 Demo Duration: "
        "<b>5 Minutes</b>\n\n"

        "🔗 Invite: <b>Single Use</b>\n"
        "🚪 Demo member: "
        "<b>5 minutes ke baad remove</b>",

        reply_markup=back_keyboard()
    )


# =========================================================
# RUN
# =========================================================

async def main():

    log.info(
        "Starting Premium Course Bot..."
    )

    # Old pending updates ko replay nahi karega.
    await bot.delete_webhook(
        drop_pending_updates=True
    )

    me = await bot.get_me()

    log.info(
        "Running as @%s",
        me.username
    )

    await dp.start_polling(
        bot,
        allowed_updates=(
            dp.resolve_used_update_types()
        )
    )


if __name__ == "__main__":

    asyncio.run(main())
