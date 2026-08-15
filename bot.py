import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from html import escape

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
    ChatJoinRequestHandler,
    InlineQueryHandler,
    ChatMemberHandler,
    filters,
)

# =========================================================
# CONFIG - KEEP SECRETS IN RAILWAY VARIABLES
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

DB_PATH = "bot.db"
DEMO_MINUTES = int(os.getenv("DEMO_MINUTES", "5"))
UNBAN_EXTRA_MINUTES = int(os.getenv("UNBAN_EXTRA_MINUTES", "2"))
REPORT_TZ = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("raj-course-bot")

# =========================================================
# EMOJI
# Using Unicode escapes prevents GitHub/encoding mojibake.
# =========================================================
BOOK = "\U0001F4DA"
FLAG = "\U0001F6A9"
NOTE = "\U0001F4DD"
GRAD = "\U0001F393"
SPARK = "\u2728"
CROWN = "\U0001F451"
GEAR = "\u2699\ufe0f"
PLUS = "\u2795"
CROSS = "\u274C"
BACK = "\u2B05\ufe0f"
LINK = "\U0001F517"
DEMO = "\u26A1"
PERM = "\u265B"
USERS = "\U0001F465"
CHANNEL = "\U0001F4CC"
SEARCH = "\U0001F50E"
FOLDER = "\U0001F4C2"
LOCK = "\U0001F512"
CHECK = "\u2705"
REPORT = "\U0001F4CA"
USER = "\U0001F464"
EDIT = "\u270F\ufe0f"
TRASH = "\U0001F5D1\ufe0f"

DEFAULT_BATCHES = [
    (BOOK, "Teaching Exam's"),
    (FLAG, "Ras/Psi"),
    (NOTE, "EO-Ro/bstc/cet"),
    (GRAD, "Net-Jrf"),
    (SPARK, "Other Exam's"),
]

# Conversation states
ADD_ADMIN, EDIT_ADMIN, REMOVE_ADMIN, ADD_BATCH, ADD_CHANNEL = range(5)

# =========================================================
# DATABASE
# =========================================================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins (
                uid INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                permissions TEXT NOT NULL DEFAULT 'demo,perm'
            );

            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emoji TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                tg_id TEXT NOT NULL UNIQUE
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
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS demo_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                ban_time TEXT NOT NULL,
                unban_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                member_name TEXT,
                username TEXT,
                joined_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                link_type TEXT,
                admin_uid INTEGER,
                admin_name TEXT,
                user_id INTEGER,
                member_name TEXT,
                username TEXT,
                channel_name TEXT,
                invite_link TEXT,
                event_time TEXT NOT NULL,
                details TEXT
            );
            """
        )

        # Migrate older bot.db files without deleting existing data.
        columns = {
            row["name"]
            for row in con.execute("PRAGMA table_info(demo_users)").fetchall()
        }
        for column, definition in (
            ("member_name", "TEXT"),
            ("username", "TEXT"),
            ("joined_at", "TEXT"),
        ):
            if column not in columns:
                con.execute(
                    f"ALTER TABLE demo_users ADD COLUMN {column} {definition}"
                )

        # Do NOT insert mojibake. These are pure Unicode values.
        for emoji, name in DEFAULT_BATCHES:
            con.execute(
                "INSERT OR IGNORE INTO batches (emoji, name) VALUES (?, ?)",
                (emoji, name),
            )

        if OWNER_ID:
            con.execute(
                """
                INSERT OR IGNORE INTO admins (uid, name, permissions)
                VALUES (?, ?, 'all')
                """,
                (OWNER_ID, "Owner"),
            )

        con.commit()


def repair_mojibake(value):
    """Repair common UTF-8 -> Latin-1/CP1252 mojibake."""
    if not value:
        return value

    current = value

    # Old data can be double-encoded, so try more than once.
    for _ in range(3):
        changed = False
        for enc in ("latin1", "cp1252"):
            try:
                fixed = current.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue

            bad = ("Ã", "Ã", "Ã¢", "Ã°", "ï¿½")
            if any(x in current for x in bad) and not any(x in fixed for x in bad):
                current = fixed
                changed = True
                break

        if not changed:
            break

    return current


def repair_database():
    with db() as con:
        rows = con.execute("SELECT id, emoji, name FROM batches").fetchall()

        # Known old category names are repaired deterministically.
        known = {
            "Teaching Exam's": (BOOK, "Teaching Exam's"),
            "Ras/Psi": (FLAG, "Ras/Psi"),
            "EO-Ro/bstc/cet": (NOTE, "EO-Ro/bstc/cet"),
            "Net-Jrf": (GRAD, "Net-Jrf"),
            "Other Exam's": (SPARK, "Other Exam's"),
        }

        for row in rows:
            old_emoji = row["emoji"] or ""
            old_name = row["name"] or ""
            new_emoji = repair_mojibake(old_emoji)
            new_name = repair_mojibake(old_name)

            # If the name survived but emoji is broken, repair only emoji.
            for plain_name, pair in known.items():
                if new_name == plain_name or old_name == plain_name:
                    new_emoji, new_name = pair
                    break

            if new_emoji != old_emoji or new_name != old_name:
                con.execute(
                    "UPDATE batches SET emoji=?, name=? WHERE id=?",
                    (new_emoji, new_name, row["id"]),
                )

        # Repair channel names too.
        channels = con.execute("SELECT id, name FROM channels").fetchall()
        for row in channels:
            new_name = repair_mojibake(row["name"] or "")
            if new_name != row["name"]:
                con.execute(
                    "UPDATE channels SET name=? WHERE id=?",
                    (new_name, row["id"]),
                )

        con.commit()


# =========================================================
# DEMO TIME SETTINGS
# =========================================================
def get_demo_minutes():
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key='demo_minutes'"
        ).fetchone()
    try:
        value = int(row["value"]) if row else DEMO_MINUTES
    except (TypeError, ValueError):
        value = DEMO_MINUTES
    return max(1, value)


def set_demo_minutes(minutes):
    minutes = max(1, int(minutes))
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES('demo_minutes',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(minutes),),
        )
        con.commit()


async def demotime_command(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            f"â±ï¸ Current Demo Time: {get_demo_minutes()} minutes\n\n"
            f"Change it with: /demotime 5"
        )
        return

    try:
        minutes = int(args[0])
        if minutes < 1 or minutes > 1440:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "â Demo time 1 à¤¸à¥ 1440 minutes à¤à¥ à¤¬à¥à¤ à¤¹à¥à¤¨à¤¾ à¤à¤¾à¤¹à¤¿à¤.\n"
            "Example: /demotime 5"
        )
        return

    set_demo_minutes(minutes)
    await update.effective_message.reply_text(
        f"â Demo Time updated: {minutes} minutes"
    )


async def demotime_button(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(
        f"â±ï¸ <b>DEMO TIME</b>\n\n"
        f"Current: <b>{get_demo_minutes()} minutes</b>\n\n"
        f"Change by sending:\n"
        f"<code>/demotime 5</code>\n"
        f"<code>/demotime 10</code>\n"
        f"<code>/demotime 30</code>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{BACK} Owner Panel", callback_data="owner")]]
        ),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# ACCESS
# =========================================================
def is_owner(uid):
    return bool(OWNER_ID) and uid == OWNER_ID


def is_admin(uid):
    if is_owner(uid):
        return True
    with db() as con:
        return con.execute(
            "SELECT 1 FROM admins WHERE uid=?",
            (uid,),
        ).fetchone() is not None


def permissions(uid):
    if is_owner(uid):
        return {"all"}
    with db() as con:
        row = con.execute(
            "SELECT permissions FROM admins WHERE uid=?",
            (uid,),
        ).fetchone()
    if not row:
        return set()
    return {x.strip().lower() for x in row["permissions"].split(",") if x.strip()}


def has_permission(uid, perm):
    if is_owner(uid):
        return True

    with db() as con:
        row = con.execute(
            "SELECT permissions FROM admins WHERE uid=?",
            (uid,),
        ).fetchone()

    if not row:
        return False

    perms = {
        p.strip().lower()
        for p in str(row["permissions"]).replace(" ", "").split(",")
        if p.strip()
    }

    requested = perm.strip().lower()

    if "all" in perms:
        return True
    if requested == "demo":
        return "demo" in perms
    if requested == "perm":
        return "perm" in perms or "permanent" in perms or "both" in perms

    return requested in perms

def normalize_permissions():
    with db() as con:
        rows = con.execute("SELECT uid, permissions FROM admins").fetchall()
        for row in rows:
            perms = {
                p.strip().lower()
                for p in str(row["permissions"]).replace(" ", "").split(",")
                if p.strip()
            }
            if "both" in perms:
                con.execute(
                    "UPDATE admins SET permissions=? WHERE uid=?",
                    ("demo,perm", row["uid"]),
                )
        con.commit()


def grid(items, cols=2):
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def log_activity(event_type, link_type=None, admin_uid=None, admin_name=None,
                 user_id=None, member_name=None, username=None,
                 channel_name=None, invite_link=None, event_time=None,
                 details=None):
    try:
        when = event_time or datetime.now(REPORT_TZ).strftime("%d/%m/%Y %H:%M:%S")
        with db() as con:
            con.execute(
                "INSERT INTO activity_events "
                "(event_type,link_type,admin_uid,admin_name,user_id,member_name,"
                "username,channel_name,invite_link,event_time,details) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_type, link_type, admin_uid, admin_name, user_id,
                 member_name, username, channel_name, invite_link, when,
                 details),
            )
            con.commit()
    except Exception:
        logger.exception("Could not log activity")


def home_keyboard(uid):
    """
    Course/category buttons are intentionally removed.
    Courses are accessed through Telegram Inline Mode:
    @YourBotName <course name>

    Owner Panel and Records remain available for admins.
    """
    kb = []

    if is_owner(uid):
        kb.append([
            InlineKeyboardButton(
                f"{GEAR} Owner Panel",
                callback_data="owner",
            ),
            InlineKeyboardButton(
                f"{REPORT} Records",
                callback_data="records",
            ),
        ])

    # Admins with demo/both access do not get the Records button.
    return InlineKeyboardMarkup(kb)


# =========================================================
# START / ACCESS
# Unauthorized users get absolutely no response.
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    uid = update.effective_user.id

    if not is_admin(uid):
        return

    args = context.args or []

    if args:
        if args[0].startswith("batch_"):
            try:
                batch_id = int(args[0].split("_", 1)[1])
            except ValueError:
                return
            await send_batch(update.effective_message, uid, batch_id)
            return

        if args[0].startswith("channel_"):
            try:
                channel_id = int(args[0].split("_", 1)[1])
            except ValueError:
                return
            await send_channel(update.effective_message, uid, channel_id)
            return

    text = (
        "Hello Seller Famaily Kese Ho\n\n"
        "I AM WIZARD ð¸ð\n\n"
        "ð <b>Course Search</b>\n"
        "Telegram ke kisi chat me likho:\n"
        "<code>@YourBotUsername course-name</code>\n\n"
        "Search results me apna course select karo."
    )

    await update.effective_message.reply_text(
        text,
        reply_markup=home_keyboard(uid),
        parse_mode=ParseMode.HTML,
    )


async def unknown_command(update, context):
    # Silent for everyone; especially unauthorized users.
    return


# =========================================================
# BATCHES
# =========================================================
async def send_batch(message, uid, batch_id):
    if not is_admin(uid):
        return

    with db() as con:
        batch = con.execute(
            "SELECT * FROM batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        channels = con.execute(
            """
            SELECT id, name
            FROM channels
            WHERE batch_id=?
            ORDER BY name COLLATE NOCASE
            """,
            (batch_id,),
        ).fetchall()

    if not batch:
        return

    emoji = repair_mojibake(batch["emoji"]) or BOOK
    name = repair_mojibake(batch["name"]) or "Category"

    buttons = []
    for c in channels:
        cname = repair_mojibake(c["name"]) or "Channel"
        buttons.append(
            InlineKeyboardButton(
                f"{BOOK} {cname}",
                callback_data=f"channel_{c['id']}",
            )
        )

    kb = grid(buttons, 1)

    if is_owner(uid):
        kb.append(
            [
                InlineKeyboardButton(
                    f"{PLUS} Add Channel",
                    callback_data=f"add_channel_{batch_id}",
                )
            ]
        )

    kb.append(
        [InlineKeyboardButton(f"{BACK} Back", callback_data="home")]
    )

    text = (
        f"{emoji} <b>{escape(name)}</b>\n\n"
        f"{CHANNEL} Channels: {len(channels)}"
    )

    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


async def batch_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    try:
        batch_id = int(q.data.split("_", 1)[1])
    except ValueError:
        return

    await q.answer()
    with db() as con:
        batch = con.execute(
            "SELECT * FROM batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        channels = con.execute(
            """
            SELECT id, name FROM channels
            WHERE batch_id=?
            ORDER BY name COLLATE NOCASE
            """,
            (batch_id,),
        ).fetchall()

    if not batch:
        return

    emoji = repair_mojibake(batch["emoji"]) or BOOK
    name = repair_mojibake(batch["name"]) or "Category"

    buttons = []
    for c in channels:
        cname = repair_mojibake(c["name"]) or "Channel"
        buttons.append(
            InlineKeyboardButton(
                f"{BOOK} {cname}",
                callback_data=f"channel_{c['id']}",
            )
        )

    kb = grid(buttons, 1)

    if is_owner(uid):
        kb.append(
            [
                InlineKeyboardButton(
                    f"{PLUS} Add Channel",
                    callback_data=f"add_channel_{batch_id}",
                )
            ]
        )

    kb.append(
        [InlineKeyboardButton(f"{BACK} Back", callback_data="home")]
    )

    await q.edit_message_text(
        f"{emoji} <b>{escape(name)}</b>\n\n"
        f"{CHANNEL} Channels: {len(channels)}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# CHANNEL PANEL
# =========================================================
async def send_channel(message, uid, channel_id):
    if not is_admin(uid):
        return

    with db() as con:
        ch = con.execute(
            "SELECT * FROM channels WHERE id=?",
            (channel_id,),
        ).fetchone()

    if not ch:
        return

    name = repair_mojibake(ch["name"]) or "Channel"
    buttons = []

    if has_permission(uid, "demo"):
        buttons.append(
            InlineKeyboardButton(
                f"{LINK} Demo Link",
                callback_data=f"gen_demo_{channel_id}",
            )
        )

    if has_permission(uid, "perm"):
        buttons.append(
            InlineKeyboardButton(
                f"{USER} Permanent Link",
                callback_data=f"gen_perm_{channel_id}",
            )
        )

    kb = grid(buttons, 2)

    if is_owner(uid):
        kb.insert(
            0,
            [
                InlineKeyboardButton(
                    f"{EDIT} Edit Name",
                    callback_data=f"edit_name_{channel_id}",
                ),
                InlineKeyboardButton(
                    f"{EDIT} Edit ID",
                    callback_data=f"edit_id_{channel_id}",
                ),
            ],
        )
        kb.insert(
            1,
            [
                InlineKeyboardButton(
                    f"{TRASH} Delete",
                    callback_data=f"delete_{channel_id}",
                )
            ],
        )

    kb.append(
        [InlineKeyboardButton(f"{BACK} Back", callback_data=f"batch_{ch['batch_id']}")]
    )

    await message.reply_text(
        f"ð  <b>{escape(name)}</b>\n\n"
        "Choose an access link below:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


async def channel_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    try:
        channel_id = int(q.data.split("_", 1)[1])
    except ValueError:
        return

    await q.answer()

    with db() as con:
        ch = con.execute(
            "SELECT * FROM channels WHERE id=?",
            (channel_id,),
        ).fetchone()

    if not ch:
        return

    name = repair_mojibake(ch["name"]) or "Channel"
    buttons = []

    if has_permission(uid, "demo"):
        buttons.append(
            InlineKeyboardButton(
                f"{LINK} Demo Link",
                callback_data=f"gen_demo_{channel_id}",
            )
        )

    if has_permission(uid, "perm"):
        buttons.append(
            InlineKeyboardButton(
                f"{USER} Permanent Link",
                callback_data=f"gen_perm_{channel_id}",
            )
        )

    kb = grid(buttons, 2)

    if is_owner(uid):
        kb.insert(
            0,
            [
                InlineKeyboardButton(
                    f"{EDIT} Edit Name",
                    callback_data=f"edit_name_{channel_id}",
                ),
                InlineKeyboardButton(
                    f"{EDIT} Edit ID",
                    callback_data=f"edit_id_{channel_id}",
                ),
            ],
        )
        kb.insert(
            1,
            [
                InlineKeyboardButton(
                    f"{TRASH} Delete",
                    callback_data=f"delete_{channel_id}",
                )
            ],
        )

    kb.append(
        [InlineKeyboardButton(f"{BACK} Back", callback_data=f"batch_{ch['batch_id']}")]
    )

    await q.edit_message_text(
        f"ð  <b>{escape(name)}</b>\n\n"
        "Choose an access link below:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# OWNER PANEL / ADMIN ACCESS
# =========================================================
async def owner_panel(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return

    with db() as con:
        admins = con.execute(
            "SELECT uid, name, permissions FROM admins ORDER BY uid"
        ).fetchall()
        batches = con.execute(
            "SELECT id, emoji, name FROM batches ORDER BY id"
        ).fetchall()
        channels = con.execute(
            "SELECT COUNT(*) AS n FROM channels"
        ).fetchone()["n"]

    text = (
        f"{CROWN} <b>OWNER PANEL</b>\n\n"
        f"{USERS} Admins: {len(admins)}\n"
        f"{FOLDER} Categories: {len(batches)}\n"
        f"{CHANNEL} Channels: {channels}"
    )

    kb = [
        [
            InlineKeyboardButton(f"{PLUS} Add Admin", callback_data="add_admin"),
            InlineKeyboardButton(f"{EDIT} Edit Access", callback_data="edit_admin"),
        ],
        [
            InlineKeyboardButton(f"{TRASH} Remove Admin", callback_data="remove_admin"),
            InlineKeyboardButton(f"{PLUS} New Category", callback_data="add_batch"),
        ],
        [
            InlineKeyboardButton(f"{USERS} View Admins", callback_data="admins"),
        ],
        [
            InlineKeyboardButton("â±ï¸ Demo Time", callback_data="demo_time"),
        ],
        [
            InlineKeyboardButton(f"{BACK} Home", callback_data="home"),
        ],
    ]

    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


async def list_admins(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return

    with db() as con:
        rows = con.execute(
            "SELECT uid, name, permissions FROM admins ORDER BY uid"
        ).fetchall()

    lines = [f"{USERS} <b>ADMINS</b>\n"]
    for r in rows:
        label = "OWNER" if r["uid"] == OWNER_ID else "ADMIN"
        lines.append(
            f"{USER} {escape(r['name'])} | <code>{r['uid']}</code>\n"
            f"Access: <code>{escape(r['permissions'])}</code> | {label}\n"
        )

    kb = [
        [InlineKeyboardButton(f"{BACK} Owner Panel", callback_data="owner")]
    ]

    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


async def add_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{PLUS} <b>ADD ADMIN</b>\n\n"
        "User ID aur access bhejo:\n\n"
        "<code>123456789 | demo</code>\n"
        "ya\n"
        "<code>123456789 | both</code>\n\n"
        "<b>demo</b> = sirf Demo links\n"
        "<b>both</b> = Demo + Permanent links",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{CROSS} Cancel", callback_data="owner")]]
        ),
        parse_mode=ParseMode.HTML,
    )
    return ADD_ADMIN


async def add_admin_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        parts = [x.strip() for x in update.message.text.split("|", 1)]

        if len(parts) != 2:
            await update.message.reply_text(
                "â Format galat hai.\n\n"
                "Example:\n"
                "<code>123456789 | demo</code>\n"
                "ya\n"
                "<code>123456789 | both</code>",
                parse_mode=ParseMode.HTML,
            )
            return ADD_ADMIN

        uid = int(parts[0])
        access = parts[1].lower()

        if uid <= 0:
            raise ValueError

        if uid == OWNER_ID:
            await update.message.reply_text("Owner already exists.")
            return ADD_ADMIN

        if access == "demo":
            permissions = "demo"
            access_text = "Demo only"
        elif access in ("both", "perm", "permanent"):
            permissions = "demo,perm"
            access_text = "Demo + Permanent"
        else:
            await update.message.reply_text(
                "â Access sirf <code>demo</code> ya <code>both</code> ho sakta hai.",
                parse_mode=ParseMode.HTML,
            )
            return ADD_ADMIN

        with db() as con:
            con.execute(
                """
                INSERT INTO admins(uid, name, permissions)
                VALUES (?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    name='Admin',
                    permissions=excluded.permissions
                """,
                (uid, "Admin", permissions),
            )
            con.commit()

        await update.message.reply_text(
            f"{CHECK} <b>ADMIN ADDED</b>\n\n"
            f"User ID: <code>{uid}</code>\n"
            f"Access: <b>{access_text}</b>",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text(
            "â User ID numeric hona chahiye.\n"
            "Example: <code>123456789 | demo</code>",
            parse_mode=ParseMode.HTML,
        )
        return ADD_ADMIN


async def edit_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{EDIT} <b>EDIT ACCESS</b>\n\n"
        "Send:\n"
        "<code>UserID | demo</code>\n"
        "or\n"
        "<code>UserID | perm</code>\n"
        "or\n"
        "<code>UserID | demo,perm</code>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{CROSS} Cancel", callback_data="owner")]]
        ),
        parse_mode=ParseMode.HTML,
    )
    return EDIT_ADMIN


async def edit_admin_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid_text, perms = [x.strip() for x in update.message.text.split("|", 1)]
        uid = int(uid_text)

        if uid == OWNER_ID:
            await update.message.reply_text("Owner access cannot be edited.")
            return EDIT_ADMIN

        with db() as con:
            row = con.execute(
                "SELECT 1 FROM admins WHERE uid=?",
                (uid,),
            ).fetchone()
            if not row:
                await update.message.reply_text("Admin not found.")
                return EDIT_ADMIN

            con.execute(
                "UPDATE admins SET permissions=? WHERE uid=?",
                (perms.lower(), uid),
            )
            con.commit()

        await update.message.reply_text(f"{CHECK} Access updated.")
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("Invalid UserID.")
        return EDIT_ADMIN


async def remove_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{TRASH} <b>REMOVE ADMIN</b>\n\nSend UserID:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{CROSS} Cancel", callback_data="owner")]]
        ),
        parse_mode=ParseMode.HTML,
    )
    return REMOVE_ADMIN


async def remove_admin_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid = int(update.message.text.strip())
        if uid == OWNER_ID:
            await update.message.reply_text("Owner cannot be removed.")
            return REMOVE_ADMIN

        with db() as con:
            con.execute("DELETE FROM admins WHERE uid=?", (uid,))
            con.commit()

        await update.message.reply_text(f"{CHECK} Access removed.")
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("Invalid UserID.")
        return REMOVE_ADMIN


# =========================================================
# ADD CATEGORY
# =========================================================
async def add_batch_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{PLUS} <b>NEW CATEGORY</b>\n\n"
        "Send:\n"
        "<code>Emoji | Category Name</code>\n\n"
        "Example:\n"
        "<code>ð | UPSC/IAS</code>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{CROSS} Cancel", callback_data="owner")]]
        ),
        parse_mode=ParseMode.HTML,
    )
    return ADD_BATCH


async def add_batch_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    parts = [x.strip() for x in update.message.text.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "Format: Emoji | Category Name"
        )
        return ADD_BATCH

    emoji, name = parts

    with db() as con:
        try:
            con.execute(
                "INSERT INTO batches(emoji, name) VALUES (?, ?)",
                (emoji, name),
            )
            con.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                "Category name already exists."
            )
            return ADD_BATCH

    await update.message.reply_text(
        f"{CHECK} Category added: {emoji} {name}"
    )
    return ConversationHandler.END


# =========================================================
# AUTO-DETECT CHANNELS
# When bot becomes admin in a channel, automatically add it.
# Default category = Other Exam's.
# =========================================================
async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm or not cm.chat:
        return

    chat = cm.chat

    # Only channels.
    if chat.type != "channel":
        return

    new_status = cm.new_chat_member.status
    old_status = cm.old_chat_member.status

    became_admin = new_status == ChatMemberStatus.ADMINISTRATOR
    if not became_admin:
        return

    if old_status == ChatMemberStatus.ADMINISTRATOR:
        return

    if not OWNER_ID:
        return

    with db() as con:
        batch = con.execute(
            """
            SELECT id FROM batches
            WHERE name='Other Exam''s'
            LIMIT 1
            """
        ).fetchone()

        if not batch:
            con.execute(
                "INSERT INTO batches(emoji,name) VALUES(?,?)",
                (SPARK, "Other Exam's"),
            )
            con.commit()
            batch = con.execute(
                "SELECT id FROM batches WHERE name='Other Exam''s'"
            ).fetchone()

        batch_id = batch["id"]
        name = chat.title or "Telegram Channel"
        tg_id = str(chat.id)

        con.execute(
            """
            INSERT INTO channels(batch_id,name,tg_id)
            VALUES(?,?,?)
            ON CONFLICT(tg_id) DO UPDATE SET
                name=excluded.name
            """,
            (batch_id, name, tg_id),
        )
        con.commit()

    logger.info("Auto-detected channel: %s (%s)", chat.title, chat.id)

    # Notify owner only if the bot can send a message to owner.
    try:
        await context.bot.send_message(
            OWNER_ID,
            f"{CHECK} <b>Channel Auto Added</b>\n\n"
            f"{BOOK} {escape(name)}\n"
            f"{CHANNEL} <code>{tg_id}</code>\n"
            f"Category: {SPARK} Other Exam's",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# =========================================================
# ADD CHANNEL MANUALLY
# =========================================================
async def add_channel_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return ConversationHandler.END

    try:
        batch_id = int(q.data.split("_", 2)[2])
    except ValueError:
        return ConversationHandler.END

    context.user_data["batch_id"] = batch_id
    await q.answer()

    await q.edit_message_text(
        f"{PLUS} <b>ADD CHANNEL</b>\n\n"
        "Send:\n"
        "<code>Channel Name | -1001234567890</code>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                f"{CROSS} Cancel",
                callback_data=f"batch_{batch_id}"
            )]]
        ),
        parse_mode=ParseMode.HTML,
    )
    return ADD_CHANNEL


async def add_channel_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    batch_id = context.user_data.get("batch_id")
    if not batch_id:
        return ConversationHandler.END

    parts = [x.strip() for x in update.message.text.split("|", 1)]
    if len(parts) != 2:
        await update.message.reply_text(
            "Format: Channel Name | -1001234567890"
        )
        return ADD_CHANNEL

    name, tg_id = parts

    if not re.fullmatch(r"-100\d+", tg_id):
        await update.message.reply_text(
            "Channel ID must look like -1001234567890"
        )
        return ADD_CHANNEL

    try:
        chat = await context.bot.get_chat(int(tg_id))
        if chat.type != "channel":
            await update.message.reply_text("This ID is not a channel.")
            return ADD_CHANNEL
    except Exception:
        await update.message.reply_text(
            "Bot cannot access this channel. Make the bot an admin first."
        )
        return ADD_CHANNEL

    with db() as con:
        try:
            con.execute(
                "INSERT INTO channels(batch_id,name,tg_id) VALUES(?,?,?)",
                (batch_id, name, tg_id),
            )
            con.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                "This channel is already added."
            )
            return ADD_CHANNEL

    await update.message.reply_text(
        f"{CHECK} Channel added: {name}"
    )
    return ConversationHandler.END


# =========================================================
# LINK GENERATION
# =========================================================
async def generate_link(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    parts = q.data.split("_")
    link_type = "demo" if parts[1] == "demo" else "perm"

    if not has_permission(uid, link_type):
        await q.answer("Permission denied.", show_alert=True)
        return

    try:
        channel_id = int(parts[2])
    except (ValueError, IndexError):
        return

    with db() as con:
        ch = con.execute(
            "SELECT * FROM channels WHERE id=?",
            (channel_id,),
        ).fetchone()

    if not ch:
        await q.answer("Channel not found.", show_alert=True)
        return

    try:
        chat_id = int(ch["tg_id"])
        now = datetime.now(timezone.utc)

        if link_type == "demo":
            # Direct-join demo link. No approval screen.
            # The actual join is tracked by ChatMemberHandler below.
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                member_limit=1,
                creates_join_request=False,
                name=f"demo_{now.strftime('%Y%m%d_%H%M%S')}",
            )

            with db() as con:
                con.execute(
                    """
                    INSERT OR REPLACE INTO demo_links
                    (invite_link,chat_id,channel_name,used)
                    VALUES(?,?,?,0)
                    """,
                    (
                        invite.invite_link,
                        str(chat_id),
                        ch["name"],
                    ),
                )
                con.execute(
                    """
                    INSERT INTO records
                    (admin_uid,admin_name,channel_name,link_type,link_url,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        uid,
                        "Owner" if is_owner(uid) else f"Admin {uid}",
                        ch["name"],
                        "DEMO",
                        invite.invite_link,
                        now.strftime("%d/%m/%Y %H:%M"),
                    ),
                )
                con.commit()

            view_link = make_view_link(ch["tg_id"])
            text = (
                f"ð <b>Access Granted: Demo Pass</b> â³\n"
                f"ââââââââââââââââââ\n\n"
                f"ð« <b>à¤à¥à¤¨à¤² / à¤à¥à¤°à¥à¤¸ à¤à¤¾ à¤¨à¤¾à¤®:</b>\n"
                f"â¡ï¸ {escape(repair_mojibake(ch['name']) or 'Course')}\n\n"
                f"ð¥ <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
                f"ð <a href=\"{escape(invite.invite_link, quote=True)}\">{escape(invite.invite_link)}</a>\n\n"
                f"ð¥ï¸ <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤¯à¤¹à¤¾à¤ à¤¦à¥à¤à¥à¤ (View Link):</b>\n"
                f"ð <a href=\"{escape(view_link, quote=True)}\">{escape(view_link)}</a>\n\n"
                f"ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶ (Important):</b>\n"
                f"â ï¸ à¤¯à¤¹ Joining Link à¤à¥à¤µà¤² 1 à¤¬à¤¾à¤° à¤à¤¾à¤® à¤à¤°à¥à¤à¤¾à¥¤ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¤¤à¥ à¤¹à¥ à¤¯à¤¹ à¤¬à¤à¤¦ à¤¹à¥ à¤à¤¾à¤à¤à¤¾à¥¤\n"
                f"â±ï¸ à¤¸à¤¿à¤¸à¥à¤à¤® à¤à¤ªà¤à¥ à¤à¥à¤¨à¤² à¤®à¥à¤ à¤à¥à¤¡à¤¼à¤¨à¥ à¤à¥ à¤ à¥à¤ <b>{get_demo_minutes()} à¤®à¤¿à¤¨à¤</b> à¤¬à¤¾à¤¦ à¤à¤à¥à¤®à¥à¤à¤¿à¤ à¤¬à¤¾à¤¹à¤° (Kick) à¤à¤° à¤¦à¥à¤à¤¾à¥¤\n\n"
                f"â à¤à¥à¤ªà¤¯à¤¾ à¤à¤à¥ à¤¸à¥ à¤à¥à¤²à¤¾à¤¸ à¤¦à¥à¤à¤¨à¥ à¤à¥ à¤²à¤¿à¤ à¤¹à¤®à¥à¤¶à¤¾ à¤à¤ªà¤° à¤¦à¤¿à¤ à¤à¤ <b>'à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤²à¤¿à¤à¤'</b> à¤à¤¾ à¤¹à¥ à¤à¤ªà¤¯à¥à¤ à¤à¤°à¥à¤à¥¤"
            )

        else:
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                member_limit=1,
                name=f"perm_{now.strftime('%Y%m%d_%H%M%S')}",
            )

            with db() as con:
                con.execute(
                    """
                    INSERT INTO records
                    (admin_uid,admin_name,channel_name,link_type,link_url,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        uid,
                        "Owner" if is_owner(uid) else f"Admin {uid}",
                        ch["name"],
                        "PERM",
                        invite.invite_link,
                        now.strftime("%d/%m/%Y %H:%M"),
                    ),
                )
                con.commit()

            view_link = make_view_link(ch["tg_id"])
            text = (
                f"ð <b>Access Granted: Permanent Pass</b> ð\n"
                f"ââââââââââââââââââ\n\n"
                f"ð« <b>à¤à¥à¤¨à¤² / à¤à¥à¤°à¥à¤¸ à¤à¤¾ à¤¨à¤¾à¤®:</b>\n"
                f"â¡ï¸ {escape(repair_mojibake(ch['name']) or 'Course')}\n\n"
                f"ð¥ <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
                f"ð <a href=\"{escape(invite.invite_link, quote=True)}\">{escape(invite.invite_link)}</a>\n\n"
                f"ð¥ï¸ <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤¯à¤¹à¤¾à¤ à¤¦à¥à¤à¥à¤ (View Link):</b>\n"
                f"ð <a href=\"{escape(view_link, quote=True)}\">{escape(view_link)}</a>\n\n"
                f"ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶ (Important):</b>\n"
                f"ð« à¤¯à¤¹ Permanent Joining Link à¤à¥à¤µà¤² à¤à¤ à¤¬à¤¾à¤° à¤à¥ à¤²à¤¿à¤ à¤¹à¥à¥¤ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¤¤à¥ à¤¹à¥ à¤¯à¤¹ Expire à¤¹à¥ à¤à¤¾à¤à¤à¤¾à¥¤\n\n"
                f"â à¤à¥à¤ªà¤¯à¤¾ à¤à¤à¥ à¤¸à¥ à¤à¥à¤²à¤¾à¤¸ à¤¦à¥à¤à¤¨à¥ à¤à¥ à¤²à¤¿à¤ à¤¹à¤®à¥à¤¶à¤¾ à¤à¤ªà¤° à¤¦à¤¿à¤ à¤à¤ <b>'à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤²à¤¿à¤à¤'</b> à¤à¤¾ à¤¹à¥ à¤à¤ªà¤¯à¥à¤ à¤à¤°à¥à¤à¥¤"
            )

        await q.answer()
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    f"{BACK} Back",
                    callback_data=f"channel_{channel_id}"
                )]]
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.exception("Link generation failed")
        await q.answer("Could not create link. Check channel admin permissions.", show_alert=True)


# =========================================================
# JOIN REQUEST + AUTO BAN/UNBAN
# =========================================================
async def handle_join_request(update, context):
    req = update.chat_join_request

    if not req or not req.invite_link:
        return

    invite = req.invite_link.invite_link

    with db() as con:
        row = con.execute(
            """
            SELECT * FROM demo_links
            WHERE invite_link=? AND used=0
            """,
            (invite,),
        ).fetchone()

        if not row:
            return

        try:
            await context.bot.approve_chat_join_request(
                req.chat.id,
                req.from_user.id,
            )
        except Exception:
            logger.exception("Could not approve demo join request")
            return

        # User gets exactly DEMO_MINUTES of access.
        remove_time = (
            datetime.now(timezone.utc)
            + timedelta(minutes=get_demo_minutes())
        )

        con.execute(
            """
            INSERT INTO demo_users
            (user_id,chat_id,channel_name,ban_time,unban_time,status)
            VALUES(?,?,?,?,?,'active')
            """,
            (
                req.from_user.id,
                str(req.chat.id),
                row["channel_name"],
                remove_time.isoformat(),
                remove_time.isoformat(),
            ),
        )

        # One generated demo link = one user.
        con.execute(
            "UPDATE demo_links SET used=1 WHERE invite_link=?",
            (invite,),
        )
        con.commit()


async def handle_member_access_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm or not cm.chat or not cm.new_chat_member or not cm.invite_link:
        return
    if cm.chat.type != "channel":
        return

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status
    if new_status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return
    if old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return

    invite = cm.invite_link.invite_link
    with db() as con:
        demo = con.execute(
            "SELECT 1 FROM demo_links WHERE invite_link=?",
            (invite,),
        ).fetchone()
        rec = con.execute(
            "SELECT admin_uid,admin_name,channel_name,link_type "
            "FROM records WHERE link_url=? ORDER BY id DESC LIMIT 1",
            (invite,),
        ).fetchone()

    # Demo joins are logged by handle_demo_member_join, which also starts the timer.
    if demo or not rec:
        return

    if str(rec["link_type"]).upper() != "PERM":
        return

    member = cm.new_chat_member.user
    member_name = (member.full_name or "").strip() or "Unknown"
    username = (member.username or "").strip() or None

    log_activity(
        "PERM_JOIN",
        link_type="PERM",
        admin_uid=rec["admin_uid"],
        admin_name=rec["admin_name"],
        user_id=member.id,
        member_name=member_name,
        username=username,
        channel_name=rec["channel_name"],
        invite_link=invite,
        details="Permanent member joined",
    )


async def handle_demo_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start demo timer when a user actually joins through a generated demo invite."""
    cm = update.chat_member
    if not cm or not cm.chat or not cm.new_chat_member:
        return

    if cm.chat.type != "channel":
        return

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status

    if new_status not in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        return

    if old_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        return

    invite = cm.invite_link
    if not invite:
        return

    member = cm.new_chat_member.user
    member_name = (member.full_name or "").strip() or "Unknown"
    username = (member.username or "").strip() or None
    joined_at = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S")

    with db() as con:
        row = con.execute(
            "SELECT * FROM demo_links WHERE invite_link=? AND used=0",
            (invite.invite_link,),
        ).fetchone()

        if not row:
            return

        remove_time = (
            datetime.now(timezone.utc)
            + timedelta(minutes=get_demo_minutes())
        )

        con.execute(
            "UPDATE demo_links SET used=1 WHERE invite_link=?",
            (invite.invite_link,),
        )

        con.execute(
            """
            INSERT INTO demo_users
            (user_id,chat_id,channel_name,ban_time,unban_time,status,
             member_name,username,joined_at)
            VALUES(?,?,?,?,?,'active',?,?,?)
            """,
            (
                member.id,
                str(cm.chat.id),
                row["channel_name"],
                remove_time.isoformat(),
                remove_time.isoformat(),
                member_name,
                username,
                joined_at,
            ),
        )
        con.commit()

    log_activity(
        "DEMO_JOIN",
        link_type="DEMO",
        user_id=member.id,
        member_name=member_name,
        username=username,
        channel_name=row["channel_name"],
        invite_link=invite.invite_link,
        details=f"Demo timer: {get_demo_minutes()} minutes",
    )

    try:
        await context.bot.revoke_chat_invite_link(
            chat_id=cm.chat.id,
            invite_link=invite.invite_link,
        )
    except Exception:
        logger.exception("Could not revoke used demo invite")

    logger.info(
        "Demo timer started: user=%s (%s) chat=%s remove_at=%s",
        member.id,
        member_name,
        cm.chat.id,
        remove_time.isoformat(),
    )


async def auto_ban_unban(context):
    """
    At DEMO_MINUTES the demo user is banned/removed.
    There is no automatic unban after that.
    """
    now = datetime.now(timezone.utc)

    with db() as con:
        rows = con.execute(
            """
            SELECT * FROM demo_users
            WHERE status='active'
            """
        ).fetchall()

        for row in rows:
            try:
                remove_time = datetime.fromisoformat(row["ban_time"])

                if now < remove_time:
                    continue

                chat_id = int(row["chat_id"])
                user_id = int(row["user_id"])

                try:
                    member_state = await context.bot.get_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                    )
                    if member_state.status in ("left", "kicked"):
                        con.execute(
                            "UPDATE demo_users SET status='removed' WHERE id=?",
                            (row["id"],),
                        )
                        con.commit()
                        log_activity(
                            "DEMO_REMOVED",
                            link_type="DEMO",
                            user_id=row["user_id"],
                            member_name=row["member_name"],
                            username=row["username"],
                            channel_name=row["channel_name"],
                            details="Member was already out of the channel",
                        )
                        continue
                except Exception:
                    # If lookup fails, still try the ban below.
                    pass

                await context.bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                )

                con.execute(
                    "UPDATE demo_users SET status='removed' WHERE id=?",
                    (row["id"],),
                )
                con.commit()

                log_activity(
                    "DEMO_REMOVED",
                    link_type="DEMO",
                    user_id=user_id,
                    member_name=row["member_name"],
                    username=row["username"],
                    channel_name=row["channel_name"],
                    details="Demo time completed; member removed",
                )

                logger.info(
                    "Demo removed: user=%s chat=%s",
                    user_id,
                    chat_id,
                )

            except Exception as e:
                logger.warning(
                    "Demo auto-remove failed for user=%s chat=%s: %s",
                    row["user_id"], row["chat_id"], e
                )

        # Keep removed members in the database so Owner > Records
        # can always see who joined, when they joined and when they were removed.
        con.commit()

# =========================================================
# RECORDS
# =========================================================
async def records_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return

    today = datetime.now(REPORT_TZ).strftime("%d/%m/%Y")

    with db() as con:
        events = con.execute(
            "SELECT * FROM activity_events "
            "WHERE substr(event_time,1,10)=? ORDER BY id DESC LIMIT 100",
            (today,),
        ).fetchall()

        members = con.execute(
            "SELECT user_id,member_name,username,channel_name,joined_at,"
            "ban_time,status FROM demo_users ORDER BY id DESC LIMIT 50"
        ).fetchall()

    lines = [
        f"{REPORT} <b>DAILY ACTIVITY REPORT</b>",
        f"ð <b>{escape(today)}</b>\n",
    ]

    if events:
        labels = {
            "DEMO_LINK": ("â¡", "Demo Link Sent"),
            "PERM_LINK": ("ð", "Permanent Link Sent"),
            "DEMO_JOIN": ("ð¤", "Demo Member Joined"),
            "PERM_JOIN": ("ð¤", "Permanent Member Joined"),
            "DEMO_REMOVED": ("ð«", "Demo Member Removed"),
        }

        for e in events:
            icon, title = labels.get(
                e["event_type"],
                ("ð", str(e["event_type"]).replace("_", " ").title()),
            )
            course = repair_mojibake(e["channel_name"] or "Course")
            admin = e["admin_name"] or (
                f"ID {e['admin_uid']}" if e["admin_uid"] else "-"
            )

            lines.append(
                f"{icon} <b>{title}</b>\n"
                f"ð« {escape(course)}\n"
                f"ð¤ Admin: {escape(admin)}\n"
            )

            if e["user_id"]:
                who = e["member_name"] or "Unknown"
                if e["username"]:
                    who += f" (@{e['username']})"
                lines.append(
                    f"ð¤ Member: {escape(who)}\n"
                    f"ð ID: <code>{e['user_id']}</code>\n"
                )

            if e["details"]:
                lines.append(f"â¹ï¸ {escape(e['details'])}\n")

            lines.append(f"ð {escape(e['event_time'])}\n")

            if e["invite_link"] and e["event_type"] in ("DEMO_LINK", "PERM_LINK"):
                link = escape(e["invite_link"], quote=True)
                lines.append(f'ð <a href="{link}">Open Link</a>\n')

            lines.append("ââââââââââââââ\n")
    else:
        lines.append("à¤à¤ à¤à¥ à¤à¥à¤ activity à¤¨à¤¹à¥à¤ à¤®à¤¿à¤²à¥.\n")

    lines.append("<b>Recent Demo Members</b>")
    if members:
        for m in members[:30]:
            display = m["member_name"] or "Unknown"
            username = f"@{m['username']}" if m["username"] else "No username"
            course = repair_mojibake(m["channel_name"] or "Course")
            lines.append(
                f"ð¤ <b>{escape(display)}</b> ({escape(username)})\n"
                f"ð <code>{m['user_id']}</code>\n"
                f"ð {escape(course)}\n"
                f"Joined: {escape(m['joined_at'] or '-')} | "
                f"Status: <b>{escape(m['status'] or 'active')}</b>\n"
            )
    else:
        lines.append("No demo members yet.")

    await q.answer()
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{BACK} Home", callback_data="home")]]
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# =========================================================
# EDIT / DELETE CHANNEL
# =========================================================
async def edit_name_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return ConversationHandler.END

    channel_id = int(q.data.split("_")[-1])
    context.user_data["edit_channel"] = channel_id
    await q.answer()

    await q.edit_message_text(
        f"{EDIT} Send new channel name:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                f"{CROSS} Cancel",
                callback_data=f"channel_{channel_id}"
            )]]
        ),
    )
    return ADD_CHANNEL


async def edit_name_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    channel_id = context.user_data.get("edit_channel")
    name = update.message.text.strip()

    if not name:
        await update.message.reply_text("Name cannot be empty.")
        return ADD_CHANNEL

    with db() as con:
        con.execute(
            "UPDATE channels SET name=? WHERE id=?",
            (name, channel_id),
        )
        con.commit()

    await update.message.reply_text(f"{CHECK} Channel name updated.")
    return ConversationHandler.END


async def edit_id_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return ConversationHandler.END

    channel_id = int(q.data.split("_")[-1])
    context.user_data["edit_channel"] = channel_id
    await q.answer()

    await q.edit_message_text(
        f"{EDIT} Send new channel ID:\n\n"
        "<code>-1001234567890</code>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                f"{CROSS} Cancel",
                callback_data=f"channel_{channel_id}"
            )]]
        ),
        parse_mode=ParseMode.HTML,
    )
    return ADD_CHANNEL


async def edit_id_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    channel_id = context.user_data.get("edit_channel")
    tg_id = update.message.text.strip()

    if not re.fullmatch(r"-100\d+", tg_id):
        await update.message.reply_text("Invalid channel ID.")
        return ADD_CHANNEL

    try:
        await context.bot.get_chat(int(tg_id))
    except Exception:
        await update.message.reply_text(
            "Bot cannot access this channel."
        )
        return ADD_CHANNEL

    with db() as con:
        try:
            con.execute(
                "UPDATE channels SET tg_id=? WHERE id=?",
                (tg_id, channel_id),
            )
            con.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                "This channel ID is already registered."
            )
            return ADD_CHANNEL

    await update.message.reply_text(f"{CHECK} Channel ID updated.")
    return ConversationHandler.END


async def delete_channel(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return

    channel_id = int(q.data.split("_")[-1])

    with db() as con:
        row = con.execute(
            "SELECT batch_id FROM channels WHERE id=?",
            (channel_id,),
        ).fetchone()

        if not row:
            await q.answer("Not found.")
            return

        con.execute(
            "DELETE FROM channels WHERE id=?",
            (channel_id,),
        )
        con.commit()

    await q.answer("Deleted.")
    await batch_callback(
        Update(update.update_id, callback_query=q),
        context,
    )


# =========================================================
# INLINE SEARCH
# @BotName kalam
# Shows channels first, then categories.
# =========================================================
async def inline_search(update, context):
    """
    @BotUsername course-name

    Demo-access users see Demo Link.
    Users with demo,perm ("both") see Demo + Permanent.
    """
    query = update.inline_query
    uid = query.from_user.id

    if not is_admin(uid):
        await query.answer([], cache_time=0, is_personal=True)
        return

    term = (query.query or "").strip().lower()

    with db() as con:
        if term:
            rows = con.execute(
                """
                SELECT c.id, c.name, b.emoji
                FROM channels c
                JOIN batches b ON b.id = c.batch_id
                WHERE LOWER(c.name) LIKE ?
                ORDER BY c.name COLLATE NOCASE
                LIMIT 50
                """,
                (f"%{term}%",),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT c.id, c.name, b.emoji
                FROM channels c
                JOIN batches b ON b.id = c.batch_id
                ORDER BY c.name COLLATE NOCASE
                LIMIT 50
                """
            ).fetchall()

    results = []

    for row in rows:
        course_name = repair_mojibake(row["name"]) or "Course"
        emoji = repair_mojibake(row["emoji"]) or BOOK

        results.append(
            InlineQueryResultArticle(
                id=f"course_{row['id']}",
                title=f"{emoji} {course_name}",
                description="Tap to open control panel",
                # Telegram inline results support a thumbnail image.
                # This keeps the course list looking like the reference
                # screenshot, with a book icon on the left.
                thumbnail_url="https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f4da.png",
                thumbnail_width=72,
                thumbnail_height=72,
                input_message_content=InputTextMessageContent(
                    f"{emoji} <b>{escape(course_name)}</b>\n\n"
                    "Choose an access link below:",
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=inline_course_keyboard(
                    uid,
                    int(row["id"]),
                ),
            )
        )

    await query.answer(
        results,
        cache_time=0,
        is_personal=True,
    )


def inline_course_keyboard(uid, channel_id):
    buttons = []

    if has_permission(uid, "demo"):
        buttons.append(
            InlineKeyboardButton(
                f"{LINK} Demo Link",
                callback_data=f"inline_demo_{channel_id}",
            )
        )

    if has_permission(uid, "perm"):
        buttons.append(
            InlineKeyboardButton(
                f"{USER} Permanent Link",
                callback_data=f"inline_perm_{channel_id}",
            )
        )

    if not buttons:
        buttons.append(
            InlineKeyboardButton(
                "â No link access",
                callback_data=f"inline_no_access_{channel_id}",
            )
        )

    return InlineKeyboardMarkup(grid(buttons, 2))


async def inline_link_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    data = q.data

    try:
        channel_id = int(data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await q.answer("Invalid course.", show_alert=True)
        return

    link_type = "demo" if data.startswith("inline_demo_") else "perm"

    if not has_permission(uid, link_type):
        await q.answer("Permission denied.", show_alert=True)
        return

    with db() as con:
        ch = con.execute(
            "SELECT * FROM channels WHERE id=?",
            (channel_id,),
        ).fetchone()

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
                name=f"demo_{now.strftime('%Y%m%d_%H%M%S')}",
            )

            with db() as con:
                con.execute(
                    """
                    INSERT OR REPLACE INTO demo_links
                    (invite_link,chat_id,channel_name,used)
                    VALUES(?,?,?,0)
                    """,
                    (invite.invite_link, str(chat_id), ch["name"]),
                )
                con.execute(
                    """
                    INSERT INTO records
                    (admin_uid,admin_name,channel_name,link_type,link_url,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        uid,
                        "Owner" if is_owner(uid) else f"Admin {uid}",
                        ch["name"],
                        "DEMO",
                        invite.invite_link,
                        now.strftime("%d/%m/%Y %H:%M"),
                    ),
                )
                con.commit()

            log_activity(
                "DEMO_LINK",
                link_type="DEMO",
                admin_uid=uid,
                admin_name="Owner" if is_owner(uid) else f"Admin {uid}",
                channel_name=ch["name"],
                invite_link=invite.invite_link,
                details=f"Demo time: {get_demo_minutes()} minutes",
            )

            minutes = get_demo_minutes()
            course_name = repair_mojibake(ch["name"]) or "Course"
            view_link = make_view_link(ch["tg_id"])
            text = (
                "ð <b>Access Granted: Demo Pass</b> â³\n"
                "ââââââââââââââââââ\n\n"
                "ð« <b>à¤à¥à¤¨à¤² / à¤à¥à¤°à¥à¤¸ à¤à¤¾ à¤¨à¤¾à¤®:</b>\n"
                f"â¡ï¸ {escape(course_name)}\n\n"
                "ð¥ <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
                f"ð <a href=\"{escape(invite.invite_link, quote=True)}\">{escape(invite.invite_link)}</a>\n\n"
                "ð¥ï¸ <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤¯à¤¹à¤¾à¤ à¤¦à¥à¤à¥à¤ (View Link):</b>\n"
                f"ð <a href=\"{escape(view_link, quote=True)}\">{escape(view_link)}</a>\n\n"
                "ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶ (Important):</b>\n"
                "â ï¸ à¤¯à¤¹ Joining Link à¤à¥à¤µà¤² 1 à¤¬à¤¾à¤° à¤à¤¾à¤® à¤à¤°à¥à¤à¤¾à¥¤ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¤¤à¥ à¤¹à¥ à¤¯à¤¹ à¤¬à¤à¤¦ à¤¹à¥ à¤à¤¾à¤à¤à¤¾à¥¤\n"
                f"â±ï¸ à¤¸à¤¿à¤¸à¥à¤à¤® à¤à¤ªà¤à¥ à¤à¥à¤¨à¤² à¤®à¥à¤ à¤à¥à¤¡à¤¼à¤¨à¥ à¤à¥ à¤ à¥à¤ <b>{minutes} à¤®à¤¿à¤¨à¤</b> à¤¬à¤¾à¤¦ à¤à¤à¥à¤®à¥à¤à¤¿à¤ à¤¬à¤¾à¤¹à¤° (Kick) à¤à¤° à¤¦à¥à¤à¤¾à¥¤\n\n"
                "â à¤à¥à¤ªà¤¯à¤¾ à¤à¤à¥ à¤¸à¥ à¤à¥à¤²à¤¾à¤¸ à¤¦à¥à¤à¤¨à¥ à¤à¥ à¤²à¤¿à¤ à¤¹à¤®à¥à¤¶à¤¾ à¤à¤ªà¤° à¤¦à¤¿à¤ à¤à¤ <b>'à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤²à¤¿à¤à¤'</b> à¤à¤¾ à¤¹à¥ à¤à¤ªà¤¯à¥à¤ à¤à¤°à¥à¤à¥¤"
            )

        else:
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                member_limit=1,
                name=f"perm_{now.strftime('%Y%m%d_%H%M%S')}",
            )

            with db() as con:
                con.execute(
                    """
                    INSERT INTO records
                    (admin_uid,admin_name,channel_name,link_type,link_url,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        uid,
                        "Owner" if is_owner(uid) else f"Admin {uid}",
                        ch["name"],
                        "PERM",
                        invite.invite_link,
                        now.strftime("%d/%m/%Y %H:%M"),
                    ),
                )
                con.commit()

            log_activity(
                "PERM_LINK",
                link_type="PERM",
                admin_uid=uid,
                admin_name="Owner" if is_owner(uid) else f"Admin {uid}",
                channel_name=ch["name"],
                invite_link=invite.invite_link,
                details="Permanent one-use invite generated",
            )

            course_name = repair_mojibake(ch["name"]) or "Course"
            view_link = make_view_link(ch["tg_id"])
            text = (
                "ð <b>Access Granted: Permanent Pass</b> ð\n"
                "ââââââââââââââââââ\n\n"
                "ð« <b>à¤à¥à¤¨à¤² / à¤à¥à¤°à¥à¤¸ à¤à¤¾ à¤¨à¤¾à¤®:</b>\n"
                f"â¡ï¸ {escape(course_name)}\n\n"
                "ð¥ <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤ (Joining Link):</b>\n"
                f"ð <a href=\"{escape(invite.invite_link, quote=True)}\">{escape(invite.invite_link)}</a>\n\n"
                "ð¥ï¸ <b>à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤¯à¤¹à¤¾à¤ à¤¦à¥à¤à¥à¤ (View Link):</b>\n"
                f"ð <a href=\"{escape(view_link, quote=True)}\">{escape(view_link)}</a>\n\n"
                "ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶ (Important):</b>\n"
                "ð« à¤¯à¤¹ Permanent Joining Link à¤à¥à¤µà¤² à¤à¤ à¤¬à¤¾à¤° à¤à¥ à¤²à¤¿à¤ à¤¹à¥à¥¤ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¤¤à¥ à¤¹à¥ à¤¯à¤¹ Expire à¤¹à¥ à¤à¤¾à¤à¤à¤¾à¥¤\n\n"
                "â à¤à¥à¤ªà¤¯à¤¾ à¤à¤à¥ à¤¸à¥ à¤à¥à¤²à¤¾à¤¸ à¤¦à¥à¤à¤¨à¥ à¤à¥ à¤²à¤¿à¤ à¤¹à¤®à¥à¤¶à¤¾ à¤à¤ªà¤° à¤¦à¤¿à¤ à¤à¤ <b>'à¤à¥à¤°à¥à¤¸ à¤®à¤à¥à¤°à¤¿à¤¯à¤² à¤²à¤¿à¤à¤'</b> à¤à¤¾ à¤¹à¥ à¤à¤ªà¤¯à¥à¤ à¤à¤°à¥à¤à¥¤"
            )

        await q.answer()
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    f"{BACK} Back",
                    callback_data=f"inline_back_{channel_id}",
                )]]
            ),
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        logger.exception("Inline link generation failed")
        await q.answer(
            "Link create nahi hua. Channel me bot ki admin permissions check karo.",
            show_alert=True,
        )


async def inline_back_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    try:
        channel_id = int(q.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return

    with db() as con:
        ch = con.execute(
            "SELECT id, name FROM channels WHERE id=?",
            (channel_id,),
        ).fetchone()

    if not ch:
        await q.answer("Course not found.", show_alert=True)
        return

    await q.answer()
    await q.edit_message_text(
        f"{BOOK} <b>{escape(ch['name'])}</b>\n\n"
        "Choose an access link below:",
        reply_markup=inline_course_keyboard(uid, channel_id),
        parse_mode=ParseMode.HTML,
    )

# =========================================================
# CALLBACK ROUTER
# =========================================================
async def callback_router(update, context):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    if data == "home":
        await q.answer()
        await q.edit_message_text(
            "Hello Seller Famaily Kese Ho\n\n"
            "I AM WIZARD ð¸ð\n\n"
            "ð <b>Course Search</b>\n"
            "Telegram ke kisi chat me likho:\n"
            "<code>@YourBotUsername course-name</code>\n\n"
            "Search results me apna course select karo.",
            reply_markup=home_keyboard(uid),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "owner":
        await owner_panel(update, context)
        return

    if data == "demo_time":
        await demotime_button(update, context)
        return

    if data == "admins":
        await list_admins(update, context)
        return

    if data == "records":
        await records_callback(update, context)
        return

    if data.startswith("batch_"):
        await batch_callback(update, context)
        return

    if data.startswith("inline_no_access_"):
        await q.answer(
            "Aapke account ko Demo/Permanent access nahi hai.",
            show_alert=True,
        )
        return

    if data.startswith("inline_demo_") or data.startswith("inline_perm_"):
        await inline_link_callback(update, context)
        return

    if data.startswith("inline_back_"):
        await inline_back_callback(update, context)
        return

    if data.startswith("channel_"):
        await channel_callback(update, context)
        return

    if data.startswith("gen_demo_") or data.startswith("gen_perm_"):
        await generate_link(update, context)
        return

    if data.startswith("delete_"):
        await delete_channel(update, context)
        return

    await q.answer()


# =========================================================
# MAIN
# =========================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add BOT_TOKEN in Railway Variables."
        )

    if not OWNER_ID:
        raise RuntimeError(
            "OWNER_ID is missing. Add OWNER_ID in Railway Variables."
        )

    init_db()
    normalize_permissions()
    repair_database()

    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('demo_minutes',?)",
            (str(DEMO_MINUTES),),
        )
        con.commit()

    app = Application.builder().token(BOT_TOKEN).build()

    # Background demo timer.
    if app.job_queue:
        app.job_queue.run_repeating(
            auto_ban_unban,
            interval=10,
            first=5,
        )
    else:
        raise RuntimeError(
            "JobQueue is unavailable. Install python-telegram-bot[job-queue]."
        )

    # Access / normal commands.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", start))
    app.add_handler(CommandHandler("demotime", demotime_command))

    # Silent random commands.
    app.add_handler(
        MessageHandler(filters.COMMAND, unknown_command)
    )

    # Auto-detect channel when bot becomes admin.
    app.add_handler(
        ChatMemberHandler(
            my_chat_member,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # Old-style demo join requests are still supported.
    app.add_handler(
        ChatJoinRequestHandler(handle_join_request)
    )

    # Record generated-link member joins (Permanent + Demo).
    app.add_handler(
        ChatMemberHandler(
            handle_member_access_event,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # New demo links are direct-join links. This handler starts the timer
    # exactly when Telegram reports that the member joined.
    app.add_handler(
        ChatMemberHandler(
            handle_demo_member_join,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # Inline search.
    app.add_handler(
        InlineQueryHandler(inline_search)
    )

    # Conversations.
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    add_admin_start,
                    pattern=r"^add_admin$",
                )
            ],
            states={
                ADD_ADMIN: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        add_admin_save,
                    )
                ]
            },
            fallbacks=[],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    edit_admin_start,
                    pattern=r"^edit_admin$",
                )
            ],
            states={
                EDIT_ADMIN: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        edit_admin_save,
                    )
                ]
            },
            fallbacks=[],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    remove_admin_start,
                    pattern=r"^remove_admin$",
                )
            ],
            states={
                REMOVE_ADMIN: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        remove_admin_save,
                    )
                ]
            },
            fallbacks=[],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    add_batch_start,
                    pattern=r"^add_batch$",
                )
            ],
            states={
                ADD_BATCH: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        add_batch_save,
                    )
                ]
            },
            fallbacks=[],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    add_channel_start,
                    pattern=r"^add_channel_\d+$",
                ),
                CallbackQueryHandler(
                    edit_name_start,
                    pattern=r"^edit_name_\d+$",
                ),
                CallbackQueryHandler(
                    edit_id_start,
                    pattern=r"^edit_id_\d+$",
                ),
            ],
            states={
                ADD_CHANNEL: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        add_channel_save,
                    )
                ]
            },
            fallbacks=[],
        )
    )

    # General callbacks LAST.
    app.add_handler(
        CallbackQueryHandler(callback_router)
    )

    logger.info("RAJ COURSE BOT started.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
