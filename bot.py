# ============================================================
# PREMIUM COURSE BOT
# Telegram + python-telegram-bot + SQLite
# ============================================================
# Railway Variables required:
#   BOT_TOKEN = your BotFather token
#   OWNER_ID  = your Telegram numeric user ID
#
# Optional:
#   DEMO_MINUTES = 5
#
# IMPORTANT:
# 1) Add the bot as ADMIN in every course channel.
# 2) Bot needs: Invite Users, Ban Users / Restrict Members.
# 3) For auto-detect, the bot must receive channel admin updates.
# 4) Material links can be added from Owner Panel.
# ============================================================

import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    ChatMemberHandler,
    InlineQueryHandler,
    filters,
)

# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
except ValueError:
    OWNER_ID = 0

try:
    DEFAULT_DEMO_MINUTES = max(1, min(1440, int(os.getenv("DEMO_MINUTES", "5"))))
except ValueError:
    DEFAULT_DEMO_MINUTES = 5

DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("premium-course-bot")

# -----------------------------
# EMOJIS
# -----------------------------
E_BOOK = "\U0001F4DA"
E_CROWN = "\U0001F451"
E_REPORT = "\U0001F4CA"
E_SEARCH = "\U0001F50E"
E_CHANNEL = "\U0001F4E2"
E_FOLDER = "\U0001F4C2"
E_LINK = "\U0001F517"
E_DEMO = "\u26A1"
E_PERM = "\U0001F48E"
E_CHECK = "\u2705"
E_CROSS = "\u274C"
E_BACK = "\u2B05\uFE0F"
E_PLUS = "\u2795"
E_EDIT = "\u270F\uFE0F"
E_DELETE = "\U0001F5D1\uFE0F"
E_USER = "\U0001F464"
E_USERS = "\U0001F465"
E_CLOCK = "\u23F0"
E_LOCK = "\U0001F512"
E_GEAR = "\u2699\uFE0F"
E_SPARK = "\u2728"
E_WARNING = "\u26A0\uFE0F"
E_PIN = "\U0001F4CC"
E_MATERIAL = "\U0001F4C2"
E_JOIN = "\U0001F4E5"
E_BOOKMARK = "\U0001F516"
E_STAR = "\U0001F31F"
E_HEART = "\U0001F495"

# Conversation states
ADD_ADMIN, EDIT_ADMIN, REMOVE_ADMIN = range(3)
ADD_CATEGORY, ADD_CHANNEL, EDIT_CHANNEL = range(3, 6)

# ============================================================
# DATABASE
# ============================================================

def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect_db() as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS admins (
                uid INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                permissions TEXT NOT NULL DEFAULT 'demo,perm'
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emoji TEXT NOT NULL DEFAULT 'ð',
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                tg_id TEXT NOT NULL UNIQUE,
                material_url TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_uid INTEGER NOT NULL,
                admin_name TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                link_type TEXT NOT NULL,
                link_url TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS demo_links (
                invite_link TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS demo_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                member_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                joined_at TEXT NOT NULL,
                remove_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS user_menu (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        # Safe migration for an older bot.db.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(channels)").fetchall()}
        if "material_url" not in cols:
            con.execute("ALTER TABLE channels ADD COLUMN material_url TEXT NOT NULL DEFAULT ''")

        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('demo_minutes',?)",
            (str(DEFAULT_DEMO_MINUTES),),
        )

        if OWNER_ID:
            con.execute(
                """
                INSERT INTO admins(uid,name,permissions)
                VALUES(?,?,?)
                ON CONFLICT(uid) DO UPDATE SET name=excluded.name, permissions='all'
                """,
                (OWNER_ID, "Owner", "all"),
            )

        defaults = [
            ("ð", "Teaching Exams"),
            ("ð©", "RAS / PSI"),
            ("ð", "EO-RO / BSTC / CET"),
            ("ð", "NET / JRF"),
            ("â¨", "Other Exams"),
        ]
        for emoji, name in defaults:
            con.execute(
                "INSERT OR IGNORE INTO categories(emoji,name) VALUES(?,?)",
                (emoji, name),
            )

        con.commit()


def get_setting(key, default=""):
    with connect_db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect_db() as con:
        con.execute(
            """
            INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(value)),
        )
        con.commit()


def get_demo_minutes():
    try:
        return max(1, min(1440, int(get_setting("demo_minutes", DEFAULT_DEMO_MINUTES))))
    except ValueError:
        return DEFAULT_DEMO_MINUTES


def repair_text(value):
    """Repair common old UTF-8 mojibake without touching valid Hindi/emoji."""
    if not value:
        return value

    s = str(value)
    bad_markers = ("Ã", "Ã", "Ã¢", "Ã°", "Ã¯Â¿Â½", "Ã Â¤")
    for _ in range(3):
        if not any(x in s for x in bad_markers):
            break
        changed = False
        for enc in ("latin1", "cp1252"):
            try:
                fixed = s.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if fixed != s and sum(s.count(x) for x in bad_markers) > sum(
                fixed.count(x) for x in bad_markers
            ):
                s = fixed
                changed = True
                break
        if not changed:
            break
    return s


# ============================================================
# ACCESS
# ============================================================

def is_owner(uid):
    return OWNER_ID != 0 and int(uid) == OWNER_ID


def get_permissions(uid):
    if is_owner(uid):
        return {"all"}

    with connect_db() as con:
        row = con.execute(
            "SELECT permissions FROM admins WHERE uid=?",
            (uid,),
        ).fetchone()

    if not row:
        return set()

    return {
        x.strip().lower()
        for x in row["permissions"].replace(" ", "").split(",")
        if x.strip()
    }


def is_admin(uid):
    return is_owner(uid) or bool(get_permissions(uid))


def has_permission(uid, permission):
    perms = get_permissions(uid)
    if "all" in perms:
        return True
    if permission == "perm":
        return "perm" in perms or "permanent" in perms or "both" in perms
    return permission in perms


# ============================================================
# UI HELPERS
# ============================================================

def safe(value):
    return html.escape(repair_text(value or ""))


def btn(text, callback):
    return InlineKeyboardButton(text, callback_data=callback)


def home_keyboard(uid):
    rows = [
        [btn(f"{E_SEARCH} Course Search", "search_help")],
    ]

    if is_owner(uid):
        rows.append([
            btn(f"{E_CROWN} Owner Panel", "owner"),
            btn(f"{E_REPORT} Daily Report", "report"),
        ])

    return InlineKeyboardMarkup(rows)


def premium_home_text():
    return (
        f"{E_SPARK} <b>WELCOME TO PREMIUM ACCESS CENTER</b> {E_SPARK}\n"
        f"<b>ââââââââââââââââââââ</b>\n\n"
        f"{E_BOOK} <b>COURSE ACCESS SYSTEM</b>\n"
        f"Fast â¢ Clean â¢ Secure â¢ Professional\n\n"
        f"{E_SEARCH} <b>COURSE SEARCH</b>\n"
        f"Telegram ke kisi bhi chat me likho:\n"
        f"<code>@YourBotUsername course-name</code>\n\n"
        f"{E_CHECK} <b>Search result me apna course select karo.</b>\n\n"
        f"{E_CHANNEL} <b>CHANNEL AUTO-DETECT</b>\n"
        f"Bot ko channel me <b>Administrator</b> banao â "
        f"channel automatically detect ho jayega.\n\n"
        f"{E_LOCK} <i>Fast â¢ Clean â¢ Secure Access System</i>"
    )


async def save_menu_message(user_id, chat_id, message_id):
    with connect_db() as con:
        con.execute(
            """
            INSERT INTO user_menu(user_id,chat_id,message_id)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                message_id=excluded.message_id
            """,
            (user_id, chat_id, message_id),
        )
        con.commit()


async def show_home_message(message, uid, edit=False):
    text = premium_home_text()
    markup = home_keyboard(uid)

    if edit:
        await message.edit_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
        return message

    sent = await message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )
    await save_menu_message(uid, sent.chat_id, sent.message_id)
    return sent


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    # Unauthorized users receive nothing.
    if not is_admin(user.id):
        return

    args = context.args or []
    if args:
        payload = args[0]
        if payload.startswith("channel_"):
            try:
                cid = int(payload.split("_", 1)[1])
                await send_channel_panel(message, user.id, cid)
            except ValueError:
                pass
            return

    # Avoid creating endless duplicate menus from repeated /start.
    with connect_db() as con:
        old = con.execute(
            "SELECT chat_id,message_id FROM user_menu WHERE user_id=?",
            (user.id,),
        ).fetchone()

    if old and old["chat_id"] == message.chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=old["message_id"],
                text=premium_home_text(),
                reply_markup=home_keyboard(user.id),
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass

    await show_home_message(message, user.id)


async def search_help(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer()
        return
    await q.answer()
    await q.edit_message_text(
        f"{E_SEARCH} <b>COURSE SEARCH</b>\n\n"
        f"Telegram ke kisi bhi chat me type karo:\n\n"
        f"<code>@YourBotUsername course-name</code>\n\n"
        f"{E_CHECK} Search result par course tap karo.\n"
        f"{E_LINK} Uske baad <b>Demo</b> ya <b>Permanent</b> link generate karo.",
        reply_markup=InlineKeyboardMarkup([
            [btn(f"{E_BACK} Home", "home")]
        ]),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# CATEGORY / CHANNEL PANELS
# ============================================================

async def category_panel_markup(uid, category_id):
    with connect_db() as con:
        rows = con.execute(
            """
            SELECT id,name FROM channels
            WHERE category_id=?
            ORDER BY name COLLATE NOCASE
            """,
            (category_id,),
        ).fetchall()
        cat = con.execute(
            "SELECT emoji,name FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()

    if not cat:
        return None, None

    keys = [
        [btn(
            f"{E_BOOK} {repair_text(r['name'])}",
            f"channel_{r['id']}"
        )]
        for r in rows
    ]

    if is_owner(uid):
        keys.append([btn(f"{E_PLUS} Add Channel", f"add_channel_{category_id}")])

    keys.append([btn(f"{E_BACK} Owner Panel", "owner")])
    return InlineKeyboardMarkup(keys), cat


async def send_category_panel(message, uid, category_id, edit=False):
    if not is_admin(uid):
        return

    markup, cat = await category_panel_markup(uid, category_id)
    if not cat:
        if edit:
            await message.edit_text("Category not found.")
        else:
            await message.reply_text("Category not found.")
        return

    with connect_db() as con:
        count = con.execute(
            "SELECT COUNT(*) AS n FROM channels WHERE category_id=?",
            (category_id,),
        ).fetchone()["n"]

    text = (
        f"{repair_text(cat['emoji'])} <b>{safe(cat['name'])}</b>\n"
        f"<b>ââââââââââââââââââââ</b>\n\n"
        f"{E_CHANNEL} <b>Channels:</b> {count}\n\n"
        f"Select a course below:"
    )

    if edit:
        await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def send_channel_panel(message, uid, channel_id, edit=False):
    if not is_admin(uid):
        return

    with connect_db() as con:
        ch = con.execute(
            """
            SELECT c.*, cat.emoji AS cat_emoji, cat.name AS cat_name
            FROM channels c
            JOIN categories cat ON cat.id=c.category_id
            WHERE c.id=?
            """,
            (channel_id,),
        ).fetchone()

    if not ch:
        if edit:
            await message.edit_text("Course not found.")
        else:
            await message.reply_text("Course not found.")
        return

    rows = []

    if has_permission(uid, "demo"):
        rows.append(btn(f"{E_DEMO} Demo Link", f"gen_demo_{channel_id}"))

    if has_permission(uid, "perm"):
        rows.append(btn(f"{E_PERM} Permanent Link", f"gen_perm_{channel_id}"))

    keyboard = []
    if rows:
        keyboard.append(rows)

    if is_owner(uid):
        keyboard.append([
            btn(f"{E_EDIT} Edit Course", f"edit_channel_{channel_id}"),
            btn(f"{E_DELETE} Delete", f"delete_channel_{channel_id}"),
        ])

    keyboard.append([btn(f"{E_BACK} Back", f"category_{ch['category_id']}")])

    material_state = (
        f"{E_CHECK} <b>Course Material:</b> Ready"
        if ch["material_url"]
        else f"{E_WARNING} <b>Course Material:</b> Not added"
    )

    text = (
        f"{repair_text(ch['cat_emoji'])} <b>{safe(ch['name'])}</b>\n"
        f"<b>ââââââââââââââââââââ</b>\n\n"
        f"{E_CHANNEL} <b>Channel ID:</b> <code>{safe(ch['tg_id'])}</code>\n"
        f"{material_state}\n\n"
        f"{E_SPARK} <i>Select the access type below.</i>"
    )

    markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


# ============================================================
# OWNER PANEL
# ============================================================

async def owner_panel(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    with connect_db() as con:
        admins = con.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
        categories = con.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"]
        channels = con.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
        demos = con.execute(
            "SELECT COUNT(*) AS n FROM demo_users WHERE status='active'"
        ).fetchone()["n"]

    text = (
        f"{E_CROWN} <b>OWNER CONTROL CENTER</b>\n"
        f"<b>ââââââââââââââââââââ</b>\n\n"
        f"{E_USERS} <b>Admins:</b> {admins}\n"
        f"{E_FOLDER} <b>Categories:</b> {categories}\n"
        f"{E_CHANNEL} <b>Courses:</b> {channels}\n"
        f"{E_DEMO} <b>Active Demos:</b> {demos}\n"
        f"{E_CLOCK} <b>Demo Time:</b> {get_demo_minutes()} min\n\n"
        f"{E_SPARK} <i>Premium management dashboard</i>"
    )

    keyboard = [
        [
            btn(f"{E_PLUS} Add Admin", "add_admin"),
            btn(f"{E_EDIT} Edit Access", "edit_admin"),
        ],
        [
            btn(f"{E_DELETE} Remove Admin", "remove_admin"),
            btn(f"{E_PLUS} Add Category", "add_category"),
        ],
        [
            btn(f"{E_FOLDER} Manage Courses", "manage_categories"),
            btn(f"{E_USERS} Admin List", "admins"),
        ],
        [
            btn(f"{E_REPORT} Daily Report", "report"),
            btn(f"{E_CLOCK} Demo Time", "demo_time"),
        ],
        [
            btn(f"{E_BACK} Home", "home"),
        ],
    ]

    await q.answer()
    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def manage_categories(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    with connect_db() as con:
        rows = con.execute(
            "SELECT id,emoji,name FROM categories ORDER BY id"
        ).fetchall()

    keyboard = [
        [btn(
            f"{repair_text(r['emoji'])} {repair_text(r['name'])}",
            f"category_{r['id']}"
        )]
        for r in rows
    ]
    keyboard.append([btn(f"{E_PLUS} Add Category", "add_category")])
    keyboard.append([btn(f"{E_BACK} Owner Panel", "owner")])

    await q.answer()
    await q.edit_message_text(
        f"{E_FOLDER} <b>COURSE CATEGORIES</b>\n\n"
        f"Select a category:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def list_admins(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    with connect_db() as con:
        rows = con.execute(
            "SELECT uid,name,permissions FROM admins ORDER BY uid"
        ).fetchall()

    lines = [f"{E_USERS} <b>ADMIN ACCESS LIST</b>\n<b>ââââââââââââââââââââ</b>\n"]
    for r in rows:
        role = "OWNER" if r["uid"] == OWNER_ID else "ADMIN"
        lines.append(
            f"{E_USER} <b>{safe(r['name'])}</b>\n"
            f"ID: <code>{r['uid']}</code>\n"
            f"Access: <b>{safe(r['permissions'])}</b> â¢ {role}\n"
        )

    await q.answer()
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [btn(f"{E_BACK} Owner Panel", "owner")]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def demo_time_panel(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    await q.answer()
    await q.edit_message_text(
        f"{E_CLOCK} <b>DEMO TIME</b>\n\n"
        f"Current: <b>{get_demo_minutes()} minutes</b>\n\n"
        f"Send:\n"
        f"<code>/demotime 5</code>\n"
        f"<code>/demotime 10</code>\n"
        f"<code>/demotime 30</code>",
        reply_markup=InlineKeyboardMarkup([
            [btn(f"{E_BACK} Owner Panel", "owner")]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def demotime_command(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            f"{E_CLOCK} Current Demo Time: <b>{get_demo_minutes()} minutes</b>\n\n"
            f"Example: <code>/demotime 5</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        value = int(args[0])
        if not 1 <= value <= 1440:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            f"{E_CROSS} Demo time must be between 1 and 1440 minutes.",
        )
        return

    set_setting("demo_minutes", value)
    await update.effective_message.reply_text(
        f"{E_CHECK} <b>Demo Time Updated</b>\n\n"
        f"New duration: <b>{value} minutes</b>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ADMIN CONVERSATIONS
# ============================================================

async def add_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{E_PLUS} <b>ADD ADMIN</b>\n"
        f"<b>ââââââââââââââââââââ</b>\n\n"
        f"Send:\n"
        f"<code>123456789 | demo</code>\n\n"
        f"or:\n"
        f"<code>123456789 | both</code>\n\n"
        f"<b>demo</b> = Demo only\n"
        f"<b>both</b> = Demo + Permanent",
        reply_markup=InlineKeyboardMarkup([[btn(f"{E_CROSS} Cancel", "owner")]]),
        parse_mode=ParseMode.HTML,
    )
    return ADD_ADMIN


async def add_admin_save(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid_text, access = [x.strip() for x in update.message.text.split("|", 1)]
        uid = int(uid_text)
    except (ValueError, TypeError):
        await update.message.reply_text(
            "Format: <code>123456789 | demo</code>",
            parse_mode=ParseMode.HTML,
        )
        return ADD_ADMIN

    access = access.lower()
    if access == "demo":
        perms = "demo"
    elif access in ("both", "demo,perm", "perm", "permanent"):
        perms = "demo,perm"
    else:
        await update.message.reply_text(
            f"{E_CROSS} Access must be <b>demo</b> or <b>both</b>.",
            parse_mode=ParseMode.HTML,
        )
        return ADD_ADMIN

    if uid == OWNER_ID:
        await update.message.reply_text("Owner is already the owner.")
        return ConversationHandler.END

    with connect_db() as con:
        con.execute(
            """
            INSERT INTO admins(uid,name,permissions)
            VALUES(?,?,?)
            ON CONFLICT(uid) DO UPDATE SET permissions=excluded.permissions
            """,
            (uid, "Admin", perms),
        )
        con.commit()

    await update.message.reply_text(
        f"{E_CHECK} <b>ADMIN ADDED</b>\n\n"
        f"User ID: <code>{uid}</code>\n"
        f"Access: <b>{safe(perms)}</b>",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def edit_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{E_EDIT} <b>EDIT ADMIN ACCESS</b>\n\n"
        f"Send:\n"
        f"<code>UserID | demo</code>\n"
        f"<code>UserID | both</code>\n"
        f"<code>UserID | demo,perm</code>",
        reply_markup=InlineKeyboardMarkup([[btn(f"{E_CROSS} Cancel", "owner")]]),
        parse_mode=ParseMode.HTML,
    )
    return EDIT_ADMIN


async def edit_admin_save(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid_text, perms = [x.strip() for x in update.message.text.split("|", 1)]
        uid = int(uid_text)
    except (ValueError, TypeError):
        await update.message.reply_text("Invalid format.")
        return EDIT_ADMIN

    perms = perms.lower().replace(" ", "")
    if perms == "both":
        perms = "demo,perm"

    if uid == OWNER_ID:
        await update.message.reply_text("Owner access cannot be edited.")
        return ConversationHandler.END

    with connect_db() as con:
        row = con.execute("SELECT uid FROM admins WHERE uid=?", (uid,)).fetchone()
        if not row:
            await update.message.reply_text("Admin not found.")
            return EDIT_ADMIN
        con.execute("UPDATE admins SET permissions=? WHERE uid=?", (perms, uid))
        con.commit()

    await update.message.reply_text(f"{E_CHECK} Admin access updated.")
    return ConversationHandler.END


async def remove_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{E_DELETE} <b>REMOVE ADMIN</b>\n\n"
        f"Send User ID:",
        reply_markup=InlineKeyboardMarkup([[btn(f"{E_CROSS} Cancel", "owner")]]),
        parse_mode=ParseMode.HTML,
    )
    return REMOVE_ADMIN


async def remove_admin_save(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("User ID must be numeric.")
        return REMOVE_ADMIN

    if uid == OWNER_ID:
        await update.message.reply_text("Owner cannot be removed.")
        return ConversationHandler.END

    with connect_db() as con:
        con.execute("DELETE FROM admins WHERE uid=?", (uid,))
        con.commit()

    await update.message.reply_text(f"{E_CHECK} Admin removed.")
    return ConversationHandler.END


# ============================================================
# CATEGORY / CHANNEL MANAGEMENT
# ============================================================

async def add_category_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{E_PLUS} <b>ADD CATEGORY</b>\n\n"
        f"Send:\n"
        f"<code>ð | UPSC / IAS</code>",
        reply_markup=InlineKeyboardMarkup([[btn(f"{E_CROSS} Cancel", "owner")]]),
        parse_mode=ParseMode.HTML,
    )
    return ADD_CATEGORY


async def add_category_save(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return ConversationHandler.END

    parts = [x.strip() for x in update.message.text.split("|", 1)]
    if len(parts) != 2 or not all(parts):
        await update.message.reply_text("Format: Emoji | Category Name")
        return ADD_CATEGORY

    emoji, name = parts
    with connect_db() as con:
        try:
            con.execute(
                "INSERT INTO categories(emoji,name) VALUES(?,?)",
                (emoji, name),
            )
            con.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text("Category already exists.")
            return ADD_CATEGORY

    await update.message.reply_text(
        f"{E_CHECK} <b>Category Added</b>\n\n{emoji} <b>{safe(name)}</b>",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def add_channel_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    try:
        category_id = int(q.data.split("_")[-1])
    except ValueError:
        await q.answer("Invalid category.", show_alert=True)
        return ConversationHandler.END

    context.user_data["category_id"] = category_id
    await q.answer()
    await q.edit_message_text(
        f"{E_PLUS} <b>ADD COURSE / CHANNEL</b>\n"
        f"<b>ââââââââââââââââââââ</b>\n\n"
        f"Send in ONE message:\n\n"
        f"<code>Course Name | -1001234567890 | Material Link</code>\n\n"
        f"Example:\n"
        f"<code>KALAM CET 12TH 2026 | -1001234567890 | https://t.me/c/123/1</code>\n\n"
        f"{E_WARNING} Bot must already be admin in the channel.",
        reply_markup=InlineKeyboardMarkup([
            [btn(f"{E_CROSS} Cancel", f"category_{category_id}")]
        ]),
        parse_mode=ParseMode.HTML,
    )
    return ADD_CHANNEL


async def add_channel_save(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return ConversationHandler.END

    category_id = context.user_data.get("category_id")
    if not category_id:
        return ConversationHandler.END

    parts = [x.strip() for x in update.message.text.split("|")]
    if len(parts) < 2:
        await update.message.reply_text(
            "Format: Course Name | Channel ID | Material Link"
        )
        return ADD_CHANNEL

    name = parts[0]
    tg_id = parts[1]
    material_url = parts[2] if len(parts) >= 3 else ""

    if not name:
        await update.message.reply_text("Course name cannot be empty.")
        return ADD_CHANNEL

    if not re.fullmatch(r"-100\d+", tg_id):
        await update.message.reply_text(
            "Channel ID must look like -1001234567890"
        )
        return ADD_CHANNEL

    if material_url and not re.match(r"^https?://", material_url, re.I):
        await update.message.reply_text(
            "Material link must start with http:// or https://"
        )
        return ADD_CHANNEL

    try:
        chat = await context.bot.get_chat(int(tg_id))
        if chat.type != "channel":
            await update.message.reply_text("This ID is not a Telegram channel.")
            return ADD_CHANNEL

        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if member.status != ChatMemberStatus.ADMINISTRATOR:
            await update.message.reply_text(
                f"{E_WARNING} Bot is not admin in this channel."
            )
            return ADD_CHANNEL
    except Exception:
        await update.message.reply_text(
            f"{E_CROSS} Bot cannot access this channel. "
            f"Make it administrator first."
        )
        return ADD_CHANNEL

    with connect_db() as con:
        try:
            con.execute(
                """
                INSERT INTO channels(category_id,name,tg_id,material_url)
                VALUES(?,?,?,?)
                """,
                (category_id, name, tg_id, material_url),
            )
            con.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                "This channel ID is already registered."
            )
            return ADD_CHANNEL

    await update.message.reply_text(
        f"{E_CHECK} <b>COURSE ADDED SUCCESSFULLY</b>\n\n"
        f"{E_BOOK} <b>{safe(name)}</b>\n"
        f"{E_CHANNEL} <code>{safe(tg_id)}</code>\n"
        f"{E_MATERIAL} Material: {'Added' if material_url else 'Not added'}",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def edit_channel_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    try:
        channel_id = int(q.data.split("_")[-1])
    except ValueError:
        await q.answer("Invalid course.", show_alert=True)
        return ConversationHandler.END

    context.user_data["edit_channel_id"] = channel_id
    await q.answer()
    await q.edit_message_text(
        f"{E_EDIT} <b>EDIT COURSE</b>\n\n"
        f"Send:\n"
        f"<code>Course Name | Channel ID | Material Link</code>",
        reply_markup=InlineKeyboardMarkup([
            [btn(f"{E_CROSS} Cancel", f"channel_{channel_id}")]
        ]),
        parse_mode=ParseMode.HTML,
    )
    return EDIT_CHANNEL


async def edit_channel_save(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return ConversationHandler.END

    cid = context.user_data.get("edit_channel_id")
    parts = [x.strip() for x in update.message.text.split("|")]

    if len(parts) < 2:
        await update.message.reply_text(
            "Format: Course Name | Channel ID | Material Link"
        )
        return EDIT_CHANNEL

    name, tg_id = parts[0], parts[1]
    material_url = parts[2] if len(parts) >= 3 else ""

    if not name or not re.fullmatch(r"-100\d+", tg_id):
        await update.message.reply_text("Invalid course name or channel ID.")
        return EDIT_CHANNEL

    if material_url and not re.match(r"^https?://", material_url, re.I):
        await update.message.reply_text("Invalid material URL.")
        return EDIT_CHANNEL

    try:
        await context.bot.get_chat(int(tg_id))
    except Exception:
        await update.message.reply_text("Bot cannot access this channel.")
        return EDIT_CHANNEL

    with connect_db() as con:
        try:
            con.execute(
                """
                UPDATE channels
                SET name=?,tg_id=?,material_url=?
                WHERE id=?
                """,
                (name, tg_id, material_url, cid),
            )
            con.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text("Channel ID already exists.")
            return EDIT_CHANNEL

    await update.message.reply_text(f"{E_CHECK} Course updated successfully.")
    return ConversationHandler.END


async def delete_channel(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    try:
        cid = int(q.data.split("_")[-1])
    except ValueError:
        await q.answer("Invalid course.", show_alert=True)
        return

    with connect_db() as con:
        row = con.execute(
            "SELECT category_id,name FROM channels WHERE id=?",
            (cid,),
        ).fetchone()
        if not row:
            await q.answer("Course not found.", show_alert=True)
            return
        con.execute("DELETE FROM channels WHERE id=?", (cid,))
        con.commit()

    await q.answer("Course deleted.")
    # Go back to category without fabricating an Update object.
    with connect_db() as con:
        cat = con.execute(
            "SELECT emoji,name FROM categories WHERE id=?",
            (row["category_id"],),
        ).fetchone()
        courses = con.execute(
            "SELECT id,name FROM channels WHERE category_id=? ORDER BY name COLLATE NOCASE",
            (row["category_id"],),
        ).fetchall()

    keys = [[btn(f"{E_BOOK} {repair_text(c['name'])}", f"channel_{c['id']}")] for c in courses]
    keys.append([btn(f"{E_PLUS} Add Channel", f"add_channel_{row['category_id']}")])
    keys.append([btn(f"{E_BACK} Categories", "manage_categories")])

    await q.edit_message_text(
        f"{repair_text(cat['emoji'])} <b>{safe(cat['name'])}</b>\n\n"
        f"{E_CHECK} Course deleted.\n\n"
        f"Select another course:",
        reply_markup=InlineKeyboardMarkup(keys),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# AUTO-DETECT CHANNEL
# ============================================================

async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm or cm.chat.type != "channel":
        return

    became_admin = cm.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR
    if not became_admin:
        return

    # Default category = Other Exams.
    with connect_db() as con:
        cat = con.execute(
            "SELECT id FROM categories WHERE name=?",
            ("Other Exams",),
        ).fetchone()

        if not cat:
            con.execute(
                "INSERT INTO categories(emoji,name) VALUES(?,?)",
                ("â¨", "Other Exams"),
            )
            con.commit()
            cat = con.execute(
                "SELECT id FROM categories WHERE name=?",
                ("Other Exams",),
            ).fetchone()

        name = cm.chat.title or "Telegram Channel"
        tg_id = str(cm.chat.id)

        con.execute(
            """
            INSERT INTO channels(category_id,name,tg_id,material_url)
            VALUES(?,?,?,?)
            ON CONFLICT(tg_id) DO UPDATE SET
                name=excluded.name
            """,
            (cat["id"], name, tg_id, ""),
        )
        con.commit()

    logger.info("Auto-detected channel: %s (%s)", name, tg_id)

    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"{E_CHECK} <b>CHANNEL AUTO-DETECTED</b>\n"
                f"<b>ââââââââââââââââââââ</b>\n\n"
                f"{E_CHANNEL} <b>{safe(name)}</b>\n"
                f"ID: <code>{safe(tg_id)}</code>\n\n"
                f"{E_WARNING} Material link Owner Panel â Edit Course se add karo.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ============================================================
# LINK GENERATION
# ============================================================

def record_link(uid, channel_name, link_type, link_url):
    with connect_db() as con:
        con.execute(
            """
            INSERT INTO records
            (admin_uid,admin_name,channel_name,link_type,link_url,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                uid,
                "Owner" if is_owner(uid) else f"Admin {uid}",
                channel_name,
                link_type,
                link_url,
                datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
            ),
        )
        con.commit()


async def create_access_link(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    parts = q.data.split("_")
    if len(parts) != 3:
        await q.answer("Invalid request.", show_alert=True)
        return

    link_type = parts[1]
    try:
        cid = int(parts[2])
    except ValueError:
        await q.answer("Invalid course.", show_alert=True)
        return

    if not has_permission(uid, link_type):
        await q.answer("Permission denied.", show_alert=True)
        return

    with connect_db() as con:
        ch = con.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()

    if not ch:
        await q.answer("Course not found.", show_alert=True)
        return

    try:
        chat_id = int(ch["tg_id"])
        now = datetime.now(timezone.utc)

        if link_type == "demo":
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                member_limit=1,
                creates_join_request=False,
                name=f"DEMO-{now.strftime('%Y%m%d-%H%M%S')}",
            )

            with connect_db() as con:
                con.execute(
                    """
                    INSERT OR REPLACE INTO demo_links
                    (invite_link,chat_id,channel_name,created_at,used)
                    VALUES(?,?,?,?,0)
                    """,
                    (
                        invite.invite_link,
                        str(chat_id),
                        ch["name"],
                        now.isoformat(),
                    ),
                )
                con.commit()

            record_link(uid, ch["name"], "DEMO", invite.invite_link)

            text = (
                f"{E_DEMO} <b>DEMO ACCESS GRANTED</b> {E_SPARK}\n"
                f"<b>ââââââââââââââââââââ</b>\n\n"
                f"{E_BOOK} <b>Course:</b> {safe(ch['name'])}\n"
                f"{E_CLOCK} <b>Demo Duration:</b> {get_demo_minutes()} minutes\n\n"
                f"{E_JOIN} <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
                f"{E_LINK} <a href=\"{html.escape(invite.invite_link, quote=True)}\">Open Demo Joining Link</a>\n\n"
                f"{E_MATERIAL} <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤¯à¤¹à¤¾à¤ à¤¦à¥à¤à¥à¤ (View Link):</b>\n"
                f"{E_LINK} <a href=\"{html.escape(ch['material_url'], quote=True)}\">Open Course Material</a>\n"
                if ch["material_url"]
                else
                f"{E_DEMO} <b>DEMO ACCESS GRANTED</b> {E_SPARK}\n"
                f"<b>ââââââââââââââââââââ</b>\n\n"
                f"{E_BOOK} <b>Course:</b> {safe(ch['name'])}\n"
                f"{E_CLOCK} <b>Demo Duration:</b> {get_demo_minutes()} minutes\n\n"
                f"{E_JOIN} <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
                f"{E_LINK} <a href=\"{html.escape(invite.invite_link, quote=True)}\">Open Demo Joining Link</a>\n\n"
                f"{E_MATERIAL} <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤²:</b> Owner Panel à¤¸à¥ material link add à¤à¤°à¥à¤."
            )

            keyboard = [
                [InlineKeyboardButton(
                    f"{E_JOIN} Joining Link",
                    url=invite.invite_link
                )]
            ]
            if ch["material_url"]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"{E_MATERIAL} Course Material",
                        url=ch["material_url"]
                    )
                ])

            keyboard.append([btn(f"{E_BACK} Back", f"channel_{cid}")])

        else:
            # Permanent = one-time invite, but access itself does not expire.
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                member_limit=1,
                creates_join_request=False,
                name=f"PERM-{now.strftime('%Y%m%d-%H%M%S')}",
            )

            record_link(uid, ch["name"], "PERMANENT", invite.invite_link)

            text = (
                f"{E_PERM} <b>PERMANENT ACCESS GRANTED</b> {E_SPARK}\n"
                f"<b>ââââââââââââââââââââ</b>\n\n"
                f"{E_BOOK} <b>Course:</b> {safe(ch['name'])}\n\n"
                f"{E_JOIN} <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
                f"{E_LINK} <a href=\"{html.escape(invite.invite_link, quote=True)}\">Open Permanent Joining Link</a>\n\n"
                f"{E_MATERIAL} <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤¯à¤¹à¤¾à¤ à¤¦à¥à¤à¥à¤ (View Link):</b>\n"
                + (
                    f"{E_LINK} <a href=\"{html.escape(ch['material_url'], quote=True)}\">Open Course Material</a>\n\n"
                    if ch["material_url"]
                    else
                    f"{E_WARNING} Material link à¤à¤­à¥ add à¤¨à¤¹à¥à¤ à¤¹à¥.\n\n"
                )
                + f"{E_PIN} <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶ (Important):</b>\n"
                f"{E_WARNING} à¤¯à¤¹ Permanent Joining Link à¤à¥à¤µà¤² <b>1 à¤¬à¤¾à¤°</b> à¤à¤¾à¤® à¤à¤°à¥à¤à¤¾à¥¤\n"
                f"{E_CHECK} Join à¤à¤°à¤¨à¥ à¤à¥ à¤¬à¤¾à¤¦ à¤à¤à¥ à¤¸à¥ à¤à¤ªà¤° à¤¦à¤¿à¤ à¤à¤ <b>Course Material Link</b> à¤à¤¾ à¤¹à¥ à¤à¤ªà¤¯à¥à¤ à¤à¤°à¥à¤."
            )

            keyboard = [
                [InlineKeyboardButton(
                    f"{E_JOIN} Permanent Joining Link",
                    url=invite.invite_link
                )]
            ]
            if ch["material_url"]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"{E_MATERIAL} Course Material",
                        url=ch["material_url"]
                    )
                ])
            keyboard.append([btn(f"{E_BACK} Back", f"channel_{cid}")])

        await q.answer()
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception:
        logger.exception("Could not generate access link")
        await q.answer(
            "Link create nahi hua. Bot ko channel me Invite Users + Ban Users permission do.",
            show_alert=True,
        )


# ============================================================
# DEMO JOIN TRACKING + AUTO REMOVE
# ============================================================

async def handle_demo_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm or cm.chat.type != "channel":
        return

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status

    member_states = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    }

    if new_status not in member_states or old_status in member_states:
        return

    invite = cm.invite_link
    if not invite:
        return

    with connect_db() as con:
        row = con.execute(
            """
            SELECT * FROM demo_links
            WHERE invite_link=? AND used=0
            """,
            (invite.invite_link,),
        ).fetchone()

        if not row:
            return

        remove_at = datetime.now(timezone.utc) + timedelta(minutes=get_demo_minutes())
        user = cm.new_chat_member.user

        con.execute(
            """
            INSERT INTO demo_users
            (user_id,chat_id,channel_name,member_name,username,joined_at,remove_at,status)
            VALUES(?,?,?,?,?,?,?,'active')
            """,
            (
                user.id,
                str(cm.chat.id),
                row["channel_name"],
                user.full_name or "Unknown",
                user.username or "",
                datetime.now(timezone.utc).isoformat(),
                remove_at.isoformat(),
            ),
        )
        con.execute(
            "UPDATE demo_links SET used=1 WHERE invite_link=?",
            (invite.invite_link,),
        )
        con.commit()

    # Revoke the one-time invite immediately after use.
    try:
        await context.bot.revoke_chat_invite_link(
            chat_id=cm.chat.id,
            invite_link=invite.invite_link,
        )
    except Exception:
        logger.warning("Could not revoke used demo link.")

    logger.info(
        "Demo started | user=%s | channel=%s | remove_at=%s",
        user.id,
        cm.chat.id,
        remove_at.isoformat(),
    )


async def auto_remove_demos(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)

    with connect_db() as con:
        rows = con.execute(
            """
            SELECT * FROM demo_users
            WHERE status='active'
            """
        ).fetchall()

        for row in rows:
            try:
                remove_at = datetime.fromisoformat(row["remove_at"])
            except Exception:
                continue

            if now < remove_at:
                continue

            try:
                await context.bot.ban_chat_member(
                    chat_id=int(row["chat_id"]),
                    user_id=int(row["user_id"]),
                )

                con.execute(
                    "UPDATE demo_users SET status='removed' WHERE id=?",
                    (row["id"],),
                )
                con.commit()

                logger.info(
                    "Demo expired | user=%s | channel=%s",
                    row["user_id"],
                    row["chat_id"],
                )

            except Exception as exc:
                logger.warning(
                    "Demo removal failed | user=%s | channel=%s | %s",
                    row["user_id"],
                    row["chat_id"],
                    exc,
                )


# ============================================================
# DAILY REPORT
# ============================================================

async def daily_report(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    # records.created_at is stored as a display string, so do not compare
    # that string lexicographically. Read recent rows and parse their UTC time.
    with connect_db() as con:
        all_records = con.execute(
            """
            SELECT link_type, channel_name, created_at
            FROM records
            ORDER BY id DESC
            """
        ).fetchall()

        total_links = len(all_records)

        links_24h = []
        for rec in all_records:
            try:
                created = datetime.strptime(
                    rec["created_at"],
                    "%d/%m/%Y %H:%M UTC",
                ).replace(tzinfo=timezone.utc)
                if created >= since:
                    links_24h.append(rec)
            except (TypeError, ValueError):
                continue

        demo_24 = sum(1 for rec in links_24h if rec["link_type"] == "DEMO")
        perm_24 = sum(1 for rec in links_24h if rec["link_type"] == "PERMANENT")

        total_channels = con.execute(
            "SELECT COUNT(*) AS n FROM channels"
        ).fetchone()["n"]

        active_demos = con.execute(
            "SELECT COUNT(*) AS n FROM demo_users WHERE status='active'"
        ).fetchone()["n"]

        total_demo_users = con.execute(
            "SELECT COUNT(*) AS n FROM demo_users"
        ).fetchone()["n"]

        recent = con.execute(
            """
            SELECT channel_name,link_type,created_at
            FROM records
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()


    lines = [
        f"{E_REPORT} <b>DAILY REPORT</b>",
        "<b>ââââââââââââââââââââ</b>",
        "",
        f"{E_CLOCK} <b>Last 24 Hours</b>",
        f"{E_DEMO} Demo Links: <b>{demo_24}</b>",
        f"{E_PERM} Permanent Links: <b>{perm_24}</b>",
        "",
        f"{E_CHANNEL} Total Courses: <b>{total_channels}</b>",
        f"{E_LINK} Total Generated Links: <b>{total_links}</b>",
        f"{E_DEMO} Active Demo Members: <b>{active_demos}</b>",
        f"{E_USERS} Total Demo Records: <b>{total_demo_users}</b>",
        "",
        f"{E_BOOKMARK} <b>Recent Activity</b>",
    ]

    if recent:
        for r in recent:
            icon = E_DEMO if r["link_type"] == "DEMO" else E_PERM
            lines.append(
                f"{icon} {safe(r['channel_name'])} â¢ "
                f"<b>{safe(r['link_type'])}</b> â¢ {safe(r['created_at'])}"
            )
    else:
        lines.append("No activity yet.")

    await q.answer()
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [btn(f"{E_BACK} Owner Panel", "owner")]
        ]),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# INLINE COURSE SEARCH
# ============================================================

async def inline_search(update, context):
    query = update.inline_query
    uid = query.from_user.id

    if not is_admin(uid):
        await query.answer([], cache_time=0, is_personal=True)
        return

    term = (query.query or "").strip().lower()

    with connect_db() as con:
        if term:
            rows = con.execute(
                """
                SELECT c.id,c.name,cat.emoji
                FROM channels c
                JOIN categories cat ON cat.id=c.category_id
                WHERE LOWER(c.name) LIKE ?
                ORDER BY c.name COLLATE NOCASE
                LIMIT 30
                """,
                (f"%{term}%",),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT c.id,c.name,cat.emoji
                FROM channels c
                JOIN categories cat ON cat.id=c.category_id
                ORDER BY c.name COLLATE NOCASE
                LIMIT 30
                """
            ).fetchall()

    results = []
    for r in rows:
        name = repair_text(r["name"])
        emoji = repair_text(r["emoji"]) or E_BOOK

        results.append(
            InlineQueryResultArticle(
                id=f"course_{r['id']}",
                title=f"{emoji} {name}",
                description="Open premium access controls",
                input_message_content=InputTextMessageContent(
                    f"{emoji} <b>{safe(name)}</b>\n\n"
                    f"{E_SPARK} Select access type:",
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=InlineKeyboardMarkup([
                    [
                        btn(f"{E_DEMO} Demo", f"inline_demo_{r['id']}"),
                        btn(f"{E_PERM} Permanent", f"inline_perm_{r['id']}"),
                    ]
                ]),
            )
        )

    await query.answer(
        results,
        cache_time=0,
        is_personal=True,
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    if data == "home":
        await q.answer()
        await q.edit_message_text(
            premium_home_text(),
            reply_markup=home_keyboard(uid),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "search_help":
        await search_help(update, context)
        return

    if data == "owner":
        await owner_panel(update, context)
        return

    if data == "report":
        await daily_report(update, context)
        return

    if data == "demo_time":
        await demo_time_panel(update, context)
        return

    if data == "admins":
        await list_admins(update, context)
        return

    if data == "manage_categories":
        await manage_categories(update, context)
        return

    if data.startswith("category_"):
        try:
            cid = int(data.split("_", 1)[1])
        except ValueError:
            await q.answer("Invalid category.", show_alert=True)
            return
        await q.answer()
        await send_category_panel(q.message, uid, cid, edit=True)
        return

    if data.startswith("channel_"):
        try:
            cid = int(data.split("_", 1)[1])
        except ValueError:
            await q.answer("Invalid course.", show_alert=True)
            return
        await q.answer()
        await send_channel_panel(q.message, uid, cid, edit=True)
        return

    if data.startswith("gen_demo_") or data.startswith("gen_perm_"):
        await create_access_link(update, context)
        return

    if data.startswith("delete_channel_"):
        await delete_channel(update, context)
        return

    # Inline result callbacks are intentionally routed here too.
    if data.startswith("inline_demo_") or data.startswith("inline_perm_"):
        try:
            cid = int(data.rsplit("_", 1)[1])
        except ValueError:
            await q.answer("Invalid course.", show_alert=True)
            return

        # Reuse the same link generator by temporarily changing callback data.
        original = q.data
        q.data = f"gen_demo_{cid}" if data.startswith("inline_demo_") else f"gen_perm_{cid}"
        await create_access_link(update, context)
        q.data = original
        return

    await q.answer()


# ============================================================
# UNKNOWN COMMANDS = SILENT
# ============================================================

async def unknown_command(update, context):
    return


# ============================================================
# MAIN
# ============================================================

def build_application():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing in Railway Variables.")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID is missing in Railway Variables.")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    if not app.job_queue:
        raise RuntimeError(
            "JobQueue unavailable. Install python-telegram-bot[job-queue]."
        )

    # Check demo expiry every 10 seconds.
    app.job_queue.run_repeating(
        auto_remove_demos,
        interval=10,
        first=5,
    )

    # Commands.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", start))
    app.add_handler(CommandHandler("demotime", demotime_command))

    # Inline course search.
    app.add_handler(InlineQueryHandler(inline_search))

    # Channel auto-detect.
    app.add_handler(
        ChatMemberHandler(
            my_chat_member,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # Demo member tracking.
    app.add_handler(
        ChatMemberHandler(
            handle_demo_member_join,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # Silent unknown commands.
    app.add_handler(
        MessageHandler(filters.COMMAND, unknown_command)
    )

    # -------------------------
    # Conversations
    # -------------------------
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(add_admin_start, pattern=r"^add_admin$")],
            states={
                ADD_ADMIN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_save)
                ]
            },
            fallbacks=[CallbackQueryHandler(owner_panel, pattern=r"^owner$")],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(edit_admin_start, pattern=r"^edit_admin$")],
            states={
                EDIT_ADMIN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, edit_admin_save)
                ]
            },
            fallbacks=[CallbackQueryHandler(owner_panel, pattern=r"^owner$")],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(remove_admin_start, pattern=r"^remove_admin$")],
            states={
                REMOVE_ADMIN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_save)
                ]
            },
            fallbacks=[CallbackQueryHandler(owner_panel, pattern=r"^owner$")],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(add_category_start, pattern=r"^add_category$")],
            states={
                ADD_CATEGORY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_category_save)
                ]
            },
            fallbacks=[CallbackQueryHandler(owner_panel, pattern=r"^owner$")],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(add_channel_start, pattern=r"^add_channel_\d+$")],
            states={
                ADD_CHANNEL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_save)
                ]
            },
            fallbacks=[],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(edit_channel_start, pattern=r"^edit_channel_\d+$")],
            states={
                EDIT_CHANNEL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, edit_channel_save)
                ]
            },
            fallbacks=[],
            per_user=True,
            per_chat=True,
        )
    )

    # General callback router MUST be last.
    app.add_handler(
        CallbackQueryHandler(callback_router)
    )

    return app


def main():
    app = build_application()
    logger.info("Premium Course Bot is starting...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
