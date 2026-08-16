import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    InlineQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# =========================================================
# RAILWAY VARIABLES
# BOT_TOKEN=your_bot_token
# OWNER_ID=your_telegram_user_id
# DEMO_MINUTES=5
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DEMO_MINUTES = max(1, int(os.getenv("DEMO_MINUTES", "5") or 5))
DB_PATH = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("course-bot")

BOOK = "\U0001f4da"
FLAG = "\U0001f6a9"
NOTE = "\U0001f4dd"
GRAD = "\U0001f393"
SPARK = "\u2728"
CROWN = "\U0001f451"
GEAR = "\u2699\ufe0f"
PLUS = "\u2795"
CROSS = "\u274c"
BACK = "\u2b05\ufe0f"
DEMO = "\u26a1"
PERM = "\U0001f48e"
USERS = "\U0001f465"
CHANNEL = "\U0001f4cc"
FOLDER = "\U0001f4c2"
CHECK = "\u2705"
REPORT = "\U0001f4ca"
USER = "\U0001f464"
EDIT = "\u270f\ufe0f"
TRASH = "\U0001f5d1\ufe0f"

DEFAULT_BATCHES = [
    (BOOK, "Teaching Exam's"),
    (FLAG, "Ras/Psi"),
    (NOTE, "EO-Ro/bstc/cet"),
    (GRAD, "Net-Jrf"),
    (SPARK, "Other Exam's"),
]

ADD_ADMIN, EDIT_ADMIN, REMOVE_ADMIN, ADD_BATCH, ADD_CHANNEL = range(5)


# =========================================================
# DATABASE
# =========================================================
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
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

        CREATE TABLE IF NOT EXISTS access_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_uid INTEGER NOT NULL,
            seller_name TEXT NOT NULL,
            member_uid INTEGER NOT NULL,
            member_name TEXT,
            username TEXT,
            channel_name TEXT NOT NULL,
            link_type TEXT NOT NULL,
            invite_link TEXT,
            joined_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'joined'
        );

        CREATE INDEX IF NOT EXISTS idx_access_events_member
            ON access_events(member_uid);
        CREATE INDEX IF NOT EXISTS idx_access_events_seller
            ON access_events(seller_uid);
        CREATE INDEX IF NOT EXISTS idx_access_events_invite
            ON access_events(invite_link);
        """)

        cols = {
            r["name"]
            for r in con.execute("PRAGMA table_info(demo_users)").fetchall()
        }
        for col, definition in (
            ("member_name", "TEXT"),
            ("username", "TEXT"),
            ("joined_at", "TEXT"),
        ):
            if col not in cols:
                con.execute(
                    f"ALTER TABLE demo_users ADD COLUMN {col} {definition}"
                )

        for emoji, name in DEFAULT_BATCHES:
            con.execute(
                "INSERT OR IGNORE INTO batches(emoji,name) VALUES(?,?)",
                (emoji, name),
            )

        if OWNER_ID:
            con.execute(
                """
                INSERT INTO admins(uid,name,permissions)
                VALUES(?, 'Owner', 'all')
                ON CONFLICT(uid) DO UPDATE SET
                    name='Owner', permissions='all'
                """,
                (OWNER_ID,),
            )

        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('demo_minutes',?)",
            (str(DEMO_MINUTES),),
        )
        con.commit()


def get_demo_minutes():
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key='demo_minutes'"
        ).fetchone()
    try:
        return max(1, int(row["value"])) if row else DEMO_MINUTES
    except (TypeError, ValueError):
        return DEMO_MINUTES


def set_demo_minutes(minutes):
    minutes = max(1, min(int(minutes), 1440))
    with db() as con:
        con.execute(
            """
            INSERT INTO settings(key,value) VALUES('demo_minutes',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(minutes),),
        )
        con.commit()


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
            "SELECT 1 FROM admins WHERE uid=?", (uid,)
        ).fetchone() is not None


def has_permission(uid, permission):
    if is_owner(uid):
        return True

    with db() as con:
        row = con.execute(
            "SELECT permissions FROM admins WHERE uid=?", (uid,)
        ).fetchone()

    if not row:
        return False

    perms = {
        x.strip().lower()
        for x in str(row["permissions"]).replace(" ", "").split(",")
        if x.strip()
    }

    if "all" in perms:
        return True
    if permission == "demo":
        return "demo" in perms
    if permission == "perm":
        return bool({"perm", "permanent", "both"} & perms)
    return permission in perms


def rows_to_grid(items, cols=2):
    return [items[i:i + cols] for i in range(0, len(items), cols)]


# =========================================================
# HOME
# =========================================================
def home_keyboard(uid):
    # Public main menu. Owner/admin commands are intentionally hidden
    # and are available only when typed directly.
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "\U0001f4ca \u092e\u0947\u0930\u0947 \u091c\u0941\u0921\u093c\u0947 \u0939\u0941\u090f \u091a\u0948\u0928\u0932",
                callback_data="my_channels",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f50e \u091a\u0948\u0928\u0932 \u0938\u0930\u094d\u091a \u0915\u0930\u0947\u0902",
                switch_inline_query_current_chat="",
            )
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /start is public: every user gets the same premium main menu.
    if not update.effective_user:
        return

    await update.effective_message.reply_text(
        "\U0001f44b <b>\u092e\u0947\u0928 \u092e\u0947\u0928\u094d\u092f\u0942 (Main Menu):</b>\n\n"
        "\u0905\u092a\u0928\u0947 \u0921\u0948\u0936\u092c\u094b\u0930\u094d\u0921 \u0915\u094b \u091a\u0932\u093e\u0928\u0947 \u0915\u0947 \u0932\u093f\u090f \u0928\u0940\u091a\u0947 \u0926\u093f\u090f \u0917\u090f \u092c\u091f\u0928 \u0926\u092c\u093e\u090f\u0901:",
        reply_markup=home_keyboard(update.effective_user.id),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# BATCH / CHANNEL
# =========================================================
def channel_keyboard(uid, ch):
    buttons = []
    if has_permission(uid, "demo"):
        buttons.append(InlineKeyboardButton(
            f"{DEMO} Demo", callback_data=f"gen_demo_{ch['id']}"
        ))
    if has_permission(uid, "perm"):
        buttons.append(InlineKeyboardButton(
            f"{PERM} Permanent", callback_data=f"gen_perm_{ch['id']}"
        ))

    kb = rows_to_grid(buttons, 2)

    if is_owner(uid):
        kb += [
            [
                InlineKeyboardButton(
                    f"{EDIT} Edit Name",
                    callback_data=f"edit_name_{ch['id']}"
                ),
                InlineKeyboardButton(
                    f"{EDIT} Edit ID",
                    callback_data=f"edit_id_{ch['id']}"
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{TRASH} Delete",
                    callback_data=f"delete_{ch['id']}"
                )
            ],
        ]

    kb.append([
        InlineKeyboardButton(
            f"{BACK} Back",
            callback_data=f"batch_{ch['batch_id']}"
        )
    ])
    return InlineKeyboardMarkup(kb)


async def show_batch(update, context, batch_id):
    q = update.callback_query
    uid = q.from_user.id

    with db() as con:
        batch = con.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        channels = con.execute(
            """
            SELECT id,name
            FROM channels
            WHERE batch_id=?
            ORDER BY name COLLATE NOCASE
            """,
            (batch_id,),
        ).fetchall()

    if not batch:
        await q.answer("Category not found.", show_alert=True)
        return

    buttons = [
        InlineKeyboardButton(
            f"{BOOK} {c['name']}", callback_data=f"channel_{c['id']}"
        )
        for c in channels
    ]
    kb = rows_to_grid(buttons, 1)

    if is_owner(uid):
        kb.append([
            InlineKeyboardButton(
                f"{PLUS} Add Channel",
                callback_data=f"add_channel_{batch_id}"
            )
        ])

    kb.append([InlineKeyboardButton(
        f"{BACK} Home", callback_data="home"
    )])

    await q.answer()
    await q.edit_message_text(
        f"{batch['emoji']} <b>{escape(batch['name'])}</b>\n\n"
        f"{CHANNEL} Channels: {len(channels)}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


async def show_channel(update, context, channel_id):
    q = update.callback_query
    uid = q.from_user.id

    with db() as con:
        ch = con.execute(
            "SELECT * FROM channels WHERE id=?", (channel_id,)
        ).fetchone()

    if not ch:
        await q.answer("Channel not found.", show_alert=True)
        return

    await q.answer()
    await q.edit_message_text(
        f"{BOOK} <b>{escape(ch['name'])}</b>\n\n"
        f"{CHANNEL} <code>{escape(ch['tg_id'])}</code>",
        reply_markup=channel_keyboard(uid, ch),
        parse_mode=ParseMode.HTML,
    )


async def resolve_seller_name(bot, uid):
    try:
        chat = await bot.get_chat(uid)
        name = (getattr(chat, "full_name", None) or "").strip()
        if not name:
            first = (getattr(chat, "first_name", None) or "").strip()
            last = (getattr(chat, "last_name", None) or "").strip()
            name = " ".join(x for x in (first, last) if x)
        if name:
            return name
        username = (getattr(chat, "username", None) or "").strip()
        return f"@{username}" if username else f"Seller {uid}"
    except Exception:
        return f"Seller {uid}"


# =========================================================
# HIDDEN OWNER COMMANDS
# /owner    -> owner panel
# /addsudo ID -> demo only
# /addsuper ID -> demo + permanent
# /rmsudo ID -> remove access
# These are deliberately not added to Telegram's command menu.
# =========================================================
def _owner_command_args(context):
    args = context.args or []
    if len(args) != 1:
        return None
    try:
        uid = int(args[0].strip())
        return uid if uid > 0 else None
    except (TypeError, ValueError):
        return None


async def owner_command(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    with db() as con:
        admins = con.execute("SELECT uid FROM admins").fetchall()
        batches = con.execute("SELECT id FROM batches").fetchall()
        channels = con.execute(
            "SELECT COUNT(*) n FROM channels"
        ).fetchone()["n"]

    kb = [
        [
            InlineKeyboardButton(
                "\u2795 Add Category", callback_data="add_batch"
            ),
            InlineKeyboardButton(
                "\U0001f465 Admins", callback_data="admins"
            ),
        ],
        [
            InlineKeyboardButton(
                "\U0001f4ca User Report", callback_data="user_report"
            ),
            InlineKeyboardButton(
                "\u23f1\ufe0f Demo Time", callback_data="demo_time"
            ),
        ],
        [
            InlineKeyboardButton(
                "\U0001f4c2 My Channels", callback_data="my_channels"
            )
        ],
    ]

    await update.effective_message.reply_text(
        f"{CROWN} <b>OWNER PANEL</b>\n\n"
        f"{USERS} Sellers: <b>{max(0, len(admins)-1)}</b>\n"
        f"{FOLDER} Categories: <b>{len(batches)}</b>\n"
        f"{CHANNEL} Channels: <b>{channels}</b>\n"
        f"\u23f1\ufe0f Demo: <b>{get_demo_minutes()} min</b>\n\n"
        "<i>Hidden access commands:</i>\n"
        "<code>/addsudo USER_ID</code>\n"
        "<code>/addsuper USER_ID</code>\n"
        "<code>/rmsudo USER_ID</code>",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


async def addsudo_command(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    uid = _owner_command_args(context)
    if uid is None or uid == OWNER_ID:
        return

    seller_name = await resolve_seller_name(context.bot, uid)

    with db() as con:
        con.execute(
            """
            INSERT INTO admins(uid,name,permissions)
            VALUES(?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                name=excluded.name,
                permissions='demo'
            """,
            (uid, seller_name, "demo"),
        )
        con.commit()

    await update.effective_message.reply_text(
        f"\u2705 <b>DEMO ACCESS ACTIVE</b>\n\n"
        f"Seller ID: <code>{uid}</code>\n"
        "Access: <b>Demo only</b>",
        parse_mode=ParseMode.HTML,
    )


async def addsuper_command(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    uid = _owner_command_args(context)
    if uid is None or uid == OWNER_ID:
        return

    seller_name = await resolve_seller_name(context.bot, uid)

    with db() as con:
        con.execute(
            """
            INSERT INTO admins(uid,name,permissions)
            VALUES(?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                name=excluded.name,
                permissions='demo,perm'
            """,
            (uid, seller_name, "demo,perm"),
        )
        con.commit()

    await update.effective_message.reply_text(
        f"\u2705 <b>SUPER ACCESS ACTIVE</b>\n\n"
        f"Seller ID: <code>{uid}</code>\n"
        "Access: <b>Demo + Permanent</b>",
        parse_mode=ParseMode.HTML,
    )


async def rmsudo_command(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    uid = _owner_command_args(context)
    if uid is None or uid == OWNER_ID:
        return

    with db() as con:
        con.execute("DELETE FROM admins WHERE uid=?", (uid,))
        con.commit()

    await update.effective_message.reply_text(
        f"\u2705 <b>ACCESS REMOVED</b>\n\n"
        f"Seller ID: <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# OWNER
# =========================================================
async def owner_panel(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    with db() as con:
        admins = con.execute("SELECT uid FROM admins").fetchall()
        batches = con.execute("SELECT id FROM batches").fetchall()
        channels = con.execute(
            "SELECT COUNT(*) n FROM channels"
        ).fetchone()["n"]

    kb = [
        [
            InlineKeyboardButton(
                f"{PLUS} Add Admin", callback_data="add_admin"
            ),
            InlineKeyboardButton(
                f"{EDIT} Edit Access", callback_data="edit_admin"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{TRASH} Remove Admin", callback_data="remove_admin"
            ),
            InlineKeyboardButton(
                f"{PLUS} New Category", callback_data="add_batch"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{USERS} View Admins", callback_data="admins"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f4ca User Report", callback_data="user_report"
            ),
            InlineKeyboardButton(
                "\u23f1\ufe0f Demo Time", callback_data="demo_time"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{BACK} Home", callback_data="home"
            )
        ],
    ]

    await q.answer()
    await q.edit_message_text(
        f"{CROWN} <b>OWNER PANEL</b>\n\n"
        f"{USERS} Admins: {len(admins)}\n"
        f"{FOLDER} Categories: {len(batches)}\n"
        f"{CHANNEL} Channels: {channels}\n"
        f"\u23f1\ufe0f Demo: <b>{get_demo_minutes()} minutes</b>",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
    )


async def list_admins(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    with db() as con:
        rows = con.execute(
            "SELECT uid,name,permissions FROM admins ORDER BY uid"
        ).fetchall()

    text = f"{USERS} <b>ADMINS</b>\n\n"
    for r in rows:
        role = "OWNER" if r["uid"] == OWNER_ID else "ADMIN"
        text += (
            f"{USER} {escape(r['name'])}\n"
            f"ID: <code>{r['uid']}</code>\n"
            f"Access: <code>{escape(r['permissions'])}</code> | {role}\n\n"
        )

    await q.answer()
    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"{BACK} Owner Panel", callback_data="owner"
            )
        ]]),
        parse_mode=ParseMode.HTML,
    )


async def demotime_command(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            f"\u23f1\ufe0f Current Demo Time: {get_demo_minutes()} minutes\n"
            "Change: /demotime 5"
        )
        return

    try:
        minutes = int(args[0])
        if not 1 <= minutes <= 1440:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "\u274c Demo time 1-1440 minutes \u0939\u094b\u0928\u093e \u091a\u093e\u0939\u093f\u090f."
        )
        return

    set_demo_minutes(minutes)
    await update.effective_message.reply_text(
        f"{CHECK} Demo Time updated: {minutes} minutes"
    )


async def demotime_button(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    await q.answer()
    await q.edit_message_text(
        f"\u23f1\ufe0f <b>DEMO TIME</b>\n\n"
        f"Current: <b>{get_demo_minutes()} minutes</b>\n\n"
        "Change:\n"
        "<code>/demotime 5</code>\n"
        "<code>/demotime 10</code>\n"
        "<code>/demotime 30</code>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"{BACK} Owner Panel", callback_data="owner"
            )
        ]]),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# ADMIN / CATEGORY / CHANNEL CONVERSATIONS
# =========================================================
async def add_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{PLUS} <b>ADD ADMIN</b>\n\n"
        "<code>123456789 | demo</code>\n"
        "or\n"
        "<code>123456789 | both</code>",
        parse_mode=ParseMode.HTML,
    )
    return ADD_ADMIN


async def add_admin_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid_text, access = [
            x.strip() for x in update.message.text.split("|", 1)
        ]
        uid = int(uid_text)

        if uid <= 0 or uid == OWNER_ID:
            raise ValueError

        access = access.lower()
        if access == "demo":
            permissions = "demo"
        elif access in ("both", "perm", "permanent"):
            permissions = "demo,perm"
        else:
            await update.message.reply_text(
                "\u274c Access demo \u092f\u093e both \u0939\u094b\u0928\u093e \u091a\u093e\u0939\u093f\u090f."
            )
            return ADD_ADMIN

        with db() as con:
            con.execute(
                """
                INSERT INTO admins(uid,name,permissions)
                VALUES(?,?,?)
                ON CONFLICT(uid) DO UPDATE SET
                    name=excluded.name,
                    permissions=excluded.permissions
                """,
                (uid, "Admin", permissions),
            )
            con.commit()

        await update.message.reply_text(
            f"{CHECK} Admin added. ID: {uid}"
        )
        return ConversationHandler.END

    except (ValueError, TypeError):
        await update.message.reply_text(
            "Format: 123456789 | demo"
        )
        return ADD_ADMIN


async def edit_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{EDIT} Send:\n"
        "<code>UserID | demo</code>\n"
        "<code>UserID | demo,perm</code>",
        parse_mode=ParseMode.HTML,
    )
    return EDIT_ADMIN


async def edit_admin_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid_text, perms = [
            x.strip() for x in update.message.text.split("|", 1)
        ]
        uid = int(uid_text)

        if uid == OWNER_ID:
            await update.message.reply_text(
                "Owner access cannot be edited."
            )
            return EDIT_ADMIN

        with db() as con:
            if not con.execute(
                "SELECT 1 FROM admins WHERE uid=?", (uid,)
            ).fetchone():
                await update.message.reply_text("Admin not found.")
                return EDIT_ADMIN

            con.execute(
                "UPDATE admins SET permissions=? WHERE uid=?",
                (perms.lower(), uid),
            )
            con.commit()

        await update.message.reply_text(
            f"{CHECK} Access updated."
        )
        return ConversationHandler.END

    except (ValueError, TypeError):
        await update.message.reply_text("Invalid format.")
        return EDIT_ADMIN


async def remove_admin_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{TRASH} Send Admin User ID:"
    )
    return REMOVE_ADMIN


async def remove_admin_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid = int(update.message.text.strip())
        if uid == OWNER_ID:
            await update.message.reply_text(
                "Owner cannot be removed."
            )
            return REMOVE_ADMIN

        with db() as con:
            con.execute("DELETE FROM admins WHERE uid=?", (uid,))
            con.commit()

        await update.message.reply_text(
            f"{CHECK} Access removed."
        )
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("Invalid User ID.")
        return REMOVE_ADMIN


async def add_batch_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    await q.answer()
    await q.edit_message_text(
        f"{PLUS} <b>NEW CATEGORY</b>\n\n"
        "Send: <code>\U0001f4da | UPSC/IAS</code>",
        parse_mode=ParseMode.HTML,
    )
    return ADD_BATCH


async def add_batch_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    parts = [x.strip() for x in update.message.text.split("|", 1)]
    if len(parts) != 2 or not all(parts):
        await update.message.reply_text(
            "Format: Emoji | Category Name"
        )
        return ADD_BATCH

    with db() as con:
        try:
            con.execute(
                "INSERT INTO batches(emoji,name) VALUES(?,?)",
                tuple(parts),
            )
            con.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                "Category already exists."
            )
            return ADD_BATCH

    await update.message.reply_text(
        f"{CHECK} Category added."
    )
    return ConversationHandler.END


async def add_channel_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    batch_id = int(q.data.rsplit("_", 1)[1])
    context.user_data["batch_id"] = batch_id
    context.user_data.pop("edit_mode", None)

    await q.answer()
    await q.edit_message_text(
        f"{PLUS} <b>ADD CHANNEL</b>\n\n"
        "Send:\n"
        "<code>Channel Name | -1001234567890</code>",
        parse_mode=ParseMode.HTML,
    )
    return ADD_CHANNEL


async def edit_name_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    channel_id = int(q.data.rsplit("_", 1)[1])
    context.user_data["edit_channel"] = channel_id
    context.user_data["edit_mode"] = "name"

    await q.answer()
    await q.edit_message_text("\u270f\ufe0f Send new channel name:")
    return ADD_CHANNEL


async def edit_id_start(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return ConversationHandler.END

    channel_id = int(q.data.rsplit("_", 1)[1])
    context.user_data["edit_channel"] = channel_id
    context.user_data["edit_mode"] = "id"

    await q.answer()
    await q.edit_message_text(
        "\u270f\ufe0f Send new channel ID:\n\n"
        "<code>-1001234567890</code>",
        parse_mode=ParseMode.HTML,
    )
    return ADD_CHANNEL


async def add_or_edit_channel_save(update, context):
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    mode = context.user_data.get("edit_mode")

    if mode == "name":
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

        await update.message.reply_text(
            f"{CHECK} Channel name updated."
        )
        return ConversationHandler.END

    if mode == "id":
        channel_id = context.user_data.get("edit_channel")
        tg_id = update.message.text.strip()

        if not re.fullmatch(r"-100\d+", tg_id):
            await update.message.reply_text("Invalid channel ID.")
            return ADD_CHANNEL

        try:
            chat = await context.bot.get_chat(int(tg_id))
            if chat.type != "channel":
                raise ValueError
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

        await update.message.reply_text(
            f"{CHECK} Channel ID updated."
        )
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
        await update.message.reply_text("Invalid channel ID.")
        return ADD_CHANNEL

    try:
        chat = await context.bot.get_chat(int(tg_id))
        if chat.type != "channel":
            await update.message.reply_text("This is not a channel.")
            return ADD_CHANNEL
    except Exception:
        await update.message.reply_text(
            "Bot cannot access this channel. Make bot admin first."
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
# AUTO-DETECT CHANNEL
# =========================================================
async def my_chat_member(update, context):
    cm = update.my_chat_member
    if not cm or not cm.chat or cm.chat.type != "channel":
        return

    if cm.new_chat_member.status != ChatMemberStatus.ADMINISTRATOR:
        return

    if cm.old_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        return

    with db() as con:
        batch = con.execute(
            "SELECT id FROM batches WHERE name=?",
            ("Other Exam's",),
        ).fetchone()

        if not batch:
            con.execute(
                "INSERT INTO batches(emoji,name) VALUES(?,?)",
                (SPARK, "Other Exam's"),
            )
            con.commit()
            batch = con.execute(
                "SELECT id FROM batches WHERE name=?",
                ("Other Exam's",),
            ).fetchone()

        con.execute(
            """
            INSERT INTO channels(batch_id,name,tg_id)
            VALUES(?,?,?)
            ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name
            """,
            (
                batch["id"],
                cm.chat.title or "Telegram Channel",
                str(cm.chat.id),
            ),
        )
        con.commit()

    try:
        await context.bot.send_message(
            OWNER_ID,
            f"{CHECK} <b>Channel Auto Added</b>\n\n"
            f"{BOOK} {escape(cm.chat.title or 'Telegram Channel')}\n"
            f"{CHANNEL} <code>{cm.chat.id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# =========================================================
# LINK CREATION
# =========================================================
async def create_link(channel_id, link_type, uid, bot):
    with db() as con:
        ch = con.execute(
            "SELECT * FROM channels WHERE id=?", (channel_id,)
        ).fetchone()

    if not ch:
        raise RuntimeError("Channel not found")

    chat_id = int(ch["tg_id"])
    now = datetime.now(timezone.utc)

    if link_type == "demo":
        invite = await bot.create_chat_invite_link(
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

        return invite, ch

    # Permanent link is reusable.
    invite = await bot.create_chat_invite_link(
        chat_id=chat_id,
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

    return invite, ch


async def generate_link(update, context):
    q = update.callback_query
    uid = q.from_user.id

    parts = q.data.split("_")
    if len(parts) != 3:
        await q.answer("Invalid request.", show_alert=True)
        return

    link_type = "demo" if parts[1] == "demo" else "perm"

    if not has_permission(uid, link_type):
        await q.answer("Permission denied.", show_alert=True)
        return

    try:
        channel_id = int(parts[2])
        invite, ch = await create_link(
            channel_id, link_type, uid, context.bot
        )

        if link_type == "demo":
            text = (
                f"{CHECK} <b>DEMO LINK</b>\n\n"
                f"{BOOK} <b>{escape(ch['name'])}</b>\n"
                f"\u23f1\ufe0f {get_demo_minutes()} minutes\n"
                "\U0001f6ab Expire \u0939\u094b\u0928\u0947 \u092a\u0930 Auto Remove + Auto Unban\n\n"
                f'<a href="{escape(invite.invite_link)}">'
                "\U0001f517 Open Demo Link</a>"
            )
        else:
            text = (
                f"{CHECK} <b>PERMANENT LINK</b>\n\n"
                f"{BOOK} <b>{escape(ch['name'])}</b>\n"
                "\u267e\ufe0f Reusable Permanent Invite\n\n"
                f'<a href="{escape(invite.invite_link)}">'
                "\U0001f517 Open Permanent Link</a>"
            )

        await q.answer()
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"{BACK} Back",
                    callback_data=f"channel_{channel_id}"
                )
            ]]),
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        log.exception("Link generation failed")
        await q.answer(
            "Link create nahi hua. Bot ko channel admin + invite/manage "
            "users permission do.",
            show_alert=True,
        )


# =========================================================
# DEMO JOIN TRACKING
# =========================================================
async def handle_demo_member_join(update, context):
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

    invite_url = invite.invite_link

    with db() as con:
        access_link = con.execute(
            """
            SELECT admin_uid, admin_name, channel_name, link_type
            FROM records
            WHERE link_url=?
            ORDER BY id DESC LIMIT 1
            """,
            (invite_url,),
        ).fetchone()

        row = con.execute(
            """
            SELECT *
            FROM demo_links
            WHERE invite_link=? AND used=0
            """,
            (invite_url,),
        ).fetchone()

        # Permanent invite: record the member and stop here.
        if access_link and access_link["link_type"] == "PERM" and not row:
            member = cm.new_chat_member.user
            joined_at = datetime.now(timezone.utc).isoformat()

            con.execute(
                """
                INSERT INTO access_events
                (
                    seller_uid,seller_name,member_uid,member_name,username,
                    channel_name,link_type,invite_link,joined_at,status
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    access_link["admin_uid"],
                    access_link["admin_name"],
                    member.id,
                    member.full_name or "Unknown",
                    member.username,
                    access_link["channel_name"],
                    "PERM",
                    invite_url,
                    joined_at,
                    "permanent_active",
                ),
            )
            con.commit()

            log.info(
                "Permanent access recorded: seller=%s member=%s channel=%s",
                access_link["admin_uid"], member.id, access_link["channel_name"]
            )
            return

        if not row:
            return

        member = cm.new_chat_member.user
        expire = (
            datetime.now(timezone.utc)
            + timedelta(minutes=get_demo_minutes())
        )

        # Mark the one-use demo link immediately.
        con.execute(
            "UPDATE demo_links SET used=1 WHERE invite_link=?",
            (invite_url,),
        )

        joined_at = datetime.now(timezone.utc).isoformat()

        con.execute(
            """
            INSERT INTO demo_users
            (
                user_id,chat_id,channel_name,ban_time,unban_time,
                status,member_name,username,joined_at
            )
            VALUES(?,?,?,?,?,'active',?,?,?)
            """,
            (
                member.id,
                str(cm.chat.id),
                row["channel_name"],
                expire.isoformat(),
                expire.isoformat(),
                member.full_name or "Unknown",
                member.username,
                joined_at,
            ),
        )

        seller = con.execute(
            """
            SELECT admin_uid, admin_name, channel_name, link_type
            FROM records
            WHERE link_url=?
            ORDER BY id DESC LIMIT 1
            """,
            (invite_url,),
        ).fetchone()

        if seller:
            con.execute(
                """
                INSERT INTO access_events
                (
                    seller_uid,seller_name,member_uid,member_name,username,
                    channel_name,link_type,invite_link,joined_at,status
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    seller["admin_uid"],
                    seller["admin_name"],
                    member.id,
                    member.full_name or "Unknown",
                    member.username,
                    seller["channel_name"],
                    seller["link_type"],
                    invite_url,
                    joined_at,
                    "demo_active",
                ),
            )

        con.commit()

    # Demo invite is one-use.
    try:
        await context.bot.revoke_chat_invite_link(
            chat_id=cm.chat.id,
            invite_link=invite_url,
        )
    except Exception:
        pass

    log.info(
        "Demo started: user=%s channel=%s expires=%s",
        member.id,
        cm.chat.id,
        expire.isoformat(),
    )


# =========================================================
# THE FIX:
# EXPIRE -> BAN/REMOVE -> WAIT 2 SEC -> UNBAN
# =========================================================
async def expire_demo_user(context, row):
    chat_id = int(row["chat_id"])
    user_id = int(row["user_id"])

    try:
        # 1. Remove the member by banning.
        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )
        log.info(
            "Demo expired: banned/removed user=%s chat=%s",
            user_id, chat_id
        )
    except Exception as exc:
        # If already left, still try the unban below.
        log.warning(
            "Ban/remove failed user=%s chat=%s: %s",
            user_id, chat_id, exc
        )

    # Give Telegram a moment to process the removal.
    await asyncio.sleep(2)

    try:
        # 2. IMPORTANT: immediately UNBAN.
        # This allows the same person to use a Permanent invite.
        await context.bot.unban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            only_if_banned=True,
        )
        log.info(
            "Demo user AUTO-UNBANNED user=%s chat=%s",
            user_id, chat_id
        )
        return True

    except Exception as exc:
        # Keep it active so a later worker run can retry.
        log.warning(
            "AUTO-UNBAN failed user=%s chat=%s: %s",
            user_id, chat_id, exc
        )
        return False


async def auto_ban_unban(context):
    """
    Runs every 10 seconds.

    Expired demo:
        BAN/REMOVE
             \u2193
          2 seconds
             \u2193
          UNBAN
             \u2193
    Permanent link can be used again.
    """
    now = datetime.now(timezone.utc)

    with db() as con:
        rows = con.execute(
            """
            SELECT *
            FROM demo_users
            WHERE status='active'
            """
        ).fetchall()

    for row in rows:
        try:
            expire_time = datetime.fromisoformat(row["ban_time"])

            if now < expire_time:
                continue

            success = await expire_demo_user(context, row)

            if success:
                with db() as con:
                    con.execute(
                        """
                        UPDATE demo_users
                        SET status='removed'
                        WHERE id=?
                        """,
                        (row["id"],),
                    )
                    con.execute(
                        """
                        UPDATE access_events
                        SET status='demo_expired'
                        WHERE member_uid=?
                          AND channel_name=?
                          AND link_type='DEMO'
                          AND status='demo_active'
                        """,
                        (row["user_id"], row["channel_name"]),
                    )
                    con.commit()

        except Exception:
            log.exception(
                "Demo expiry worker error for user=%s",
                row["user_id"],
            )


# =========================================================
# USER REPORT
# =========================================================
async def user_report_callback(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    with db() as con:
        events = con.execute(
            """
            SELECT seller_uid, seller_name, member_uid, member_name,
                   username, channel_name, link_type, joined_at, status
            FROM access_events
            ORDER BY id DESC
            LIMIT 60
            """
        ).fetchall()

        generated = con.execute(
            """
            SELECT admin_uid, admin_name, channel_name, link_type,
                   created_at
            FROM records
            ORDER BY id DESC
            LIMIT 30
            """
        ).fetchall()

    lines = [
        "\U0001f4ca <b>USER ACCESS REPORT</b>",
        "",
        "<b>\U0001f464 Member \u2192 Seller Mapping</b>",
    ]

    if events:
        for e in events:
            username = f"@{e['username']}" if e["username"] else "No username"
            lines.append(
                f"\n{DEMO if e['link_type']=='DEMO' else PERM} "
                f"<b>{escape(e['channel_name'])}</b> \u2014 "
                f"{escape(e['link_type'])}\n"
                f"\U0001f464 Member: <b>{escape(e['member_name'] or 'Unknown')}</b> "
                f"({escape(username)})\n"
                f"\U0001f194 Member ID: <code>{e['member_uid']}</code>\n"
                f"\U0001f9d1\u200d\U0001f4bc Seller: <b>{escape(e['seller_name'])}</b> "
                f"(<code>{e['seller_uid']}</code>)\n"
                f"\U0001f552 Joined: {escape(e['joined_at'])}\n"
                f"\U0001f4cc Status: <b>{escape(e['status'])}</b>"
            )
    else:
        lines.append("No member access recorded yet.")

    lines += ["", "<b>\U0001f517 Generated Links</b>"]

    for r in generated:
        lines.append(
            f"{DEMO if r['link_type']=='DEMO' else PERM} "
            f"{escape(r['channel_name'])} \u2014 "
            f"<b>{escape(r['link_type'])}</b>\n"
            f"\U0001f9d1\u200d\U0001f4bc Seller: {escape(r['admin_name'])} "
            f"(<code>{r['admin_uid']}</code>)\n"
            f"\U0001f552 {escape(r['created_at'])}"
        )

    report = "\n".join(lines)
    if len(report) > 3900:
        report = report[:3900] + "\n\n\u2026"

    await q.answer()
    await q.edit_message_text(
        report,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "\U0001f504 Refresh", callback_data="user_report"
                ),
                InlineKeyboardButton(
                    f"{BACK} Owner", callback_data="owner"
                ),
            ]
        ]),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# RECORDS
# =========================================================
async def records_callback(update, context):
    q = update.callback_query

    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    with db() as con:
        links = con.execute(
            """
            SELECT admin_name,channel_name,link_type,created_at
            FROM records
            ORDER BY id DESC
            LIMIT 30
            """
        ).fetchall()

        members = con.execute(
            """
            SELECT user_id,member_name,username,channel_name,
                   joined_at,status
            FROM demo_users
            ORDER BY id DESC
            LIMIT 30
            """
        ).fetchall()

    lines = [f"{REPORT} <b>RECORDS</b>\n", "<b>Generated Links</b>"]

    for r in links:
        icon = DEMO if r["link_type"] == "DEMO" else PERM
        lines.append(
            f"{icon} {escape(r['channel_name'])}\n"
            f"{USER} {escape(r['admin_name'])}\n"
            f"Time: {escape(r['created_at'])}\n"
        )

    lines.append("\n<b>Demo Members</b>")

    for m in members:
        username = f"@{m['username']}" if m["username"] else "No username"
        lines.append(
            f"{USER} <b>{escape(m['member_name'] or 'Unknown')}</b> "
            f"({escape(username)})\n"
            f"ID: <code>{m['user_id']}</code>\n"
            f"{BOOK} {escape(m['channel_name'])}\n"
            f"Joined: {escape(m['joined_at'] or '-')} | "
            f"Status: <b>{escape(m['status'])}</b>\n"
        )

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n\u2026"

    await q.answer()
    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"{BACK} Home", callback_data="home"
            )
        ]]),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# INLINE SEARCH
# =========================================================
async def inline_search(update, context):
    query = update.inline_query
    uid = query.from_user.id

    if not is_admin(uid):
        await query.answer(
            [], cache_time=0, is_personal=True
        )
        return

    term = (query.query or "").strip().lower()

    with db() as con:
        if term:
            rows = con.execute(
                """
                SELECT c.id,c.name,b.emoji
                FROM channels c
                JOIN batches b ON b.id=c.batch_id
                WHERE LOWER(c.name) LIKE ?
                ORDER BY c.name COLLATE NOCASE
                LIMIT 50
                """,
                (f"%{term}%",),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT c.id,c.name,b.emoji
                FROM channels c
                JOIN batches b ON b.id=c.batch_id
                ORDER BY c.name COLLATE NOCASE
                LIMIT 50
                """
            ).fetchall()

    results = []

    for r in rows:
        buttons = []

        if has_permission(uid, "demo"):
            buttons.append(InlineKeyboardButton(
                f"{DEMO} Demo",
                callback_data=f"inline_demo_{r['id']}"
            ))

        if has_permission(uid, "perm"):
            buttons.append(InlineKeyboardButton(
                f"{PERM} Permanent",
                callback_data=f"inline_perm_{r['id']}"
            ))

        results.append(
            InlineQueryResultArticle(
                id=f"course_{row['id']}",
                title=course_name,
                description=f"ID: {row['tg_id']}",
                input_message_content=InputTextMessageContent(
                    f"<b>{escape(course_name)}</b>\n"
                    f"<code>ID: {escape(str(row['tg_id']))}</code>",
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=inline_course_keyboard(
                    uid,
                    int(row["id"]),
                ),
            )
        )

    await query.answer(
        results, cache_time=0, is_personal=True
    )


async def inline_link_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    try:
        channel_id = int(q.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await q.answer("Invalid course.", show_alert=True)
        return

    link_type = (
        "demo"
        if q.data.startswith("inline_demo_")
        else "perm"
    )

    if not has_permission(uid, link_type):
        await q.answer("Permission denied.", show_alert=True)
        return

    try:
        invite, ch = await create_link(
            channel_id, link_type, uid, context.bot
        )

        if link_type == "demo":
            text = (
                f"{CHECK} <b>DEMO LINK</b>\n\n"
                f"{BOOK} <b>{escape(ch['name'])}</b>\n"
                f"\u23f1\ufe0f {get_demo_minutes()} minutes\n"
                "\U0001f6ab Expire \u2192 Auto Remove \u2192 Auto Unban\n\n"
                f'<a href="{escape(invite.invite_link)}">'
                "\U0001f517 Open Demo Link</a>"
            )
        else:
            text = (
                f"{CHECK} <b>PERMANENT LINK</b>\n\n"
                f"{BOOK} <b>{escape(ch['name'])}</b>\n"
                "\u267e\ufe0f Reusable Permanent Invite\n\n"
                f'<a href="{escape(invite.invite_link)}">'
                "\U0001f517 Open Permanent Link</a>"
            )

        await q.answer()
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"{BACK} Back",
                    callback_data=f"inline_back_{channel_id}"
                )
            ]]),
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        log.exception("Inline link generation failed")
        await q.answer(
            "Link create nahi hua. Channel admin permissions check karo.",
            show_alert=True,
        )


async def inline_back_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    try:
        channel_id = int(q.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return

    with db() as con:
        ch = con.execute(
            "SELECT * FROM channels WHERE id=?", (channel_id,)
        ).fetchone()

    if not ch:
        await q.answer("Course not found.", show_alert=True)
        return

    buttons = []
    if has_permission(uid, "demo"):
        buttons.append(InlineKeyboardButton(
            f"{DEMO} Demo",
            callback_data=f"inline_demo_{channel_id}"
        ))
    if has_permission(uid, "perm"):
        buttons.append(InlineKeyboardButton(
            f"{PERM} Permanent",
            callback_data=f"inline_perm_{channel_id}"
        ))

    await q.answer()
    await q.edit_message_text(
        f"{BOOK} <b>{escape(ch['name'])}</b>\n\n"
        "\u2728 Select access type:",
        reply_markup=InlineKeyboardMarkup(
            rows_to_grid(buttons, 2)
        ),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# DELETE
# =========================================================
async def delete_channel(update, context):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return

    channel_id = int(q.data.rsplit("_", 1)[1])

    with db() as con:
        row = con.execute(
            "SELECT batch_id FROM channels WHERE id=?",
            (channel_id,),
        ).fetchone()

        if not row:
            await q.answer("Not found.", show_alert=True)
            return

        batch_id = row["batch_id"]

        con.execute(
            "DELETE FROM channels WHERE id=?",
            (channel_id,),
        )
        con.commit()

    await q.answer("Deleted.")

    # Re-render category.
    q.data = f"batch_{batch_id}"
    await show_batch(update, context, batch_id)


# =========================================================
# MY CONNECTED CHANNELS
# =========================================================
async def my_channels_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if not is_admin(uid):
        await q.answer()
        return

    with db() as con:
        rows = con.execute(
            """
            SELECT c.id, c.name, c.tg_id, b.emoji
            FROM channels c
            JOIN batches b ON b.id = c.batch_id
            ORDER BY b.id, c.name COLLATE NOCASE
            """
        ).fetchall()

    buttons = [
        InlineKeyboardButton(
            f"{r['emoji']} {r['name']}",
            callback_data=f"channel_{r['id']}",
        )
        for r in rows
    ]

    kb = rows_to_grid(buttons, 1)
    kb.append([
        InlineKeyboardButton(
            "\U0001f50e \u091a\u0948\u0928\u0932 \u0938\u0930\u094d\u091a \u0915\u0930\u0947\u0902",
            switch_inline_query_current_chat="",
        )
    ])
    kb.append([
        InlineKeyboardButton(
            f"{BACK} Main Menu",
            callback_data="home",
        )
    ])

    await q.answer()
    await q.edit_message_text(
        f"\U0001f4ca <b>\u092e\u0947\u0930\u0947 \u091c\u0941\u0921\u093c\u0947 \u0939\u0941\u090f \u091a\u0948\u0928\u0932</b>\n\n"
        f"\u0915\u0941\u0932 \u091a\u0948\u0928\u0932: <b>{len(rows)}</b>",
        reply_markup=InlineKeyboardMarkup(kb),
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
            "\U0001f44b <b>\u092e\u0947\u0928 \u092e\u0947\u0928\u094d\u092f\u0942 (Main Menu):</b>\n\n"
            "\u0905\u092a\u0928\u0947 \u0921\u0948\u0936\u092c\u094b\u0930\u094d\u0921 \u0915\u094b \u091a\u0932\u093e\u0928\u0947 \u0915\u0947 \u0932\u093f\u090f \u0928\u0940\u091a\u0947 \u0926\u093f\u090f \u0917\u090f \u092c\u091f\u0928 \u0926\u092c\u093e\u090f\u0901:",
            reply_markup=home_keyboard(uid),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "owner":
        await owner_panel(update, context)
        return

    if data == "my_channels":
        await my_channels_callback(update, context)
        return

    if data == "admins":
        await list_admins(update, context)
        return

    if data == "demo_time":
        await demotime_button(update, context)
        return

    if data == "records":
        await records_callback(update, context)
        return

    if data == "user_report":
        await user_report_callback(update, context)
        return

    if data.startswith("batch_"):
        await show_batch(
            update, context, int(data.split("_", 1)[1])
        )
        return

    if data.startswith("channel_"):
        await show_channel(
            update, context, int(data.split("_", 1)[1])
        )
        return

    if data.startswith("gen_demo_") or data.startswith("gen_perm_"):
        await generate_link(update, context)
        return

    if data.startswith("inline_demo_") or data.startswith("inline_perm_"):
        await inline_link_callback(update, context)
        return

    if data.startswith("inline_back_"):
        await inline_back_callback(update, context)
        return

    if data.startswith("delete_"):
        await delete_channel(update, context)
        return

    await q.answer()


# =========================================================
# MAIN
# =========================================================
async def post_init(application):
    # Keep owner access commands hidden from Telegram's command menu.
    try:
        await application.bot.set_my_commands([])
    except Exception:
        log.exception("Could not clear Telegram command menu")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN missing. Railway Variables me BOT_TOKEN add karo."
        )
    if not OWNER_ID:
        raise RuntimeError(
            "OWNER_ID missing. Railway Variables me OWNER_ID add karo."
        )

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    if not app.job_queue:
        raise RuntimeError(
            "JobQueue unavailable. Install "
            "python-telegram-bot[job-queue]."
        )

    # Expiry checker: every 10 seconds.
    # EXPIRE -> BAN/REMOVE -> 2 sec -> UNBAN.
    app.job_queue.run_repeating(
        auto_ban_unban,
        interval=10,
        first=5,
    )

    app.add_handler(CommandHandler("start", start))

    # Hidden owner-only commands. Do NOT add these to set_my_commands.
    app.add_handler(CommandHandler("owner", owner_command))
    app.add_handler(CommandHandler("addsudo", addsudo_command))
    app.add_handler(CommandHandler("addsuper", addsuper_command))
    app.add_handler(CommandHandler("rmsudo", rmsudo_command))

    # Legacy owner/admin entry + demo time.
    app.add_handler(CommandHandler("admin", start))
    app.add_handler(CommandHandler("demotime", demotime_command))

    app.add_handler(InlineQueryHandler(inline_search))

    app.add_handler(ChatMemberHandler(
        my_chat_member,
        ChatMemberHandler.MY_CHAT_MEMBER,
    ))

    app.add_handler(ChatMemberHandler(
        handle_demo_member_join,
        ChatMemberHandler.CHAT_MEMBER,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(
            add_admin_start, pattern=r"^add_admin$"
        )],
        states={
            ADD_ADMIN: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_admin_save
            )]
        },
        fallbacks=[],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(
            edit_admin_start, pattern=r"^edit_admin$"
        )],
        states={
            EDIT_ADMIN: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                edit_admin_save
            )]
        },
        fallbacks=[],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(
            remove_admin_start, pattern=r"^remove_admin$"
        )],
        states={
            REMOVE_ADMIN: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                remove_admin_save
            )]
        },
        fallbacks=[],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(
            add_batch_start, pattern=r"^add_batch$"
        )],
        states={
            ADD_BATCH: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_batch_save
            )]
        },
        fallbacks=[],
    ))

    app.add_handler(ConversationHandler(
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
            ADD_CHANNEL: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_or_edit_channel_save
            )]
        },
        fallbacks=[],
    ))

    # General callbacks LAST.
    app.add_handler(
        CallbackQueryHandler(callback_router)
    )

    log.info(
        "Bot started. Demo time=%s minutes",
        get_demo_minutes()
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
