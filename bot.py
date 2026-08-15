# -*- coding: utf-8 -*-
import os
import html
import logging
import sqlite3
import asyncio
import socket
import atexit
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes,
    InlineQueryHandler, ChatMemberHandler, filters
)


# ============================================================
# SINGLE INSTANCE / DUPLICATE PROTECTION
# ============================================================
_INSTANCE_LOCK_HANDLE = None

def acquire_single_instance_lock():
    """
    Allow only ONE copy of this bot on the same machine/container.

    This prevents the exact symptom where one /start creates the same
    screen many times or one report button opens REPORT CENTER repeatedly.
    The lock is released automatically when the process exits.
    """
    global _INSTANCE_LOCK_HANDLE

    lock_path = os.getenv(
        "BOT_LOCK_PATH",
        os.path.abspath("raj_course_bot.lock")
    )

    handle = open(lock_path, "a+", encoding="utf-8")

    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                raise RuntimeError(
                    "Another RAJ COURSE BOT process is already running. "
                    "Stop the old bot process/workflow first."
                )
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise RuntimeError(
                    "Another RAJ COURSE BOT process is already running. "
                    "Stop the old bot process/workflow first."
                )

        handle.seek(0)
        handle.truncate()
        handle.write(
            f"pid={os.getpid()} host={socket.gethostname()}\n"
        )
        handle.flush()

        _INSTANCE_LOCK_HANDLE = handle
        atexit.register(release_single_instance_lock)

    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        raise


def release_single_instance_lock():
    global _INSTANCE_LOCK_HANDLE
    if _INSTANCE_LOCK_HANDLE is None:
        return

    try:
        if os.name != "nt":
            import fcntl
            fcntl.flock(
                _INSTANCE_LOCK_HANDLE.fileno(),
                fcntl.LOCK_UN
            )
        else:
            import msvcrt
            _INSTANCE_LOCK_HANDLE.seek(0)
            msvcrt.locking(
                _INSTANCE_LOCK_HANDLE.fileno(),
                msvcrt.LK_UNLCK,
                1
            )
    except Exception:
        pass

    try:
        _INSTANCE_LOCK_HANDLE.close()
    except Exception:
        pass

    _INSTANCE_LOCK_HANDLE = None


# In-process update/callback de-duplication.
# Telegram normally delivers each update once, but this protects against
# accidental repeated delivery while the process is alive.
_SEEN_EVENTS = {}
_SEEN_EVENT_LIMIT = 2000


def claim_event(key, ttl=300):
    current = asyncio.get_event_loop().time()

    old_keys = [
        k for k, timestamp in _SEEN_EVENTS.items()
        if current - timestamp > ttl
    ]
    for k in old_keys:
        _SEEN_EVENTS.pop(k, None)

    if key in _SEEN_EVENTS:
        return False

    _SEEN_EVENTS[key] = current

    if len(_SEEN_EVENTS) > _SEEN_EVENT_LIMIT:
        oldest = sorted(
            _SEEN_EVENTS.items(),
            key=lambda item: item[1]
        )[:200]
        for k, _ in oldest:
            _SEEN_EVENTS.pop(k, None)

    return True

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "bot.db")

ADD_ADMIN, ADD_CATEGORY, ADD_COURSE = range(3)
IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("raj-course-bot")


# ============================================================
# DATABASE
# ============================================================
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS admins(
            uid INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            permissions TEXT NOT NULL DEFAULT 'demo'
        );

        CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emoji TEXT NOT NULL DEFAULT '\U0001f4da',
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS courses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            tg_id TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS channels(
            chat_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            detected_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS links(
            invite_link TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            course_id INTEGER NOT NULL,
            course_name TEXT NOT NULL,
            link_type TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            creator_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS members(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            course_id INTEGER NOT NULL,
            course_name TEXT NOT NULL,
            invite_link TEXT NOT NULL,
            link_type TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            creator_name TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            expires_at TEXT,
            removed_at TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reports_sent(
            report_key TEXT PRIMARY KEY,
            message_id INTEGER,
            sent_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS report_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_key TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_members_joined ON members(joined_at);
        CREATE INDEX IF NOT EXISTS idx_members_creator ON members(creator_id);
        CREATE INDEX IF NOT EXISTS idx_links_created ON links(created_at);
        """)

        defaults = [
            ("\U0001f4da", "Teaching Exam's"),
            ("\U0001f6a9", "Ras/Psi"),
            ("\U0001f4dd", "EO-Ro/bstc/cet"),
            ("\U0001f393", "Net-Jrf"),
            ("\u2728", "Other Exam's"),
        ]
        for emoji, name in defaults:
            c.execute(
                "INSERT OR IGNORE INTO categories(emoji,name) VALUES(?,?)",
                (emoji, name)
            )

        c.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('demo_minutes','5')"
        )

        if OWNER_ID:
            c.execute(
                "INSERT OR IGNORE INTO admins(uid,name,permissions) VALUES(?,?,?)",
                (OWNER_ID, "Owner", "all")
            )


init_db()


# ============================================================
# HELPERS
# ============================================================
def now():
    return datetime.now(timezone.utc)


def ist_now():
    return datetime.now(IST)


def iso(dt):
    return dt.isoformat() if dt else None


def esc(value):
    return html.escape(str(value or ""))


def user_name(user):
    name = " ".join(
        x for x in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
        if x
    ).strip()
    return name or (
        f"@{user.username}" if getattr(user, "username", None) else str(user.id)
    )


def owner(uid):
    return bool(OWNER_ID and uid == OWNER_ID)


def admin(uid):
    if owner(uid):
        return True
    with db() as c:
        return c.execute(
            "SELECT 1 FROM admins WHERE uid=?", (uid,)
        ).fetchone() is not None


def perms(uid):
    if owner(uid):
        return {"all", "demo", "perm"}
    with db() as c:
        row = c.execute(
            "SELECT permissions FROM admins WHERE uid=?", (uid,)
        ).fetchone()
    if not row:
        return set()
    return {x.strip().lower() for x in row["permissions"].split(",") if x.strip()}


def can(uid, permission):
    p = perms(uid)
    return "all" in p or permission in p


def admin_name(uid):
    if owner(uid):
        return "Owner"
    with db() as c:
        row = c.execute(
            "SELECT name FROM admins WHERE uid=?", (uid,)
        ).fetchone()
    return row["name"] if row else f"Admin {uid}"


def demo_minutes():
    with db() as c:
        row = c.execute(
            "SELECT value FROM settings WHERE key='demo_minutes'"
        ).fetchone()
    try:
        return int(row["value"])
    except Exception:
        return 5


def rows_buttons(items, cols=2):
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def clean_ui_text(text):
    return str(text)


async def safe_edit(query, text, keyboard=None):
    try:
        await query.edit_message_text(
            clean_ui_text(text),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        log.warning("edit: %s", e)


def local_dt(value):
    try:
        return datetime.fromisoformat(value).astimezone(IST)
    except Exception:
        return None


def local_date(value):
    d = local_dt(value)
    return d.date().isoformat() if d else str(value)[:10]


def local_time(value):
    d = local_dt(value)
    return d.strftime("%d/%m/%Y %H:%M:%S IST") if d else str(value)


# ============================================================
# HOME / AUTO-DETECTED CHANNELS
# ============================================================
def home_keyboard():
    # Owner Home: exactly TWO buttons. No channels/categories/add-course.
    if owner(OWNER_ID):
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "\u2699\ufe0f OWNER PANEL",
                    callback_data="owner"
                ),
                InlineKeyboardButton(
                    "\U0001f4ca DAILY REPORT",
                    callback_data="reports"
                ),
            ]
        ])

    # Non-owner sellers keep their normal course search access.
    with db() as c:
        cats = c.execute(
            "SELECT id,emoji,name FROM categories ORDER BY id"
        ).fetchall()

    buttons = [
        InlineKeyboardButton(
            f"{r['emoji']} {r['name']}",
            callback_data=f"cat:{r['id']}"
        )
        for r in cats
    ]
    return InlineKeyboardMarkup(rows_buttons(buttons, 2))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.update_id is not None and not claim_event(f"start:{update.update_id}"):
        return

    if not admin(uid):
        msg = (
            "\u274c <b>ACCESS DENIED</b>\n\n"
            "\U0001f512 Aapko is bot ka access nahi diya gaya hai."
        )
        if update.message:
            await update.message.reply_text(
                msg, parse_mode=ParseMode.HTML
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "\u274c Access Denied",
                show_alert=True
            )
        return

    text = (
        "\U0001f338 <b>HELLO SELLER FAMILY, KESE HO?</b>\n\n"
        "\u2728 <b>I AM WIZARD</b> \U0001f338\U0001f495\n\n"
        "\u2501" * 18 + "\n\n"
        "\U0001f50e <b>COURSE SEARCH</b>\n\n"
        "<b>Telegram ke kisi bhi chat me likho:</b>\n"
        "<code>@YourBotUsername course-name</code>\n\n"
        "\U0001f449 <b>Search result me course select karo.</b>\n\n"
        "\U0001f4a1 <i>Fast \u2022 Clean \u2022 Secure Access System</i>"
    )

    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(
            update.callback_query,
            text,
            home_keyboard()
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=home_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

async def bot_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm:
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("channel", "supergroup"):
        return

    new = cm.new_chat_member
    if new.status not in ("administrator", "creator"):
        if new.status in ("left", "kicked"):
            with db() as c:
                c.execute(
                    "DELETE FROM channels WHERE chat_id=?",
                    (str(chat.id),)
                )
        return

    # Save every channel where this bot becomes admin.
    title = getattr(chat, "title", None) or str(chat.id)
    username = getattr(chat, "username", None)

    with db() as c:
        c.execute(
            """
            INSERT INTO channels(chat_id,title,username,detected_at)
            VALUES(?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                username=excluded.username
            """,
            (str(chat.id), title, username, iso(now()))
        )

    log.info("Detected channel: %s (%s)", title, chat.id)

    # Automatically create a course if this channel isn't registered.
    with db() as c:
        existing = c.execute(
            "SELECT id FROM courses WHERE tg_id=?",
            (str(chat.id),)
        ).fetchone()
        cat = c.execute(
            "SELECT id FROM categories ORDER BY id LIMIT 1"
        ).fetchone()

        if not existing and cat:
            c.execute(
                """
                INSERT OR IGNORE INTO courses(category_id,name,tg_id)
                VALUES(?,?,?)
                """,
                (cat["id"], title, str(chat.id))
            )

    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                "\U0001f4e1 <b>CHANNEL AUTO-DETECTED</b>\n\n"
                f"\U0001f4da Channel: <b>{esc(title)}</b>\n"
                f"\U0001f194 ID: <code>{chat.id}</code>\n\n"
                "Channel ko automatically course list me add kar diya.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


async def channels_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not admin(q.from_user.id):
        await q.answer("\u274c Access denied", show_alert=True)
        return
    await q.answer()

    with db() as c:
        rows = c.execute(
            "SELECT * FROM channels ORDER BY title"
        ).fetchall()

    kb = []
    for r in rows:
        kb.append([
            InlineKeyboardButton(
                f"\U0001f4e1 {r['title']}",
                callback_data=f"detected:{r['chat_id']}"
            )
        ])
    kb.append([InlineKeyboardButton("\u2b05\ufe0f Home", callback_data="home")])

    text = (
        "\U0001f4e1 <b>AUTO-DETECTED CHANNELS</b>\n\n"
        "Bot ko administrator banate hi channel yahan automatically aata hai.\n\n"
        f"Total: <b>{len(rows)}</b>"
    )
    await safe_edit(q, text, InlineKeyboardMarkup(kb))


async def detected_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not admin(q.from_user.id):
        await q.answer("\u274c Access denied", show_alert=True)
        return
    await q.answer()

    chat_id = q.data.split(":", 1)[1]
    with db() as c:
        channel = c.execute(
            "SELECT * FROM channels WHERE chat_id=?",
            (chat_id,)
        ).fetchone()
        courses = c.execute(
            "SELECT * FROM courses WHERE tg_id=?",
            (chat_id,)
        ).fetchall()

    if not channel:
        await safe_edit(
            q,
            "\u274c Channel not found.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2b05\ufe0f Channels", callback_data="channels")]
            ])
        )
        return

    text = (
        "\U0001f4e1 <b>CHANNEL</b>\n\n"
        f"\U0001f4da Name: <b>{esc(channel['title'])}</b>\n"
        f"\U0001f194 ID: <code>{esc(chat_id)}</code>\n\n"
        f"\U0001f4d6 Registered courses: <b>{len(courses)}</b>\n"
    )
    kb = []
    for r in courses:
        kb.append([
            InlineKeyboardButton(
                f"\U0001f4da {r['name']}",
                callback_data=f"course:{r['id']}"
            )
        ])
    kb.append([
        InlineKeyboardButton("\u2b05\ufe0f Channels", callback_data="channels")
    ])

    await safe_edit(q, text, InlineKeyboardMarkup(kb))


# ============================================================
# CATEGORY / COURSE
# ============================================================
async def category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split(":")[1])

    with db() as c:
        cat = c.execute(
            "SELECT * FROM categories WHERE id=?", (cid,)
        ).fetchone()
        courses = c.execute(
            "SELECT * FROM courses WHERE category_id=? ORDER BY name",
            (cid,)
        ).fetchall()

    if not cat:
        return

    kb = rows_buttons([
        InlineKeyboardButton(
            f"\U0001f4da {r['name']}",
            callback_data=f"course:{r['id']}"
        )
        for r in courses
    ], 2)

    kb.append([InlineKeyboardButton("\u2b05\ufe0f Home", callback_data="home")])

    await safe_edit(
        q,
        f"{cat['emoji']} <b>{esc(cat['name'])}</b>\n\n"
        f"Courses: <b>{len(courses)}</b>",
        InlineKeyboardMarkup(kb)
    )


async def course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split(":")[1])

    with db() as c:
        r = c.execute(
            "SELECT * FROM courses WHERE id=?", (course_id,)
        ).fetchone()

    if not r:
        await q.answer("\u274c Course not found", show_alert=True)
        return

    buttons = []
    if can(q.from_user.id, "demo"):
        buttons.append(
            InlineKeyboardButton(
                "\U0001f517 Demo Link",
                callback_data=f"link:demo:{course_id}"
            )
        )
    if can(q.from_user.id, "perm"):
        buttons.append(
            InlineKeyboardButton(
                "\U0001f464 Permanent Link",
                callback_data=f"link:perm:{course_id}"
            )
        )

    if not buttons:
        await q.answer("\u274c No access", show_alert=True)
        return

    kb = rows_buttons(buttons, 2)
    kb.append([
        InlineKeyboardButton(
            "\u2b05\ufe0f Back",
            callback_data=f"cat:{r['category_id']}"
        )
    ])

    await safe_edit(
        q,
        f"\U0001f4da <b>{esc(r['name'])}</b>\n\n"
        "Choose an access link below:",
        InlineKeyboardMarkup(kb)
    )


# ============================================================
# TELEGRAM PERMISSION CHECK + LINK CREATION
# ============================================================
async def permission_check(bot, chat_id, demo=False):
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)

        if member.status not in ("administrator", "creator"):
            return False, (
                f"Bot status: <b>{esc(member.status)}</b>\n"
                "Bot ko isi channel me administrator banana zaroori hai."
            )

        if member.status == "creator":
            return True, ""

        invite = getattr(member, "can_invite_users", None)
        restrict = getattr(member, "can_restrict_members", None)

        if invite is not True:
            return False, (
                "\u274c <b>Add Subscribers / Invite Users via Link</b> OFF hai.\n"
                f"Invite permission: <code>{invite}</code>\n\n"
                "Channel \u2192 Administrators \u2192 Bot \u2192 "
                "Add Subscribers ON \u2192 Save."
            )

        if demo and restrict is not True:
            return False, (
                "\u274c Demo auto-remove ke liye <b>Ban Users</b> ON hona chahiye.\n"
                f"Ban Users: <code>{restrict}</code>"
            )

        return True, ""
    except Exception as e:
        return False, f"\u274c Telegram check failed:\n<code>{esc(e)}</code>"


async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, link_type, course_id_s = q.data.split(":")
    course_id = int(course_id_s)

    if not can(q.from_user.id, link_type):
        await q.answer("\u274c Permission denied", show_alert=True)
        return

    with db() as c:
        course_row = c.execute(
            "SELECT * FROM courses WHERE id=?", (course_id,)
        ).fetchone()

    if not course_row:
        await q.answer("\u274c Course not found", show_alert=True)
        return

    chat_id = int(course_row["tg_id"])
    ok, reason = await permission_check(
        context.bot,
        chat_id,
        demo=(link_type == "demo")
    )

    if not ok:
        await safe_edit(
            q,
            "\u274c <b>Link create nahi hua</b>\n\n"
            f"\U0001f4da Course: <b>{esc(course_row['name'])}</b>\n\n"
            f"{reason}\n\n"
            "Permission save karne ke baad /checkchannel chalao.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=f"course:{course_id}"
                )]
            ])
        )
        return

    try:
        created = now()
        creator = admin_name(q.from_user.id)

        invite = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            name=f"{link_type}_{q.from_user.id}_{created.strftime('%Y%m%d%H%M%S')}",
            member_limit=1,
            creates_join_request=False
        )

        with db() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO links(
                    invite_link,chat_id,course_id,course_name,link_type,
                    creator_id,creator_name,created_at,used
                ) VALUES(?,?,?,?,?,?,?,?,0)
                """,
                (
                    invite.invite_link,
                    str(chat_id),
                    course_id,
                    course_row["name"],
                    link_type,
                    q.from_user.id,
                    creator,
                    iso(created)
                )
            )

        if link_type == "demo":
            text = (
                "\U0001f393 <b>Access Granted: Demo Pass</b>\n\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f3e2 <b>Course:</b> {esc(course_row['name'])}\n\n"
                "\U0001f4e5 <b>Join:</b>\n"
                f"\U0001f517 <a href=\"{esc(invite.invite_link)}\">Open Demo Link</a>\n\n"
                "\U0001f4cc <b>Important:</b>\n"
                "\u26a0\ufe0f This joining link works only once.\n"
                f"\u23f1 Member will be removed automatically after "
                f"<b>{demo_minutes()} minutes</b>."
            )
        else:
            text = (
                "\U0001f48e <b>Access Granted: Permanent Pass</b>\n\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f3e2 <b>Course:</b> {esc(course_row['name'])}\n\n"
                "\U0001f4e5 <b>Join:</b>\n"
                f"\U0001f517 <a href=\"{esc(invite.invite_link)}\">Open Permanent Link</a>\n\n"
                "\u26a0\ufe0f This joining link works only once."
            )

        await safe_edit(
            q,
            text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=f"course:{course_id}"
                )]
            ])
        )

    except Exception as e:
        log.exception("link creation")
        await safe_edit(
            q,
            "\u274c <b>Telegram link creation failed</b>\n\n"
            f"<code>{esc(e)}</code>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=f"course:{course_id}"
                )]
            ])
        )


# ============================================================
# MEMBER TRACKING
# ============================================================
async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm:
        return

    old = cm.old_chat_member.status
    new = cm.new_chat_member.status

    if new not in ("member", "administrator"):
        return
    if old not in ("left", "kicked"):
        return

    invite = getattr(cm, "invite_link", None)
    invite_url = getattr(invite, "invite_link", None) if invite else None
    if not invite_url:
        return

    with db() as c:
        link = c.execute(
            "SELECT * FROM links WHERE invite_link=?",
            (invite_url,)
        ).fetchone()

    if not link:
        return

    u = cm.from_user
    joined = cm.date or now()
    expires = (
        joined + timedelta(minutes=demo_minutes())
        if link["link_type"] == "demo" else None
    )

    with db() as c:
        # Keep historical rows. Never delete a demo member from reports.
        c.execute(
            """
            INSERT INTO members(
                user_id,username,full_name,chat_id,course_id,course_name,
                invite_link,link_type,creator_id,creator_name,joined_at,
                expires_at,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                u.id,
                getattr(u, "username", None),
                user_name(u),
                str(cm.chat.id),
                link["course_id"],
                link["course_name"],
                invite_url,
                link["link_type"],
                link["creator_id"],
                link["creator_name"],
                iso(joined),
                iso(expires),
                "active"
            )
        )
        c.execute(
            "UPDATE links SET used=1 WHERE invite_link=?",
            (invite_url,)
        )

    if OWNER_ID:
        kind = "\U0001f393 DEMO" if link["link_type"] == "demo" else "\U0001f48e PERMANENT"
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"{kind} <b>MEMBER JOINED</b>\n\n"
                f"\U0001f464 Added by: <b>{esc(link['creator_name'])}</b>\n"
                f"\U0001f4da Course: <b>{esc(link['course_name'])}</b>\n"
                f"\U0001f464 Member: <b>{esc(user_name(u))}</b>\n"
                f"\U0001f194 ID: <code>{u.id}</code>\n"
                f"\U0001f550 Joined: <b>{local_time(iso(joined))}</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


async def demo_job(context: ContextTypes.DEFAULT_TYPE):
    current = now()

    with db() as c:
        rows = c.execute(
            """
            SELECT * FROM members
            WHERE link_type='demo'
              AND status='active'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (iso(current),)
        ).fetchall()

    for r in rows:
        try:
            await context.bot.ban_chat_member(
                chat_id=int(r["chat_id"]),
                user_id=int(r["user_id"])
            )
            try:
                await context.bot.unban_chat_member(
                    chat_id=int(r["chat_id"]),
                    user_id=int(r["user_id"]),
                    only_if_banned=True
                )
            except Exception:
                pass

            with db() as c:
                c.execute(
                    """
                    UPDATE members
                    SET status='removed', removed_at=?
                    WHERE id=?
                    """,
                    (iso(current), r["id"])
                )
        except Exception as e:
            log.error("demo remove: %s", e)


# ============================================================
# REPORT ENGINE
# ============================================================
def member_line(r, n):
    kind = "\U0001f393 DEMO" if r["link_type"] == "demo" else "\U0001f48e PERMANENT"
    status = {
        "active": "\U0001f7e2 Active",
        "removed": "\U0001f6ab Removed",
    }.get(r["status"], esc(r["status"]))

    username = f"@{esc(r['username'])}" if r["username"] else "No username"

    return (
        f"<b>#{n} {kind}</b>\n"
        f"\U0001f464 Member: <b>{esc(r['full_name'])}</b>\n"
        f"\U0001f517 Username: <b>{username}</b>\n"
        f"\U0001f194 ID: <code>{r['user_id']}</code>\n"
        f"\U0001f464 Added by: <b>{esc(r['creator_name'])}</b>\n"
        f"\U0001f4da Course: <b>{esc(r['course_name'])}</b>\n"
        f"\U0001f550 Joined: <b>{local_time(r['joined_at'])}</b>\n"
        f"\U0001f4cc Status: <b>{status}</b>\n"
    )


def build_report(start_utc, end_utc, title, report_key):
    start_iso = iso(start_utc)
    end_iso = iso(end_utc)

    with db() as c:
        links = c.execute(
            """
            SELECT * FROM links
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at
            """,
            (start_iso, end_iso)
        ).fetchall()

        members = c.execute(
            """
            SELECT * FROM members
            WHERE joined_at >= ? AND joined_at < ?
            ORDER BY joined_at
            """,
            (start_iso, end_iso)
        ).fetchall()

    demo_members = [r for r in members if r["link_type"] == "demo"]
    perm_members = [r for r in members if r["link_type"] == "perm"]
    demo_links = [r for r in links if r["link_type"] == "demo"]
    perm_links = [r for r in links if r["link_type"] == "perm"]

    # IMPORTANT: Header is created EXACTLY ONCE.
    text = (
        f"\U0001f4ca <b>{title}</b>\n"
        f"\U0001f4c5 <b>{start_utc.astimezone(IST).strftime('%d/%m/%Y %H:%M:%S')}</b>"
        " \u2192 "
        f"<b>{end_utc.astimezone(IST).strftime('%d/%m/%Y %H:%M:%S')} IST</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        "\U0001f517 <b>LINK ACTIVITY</b>\n"
        f"\U0001f393 Demo links created: <b>{len(demo_links)}</b>\n"
        f"\U0001f48e Permanent links created: <b>{len(perm_links)}</b>\n"
        f"\U0001f517 Total links: <b>{len(links)}</b>\n\n"
    )

    if links:
        text += "\U0001f4cb <b>GENERATED LINKS</b>\n\n"
        for i, r in enumerate(links, 1):
            kind = "\U0001f393 DEMO" if r["link_type"] == "demo" else "\U0001f48e PERMANENT"
            used = "\u2705 Used" if r["used"] else "\u26aa Unused"
            text += (
                f"#{i} {kind}\n"
                f"\U0001f464 Seller/Admin: <b>{esc(r['creator_name'])}</b>\n"
                f"\U0001f4da Course: <b>{esc(r['course_name'])}</b>\n"
                f"\U0001f550 Created: <b>{local_time(r['created_at'])}</b>\n"
                f"\U0001f4cc Status: <b>{used}</b>\n\n"
            )
    else:
        text += "\U0001f4cb <b>GENERATED LINKS</b>\n\u274c None\n\n"

    text += (
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        "\U0001f465 <b>MEMBER JOIN ACTIVITY</b>\n"
        f"\U0001f4ca Total members: <b>{len(members)}</b>\n"
        f"\U0001f393 Demo: <b>{len(demo_members)}</b>\n"
        f"\U0001f48e Permanent: <b>{len(perm_members)}</b>\n\n"
    )

    if members:
        for i, r in enumerate(members, 1):
            text += member_line(r, i) + "\n"
    else:
        text += "\u274c No members joined in this period.\n"

    text += (
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4ca <b>SUMMARY</b>\n"
        f"\U0001f393 Demo members: <b>{len(demo_members)}</b>\n"
        f"\U0001f48e Permanent members: <b>{len(perm_members)}</b>\n"
        f"\U0001f465 Total: <b>{len(members)}</b>"
    )
    return text


def split_report(text, limit=3900):
    """
    Split WITHOUT repeating the report header.
    Only the first chunk contains the header; following chunks say CONTINUED.
    """
    if len(text) <= limit:
        return [text]

    lines = text.splitlines(keepends=True)
    chunks = []
    current = ""

    for line in lines:
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line

    if current:
        chunks.append(current)

    if len(chunks) > 1:
        for i in range(1, len(chunks)):
            prefix = "\U0001f4ca <b>REPORT CONTINUED</b>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            chunks[i] = prefix + chunks[i]

    return chunks


async def send_report_once(bot, report_key, text):
    """
    Send report exactly once per key.
    DB UNIQUE(report_key) prevents duplicate sends caused by repeated
    callbacks or multiple workers.
    """
    with db() as c:
        exists = c.execute(
            "SELECT 1 FROM reports_sent WHERE report_key=?",
            (report_key,)
        ).fetchone()
        if exists:
            return False

        # Reserve BEFORE sending. This prevents concurrent callbacks.
        c.execute(
            "INSERT INTO reports_sent(report_key,message_id,sent_at) VALUES(?,?,?)",
            (report_key, 0, iso(now()))
        )
        c.commit()

    chunks = split_report(text)
    first_id = None

    try:
        for chunk in chunks:
            msg = await bot.send_message(
                OWNER_ID,
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            if first_id is None:
                first_id = msg.message_id
            with db() as c:
                c.execute(
                    """
                    INSERT INTO report_messages(report_key,message_id,created_at)
                    VALUES(?,?,?)
                    """,
                    (report_key, msg.message_id, iso(now()))
                )
                c.commit()

        with db() as c:
            c.execute(
                "UPDATE reports_sent SET message_id=? WHERE report_key=?",
                (first_id or 0, report_key)
            )
            c.commit()
        return True

    except Exception:
        # Allow retry if Telegram send failed.
        with db() as c:
            c.execute(
                "DELETE FROM reports_sent WHERE report_key=?",
                (report_key,)
            )
            c.commit()
        raise


def one_hour_range():
    end = now()
    start = end - timedelta(hours=1)
    return start, end


def yesterday_range():
    today = ist_now().date()
    y = today - timedelta(days=1)
    start_local = datetime(y.year, y.month, y.day, 0, 0, 0, tzinfo=IST)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), y.isoformat()


async def report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    if not owner(q.from_user.id):
        await q.answer("\u274c OWNER ONLY", show_alert=True)
        return

    await q.answer()

    text = (
        "\U0001f4ca <b>REPORT CENTER</b>\n\n"
        "\U0001f553 <b>LAST 1 HOUR</b>\n"
        "<i>Last 60 minutes ki complete activity.</i>\n\n"
        "\U0001f4c5 <b>LAST 24 HOURS</b>\n"
        "<i>Previous completed day ki detailed activity.</i>\n\n"
        "\u2501" * 18 + "\n\n"
        "\U0001f465 <b>Member</b> \u2022 "
        "\U0001f464 <b>Seller</b> \u2022 "
        "\U0001f4da <b>Course</b> \u2022 "
        "\U0001f393 <b>Access Type</b> \u2022 "
        "\U0001f552 <b>Exact Time</b>"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "\U0001f553 LAST 1 HOUR",
                callback_data="rpt:hour"
            ),
            InlineKeyboardButton(
                "\U0001f4c5 LAST 24 HOURS",
                callback_data="rpt:day"
            )
        ],
        [
            InlineKeyboardButton(
                "\u2b05\ufe0f OWNER PANEL",
                callback_data="owner"
            )
        ]
    ])

    await safe_edit(q, text, kb)

async def hourly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return
    await q.answer("Generating 1-hour report\u2026")

    start, end = one_hour_range()
    # Round key to the exact minute, so repeated button taps in the same minute
    # produce the same key and are rejected.
    end_key = end.astimezone(IST).strftime("%Y%m%d%H%M")
    key = f"hour:{end_key}"

    text = build_report(
        start,
        end,
        "\U0001f4ca \u23f1 LAST 1 HOUR REPORT",
        key
    )

    # Display report by EDITING the existing button message.
    # This avoids creating another report message for the first chunk.
    chunks = split_report(text)
    await safe_edit(q, chunks[0])

    # Reserve this report before sending continuation chunks.
    with db() as c:
        exists = c.execute(
            "SELECT 1 FROM reports_sent WHERE report_key=?",
            (key,)
        ).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO reports_sent(report_key,message_id,sent_at) VALUES(?,?,?)",
                (key, q.message.message_id, iso(now()))
            )
            c.commit()
            reserved = True
        else:
            reserved = False

    if not reserved:
        return

    try:
        with db() as c:
            c.execute(
                "INSERT INTO report_messages(report_key,message_id,created_at) VALUES(?,?,?)",
                (key, q.message.message_id, iso(now()))
            )
            c.commit()

        for chunk in chunks[1:]:
            msg = await context.bot.send_message(
                OWNER_ID,
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            with db() as c:
                c.execute(
                    "INSERT INTO report_messages(report_key,message_id,created_at) VALUES(?,?,?)",
                    (key, msg.message_id, iso(now()))
                )
                c.commit()
    except Exception:
        log.exception("hourly report")
        with db() as c:
            c.execute(
                "DELETE FROM reports_sent WHERE report_key=?",
                (key,)
            )
            c.commit()


async def daily_report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return
    await q.answer("Generating daily report\u2026")

    start, end, date_key = yesterday_range()
    key = f"day:{date_key}"

    text = build_report(
        start,
        end,
        "\U0001f4ca \U0001f4c5 24 HOURS / DAILY REPORT",
        key
    )

    chunks = split_report(text)
    await safe_edit(q, chunks[0])

    with db() as c:
        exists = c.execute(
            "SELECT 1 FROM reports_sent WHERE report_key=?",
            (key,)
        ).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO reports_sent(report_key,message_id,sent_at) VALUES(?,?,?)",
                (key, q.message.message_id, iso(now()))
            )
            c.commit()
            reserved = True
        else:
            reserved = False

    if not reserved:
        return

    try:
        with db() as c:
            c.execute(
                "INSERT INTO report_messages(report_key,message_id,created_at) VALUES(?,?,?)",
                (key, q.message.message_id, iso(now()))
            )
            c.commit()

        for chunk in chunks[1:]:
            msg = await context.bot.send_message(
                OWNER_ID,
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            with db() as c:
                c.execute(
                    "INSERT INTO report_messages(report_key,message_id,created_at) VALUES(?,?,?)",
                    (key, msg.message_id, iso(now()))
                )
                c.commit()
    except Exception:
        log.exception("daily report")
        with db() as c:
            c.execute(
                "DELETE FROM reports_sent WHERE report_key=?",
                (key,)
            )
            c.commit()


async def automatic_daily_report(context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID:
        return

    local = ist_now()
    if local.hour != 0 or local.minute < 5:
        return

    start, end, date_key = yesterday_range()
    key = f"auto-day:{date_key}"

    text = build_report(
        start,
        end,
        "\U0001f4ca \U0001f4c5 AUTOMATIC DAILY REPORT",
        key
    )

    try:
        await send_report_once(context.bot, key, text)
    except Exception:
        log.exception("automatic daily report")


async def clear_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return

    with db() as c:
        rows = c.execute(
            "SELECT message_id FROM report_messages ORDER BY id"
        ).fetchall()

    deleted = 0
    for r in rows:
        try:
            await context.bot.delete_message(
                OWNER_ID,
                int(r["message_id"])
            )
            deleted += 1
        except Exception:
            pass

    with db() as c:
        c.execute("DELETE FROM report_messages")
        c.execute("DELETE FROM reports_sent")
        c.commit()

    await update.message.reply_text(
        f"\u2705 Tracked report messages cleared: {deleted}\n"
        "Report keys reset. New reports can be generated again."
    )


async def dailyreport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return
    # Command shows yesterday directly.
    start, end, date_key = yesterday_range()
    text = build_report(
        start, end, "\U0001f4ca \U0001f4c5 24 HOURS / DAILY REPORT", f"manual-day:{date_key}"
    )
    chunks = split_report(text)
    for chunk in chunks:
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )


async def hourly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return
    start, end = one_hour_range()
    text = build_report(
        start, end, "\U0001f4ca \u23f1 LAST 1 HOUR REPORT", "manual-hour"
    )
    for chunk in split_report(text):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )


# ============================================================
# OWNER PANEL / ADMIN
# ============================================================
async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    if not owner(q.from_user.id):
        await q.answer("\u274c OWNER ONLY", show_alert=True)
        return

    await q.answer()

    with db() as c:
        admins = c.execute("SELECT * FROM admins").fetchall()
        channels = c.execute("SELECT * FROM channels").fetchall()
        courses = c.execute("SELECT * FROM courses").fetchall()

    text = (
        "\U0001f451 <b>OWNER CONTROL PANEL</b>\n\n"
        "\U0001f4ca <b>DASHBOARD</b>\n"
        "\U0001f465 Active Admins: <b>" + str(len(admins)) + "</b>\n"
        "\U0001f4da Courses: <b>" + str(len(courses)) + "</b>\n"
        "\U0001f4e1 Detected Channels: <b>" + str(len(channels)) + "</b>\n"
        "\u23f1\ufe0f Demo Duration: <b>" + str(demo_minutes()) + " Minutes</b>\n\n"
        "\u2501" * 18 + "\n\n"
        "\u2728 <i>Seller, access & report management</i>"
    )

    kb = [
        [
            InlineKeyboardButton(
                "\U0001f465 VIEW ADMINS",
                callback_data="editadmins"
            ),
            InlineKeyboardButton(
                "\u2795 ADD ADMIN",
                callback_data="addadmin"
            )
        ],
        [
            InlineKeyboardButton(
                "\u23f1\ufe0f DEMO TIME",
                callback_data="showtime"
            ),
            InlineKeyboardButton(
                "\U0001f4ca DAILY REPORT",
                callback_data="reports"
            )
        ],
        [
            InlineKeyboardButton(
                "\u2b05\ufe0f BACK TO HOME",
                callback_data="home"
            )
        ]
    ]

    await safe_edit(q, text, InlineKeyboardMarkup(kb))

async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return ConversationHandler.END
    await q.answer()

    await safe_edit(
        q,
        "\u2795 <b>ADD ADMIN</b>\n\n"
        "User ID bhejo:\n<code>123456789</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\u274c Cancel", callback_data="owner")]
        ])
    )
    return ADD_ADMIN


async def add_admin_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid = int(update.message.text.strip())
    except Exception:
        await update.message.reply_text("\u274c Numeric Telegram User ID do.")
        return ADD_ADMIN

    if uid == OWNER_ID:
        await update.message.reply_text("\u274c Owner already exists.")
        return ConversationHandler.END

    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO admins(uid,name,permissions) VALUES(?,?,?)",
            (uid, f"Admin {uid}", "demo")
        )

    await update.message.reply_text(
        f"\u2705 Admin added: <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "\U0001f393 Demo Only", callback_data=f"access:demo:{uid}"
                ),
                InlineKeyboardButton(
                    "\U0001f393+\U0001f48e Both", callback_data=f"access:both:{uid}"
                )
            ]
        ])
    )
    return ConversationHandler.END


async def set_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return
    await q.answer()

    _, access, uid_s = q.data.split(":")
    uid = int(uid_s)
    permissions = "demo" if access == "demo" else "demo,perm"

    with db() as c:
        c.execute(
            "UPDATE admins SET permissions=? WHERE uid=?",
            (permissions, uid)
        )

    await safe_edit(
        q,
        f"\u2705 <b>ADMIN ACCESS UPDATED</b>\n\n"
        f"\U0001f194 <code>{uid}</code>\n"
        f"\U0001f510 <b>{'Demo Only' if access == 'demo' else 'Demo + Permanent'}</b>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b05\ufe0f Owner Panel", callback_data="owner")]
        ])
    )


async def edit_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return
    await q.answer()

    with db() as c:
        rows = c.execute(
            "SELECT uid,name,permissions FROM admins ORDER BY uid"
        ).fetchall()

    kb = []
    for r in rows:
        if r["uid"] == OWNER_ID:
            continue
        kb.append([
            InlineKeyboardButton(
                f"{r['uid']} \u2014 {r['permissions']}",
                callback_data=f"chooseaccess:{r['uid']}"
            )
        ])
    kb.append([InlineKeyboardButton("\u2b05\ufe0f Owner", callback_data="owner")])
    await safe_edit(q, "\U0001f465 <b>ADMINS</b>", InlineKeyboardMarkup(kb))


async def choose_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return
    await q.answer()
    uid = int(q.data.split(":")[1])

    await safe_edit(
        q,
        f"\U0001f194 Admin: <code>{uid}</code>\n\nSelect admin access:",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "\U0001f393 Demo Only", callback_data=f"access:demo:{uid}"
                ),
                InlineKeyboardButton(
                    "\U0001f393+\U0001f48e Both", callback_data=f"access:both:{uid}"
                )
            ],
            [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="editadmins")]
        ])
    )


async def show_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return
    await q.answer()

    await safe_edit(
        q,
        f"\u23f1 <b>Demo Time: {demo_minutes()} minutes</b>\n\n"
        "Command:\n<code>/demotime 5</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b05\ufe0f Owner Panel", callback_data="owner")]
        ])
    )


async def demotime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            f"Current Demo Time: {demo_minutes()} minutes"
        )
        return
    try:
        n = int(context.args[0])
        if n < 1 or n > 1440:
            raise ValueError
        with db() as c:
            c.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('demo_minutes',?)",
                (str(n),)
            )
        await update.message.reply_text(f"\u2705 Demo time = {n} minutes.")
    except Exception:
        await update.message.reply_text("\u274c 1\u20131440 minutes ke beech number do.")


# ============================================================
# CATEGORY / COURSE CREATION (OWNER)
# ============================================================
async def add_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return ConversationHandler.END
    await q.answer()

    await safe_edit(
        q,
        "\u2795 <b>NEW CATEGORY</b>\n\n"
        "Format:\n<code>\U0001f4da | Category Name</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\u274c Cancel", callback_data="owner")]
        ])
    )
    return ADD_CATEGORY


async def add_category_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return ConversationHandler.END

    p = [x.strip() for x in update.message.text.split("|", 1)]
    if len(p) != 2:
        await update.message.reply_text("\u274c Format: \U0001f4da | Category Name")
        return ADD_CATEGORY

    try:
        with db() as c:
            c.execute(
                "INSERT INTO categories(emoji,name) VALUES(?,?)",
                (p[0], p[1])
            )
        await update.message.reply_text("\u2705 Category added.")
        return ConversationHandler.END
    except sqlite3.IntegrityError:
        await update.message.reply_text("\u274c Category already exists.")
        return ADD_CATEGORY


# ============================================================
# CHANNEL CHECK
# ============================================================
async def checkchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Use: /checkchannel -1001234567890"
        )
        return

    try:
        chat_id = int(context.args[0])
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        chat = await context.bot.get_chat(chat_id)

        await update.message.reply_text(
            "\U0001f50e <b>CHANNEL CHECK</b>\n\n"
            f"\U0001f4da <b>{esc(chat.title)}</b>\n"
            f"\U0001f194 <code>{chat_id}</code>\n"
            f"\U0001f916 Status: <b>{esc(member.status)}</b>\n"
            f"\U0001f517 Invite/Add Subscribers: <b>{getattr(member,'can_invite_users',None)}</b>\n"
            f"\U0001f6ab Ban Users: <b>{getattr(member,'can_restrict_members',None)}</b>\n\n"
            "Permanent: Invite permission ON.\n"
            "Demo: Invite + Ban Users ON.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(
            f"\u274c Check failed:\n{e}"
        )


# ============================================================
# INLINE SEARCH
# ============================================================
async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip().lower()

    with db() as c:
        rows = c.execute(
            """
            SELECT courses.id,courses.name,categories.emoji,categories.name AS cat
            FROM courses
            JOIN categories ON categories.id=courses.category_id
            WHERE lower(courses.name) LIKE ?
            ORDER BY courses.name
            LIMIT 50
            """,
            (f"%{query}%",)
        ).fetchall()

    results = []
    for r in rows:
        results.append(
            InlineQueryResultArticle(
                id=f"course_{r['id']}",
                title=f"{r['emoji']} {r['name']}",
                description=f"{r['cat']} \u2022 Course",
                input_message_content=InputTextMessageContent(
                    f"{r['emoji']} <b>{esc(r['name'])}</b>\n\n"
                    "Course selected.",
                    parse_mode=ParseMode.HTML
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "\U0001f4da Open Course",
                        callback_data=f"course:{r['id']}"
                    )]
                ])
            )
        )

    await update.inline_query.answer(
        results,
        cache_time=0,
        is_personal=True
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data or ""
    if q.id and not claim_event(f"callback:{q.id}"):
        return

    if d == "home":
        await start(update, context)
    elif d == "owner":
        await owner_panel(update, context)
    elif d == "reports":
        await report_menu(update, context)
    elif d == "rpt:hour":
        await hourly_report(update, context)
    elif d == "rpt:day":
        await daily_report_button(update, context)
    elif d.startswith("cat:"):
        await category(update, context)
    elif d.startswith("course:"):
        await course(update, context)
    elif d.startswith("link:"):
        await create_link(update, context)
    elif d == "channels":
        await channels_page(update, context)
    elif d.startswith("detected:"):
        await detected_channel(update, context)
    elif d == "editadmins":
        await edit_admins(update, context)
    elif d.startswith("chooseaccess:"):
        await choose_access(update, context)
    elif d.startswith("access:"):
        await set_access(update, context)
    elif d == "showtime":
        await show_time(update, context)
    elif d == "addadmin":
        await q.answer()
    elif d == "addcat":
        await add_category_start(update, context)
    else:
        await q.answer()


# ============================================================
# MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing.")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID missing.")

    acquire_single_instance_lock()
    log.info(
        "Single-instance lock acquired | pid=%s | host=%s",
        os.getpid(),
        socket.gethostname()
    )

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(add_admin_start, pattern=r"^addadmin$")
            ],
            states={
                ADD_ADMIN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_save)
                ]
            },
            fallbacks=[]
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(add_category_start, pattern=r"^addcat$")
            ],
            states={
                ADD_CATEGORY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_category_save)
                ]
            },
            fallbacks=[]
        )
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("demotime", demotime))
    app.add_handler(CommandHandler("checkchannel", checkchannel))
    app.add_handler(CommandHandler("dailyreport", dailyreport_command))
    app.add_handler(CommandHandler("hourlyreport", hourly_command))
    app.add_handler(CommandHandler("clearreports", clear_reports))
    app.add_handler(InlineQueryHandler(inline_search))

    # Bot promotion/admin detection.
    app.add_handler(
        ChatMemberHandler(
            bot_chat_member_update,
            ChatMemberHandler.MY_CHAT_MEMBER
        )
    )

    # Real member joins through our invite links.
    app.add_handler(
        ChatMemberHandler(
            chat_member_update,
            ChatMemberHandler.CHAT_MEMBER
        )
    )

    app.add_handler(CallbackQueryHandler(callbacks))

    app.job_queue.run_repeating(
        demo_job,
        interval=15,
        first=10
    )

    app.job_queue.run_repeating(
        automatic_daily_report,
        interval=60,
        first=20
    )

    log.info("RAJ COURSE BOT started ONCE. DB=%s | Duplicate protection=ON", DB_PATH)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
