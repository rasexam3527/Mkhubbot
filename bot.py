import os
import html
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    InlineQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

try:
    IST = ZoneInfo("Asia/Kolkata") if ZoneInfo else timezone(timedelta(hours=5, minutes=30))
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))


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

        CREATE TABLE IF NOT EXISTS channels(
            chat_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            chat_type TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            bot_status TEXT NOT NULL DEFAULT 'administrator',
            can_invite INTEGER NOT NULL DEFAULT 0,
            can_ban INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS links(
            invite_link TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
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

        CREATE TABLE IF NOT EXISTS reports(
            report_date TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        );
        """)

        c.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('demo_minutes','5')"
        )

        if OWNER_ID:
            c.execute(
                """
                INSERT OR IGNORE INTO admins(uid,name,permissions)
                VALUES(?,?,?)
                """,
                (OWNER_ID, "Owner", "all"),
            )


init_db()


# ============================================================
# HELPERS
# ============================================================
def now():
    return datetime.now(timezone.utc)


def ist_now():
    return datetime.now(IST)


def iso(x):
    return x.isoformat() if x else None


def esc(x):
    return html.escape(str(x or ""))


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
        r = c.execute(
            "SELECT permissions FROM admins WHERE uid=?", (uid,)
        ).fetchone()
    if not r:
        return set()
    return {x.strip().lower() for x in r["permissions"].split(",") if x.strip()}


def can(uid, permission):
    p = perms(uid)
    return "all" in p or permission in p


def admin_name(uid):
    if owner(uid):
        return "Owner"
    with db() as c:
        r = c.execute(
            "SELECT name FROM admins WHERE uid=?", (uid,)
        ).fetchone()
    return r["name"] if r else f"Admin {uid}"


def user_name(u):
    name = " ".join(
        x for x in [u.first_name, u.last_name] if x
    ).strip()
    return name or (f"@{u.username}" if u.username else str(u.id))


def demo_minutes():
    with db() as c:
        r = c.execute(
            "SELECT value FROM settings WHERE key='demo_minutes'"
        ).fetchone()
    try:
        return int(r["value"])
    except Exception:
        return 5


def local_date(value=None):
    if value is None:
        return ist_now().date().isoformat()
    try:
        return datetime.fromisoformat(value).astimezone(IST).date().isoformat()
    except Exception:
        return str(value)[:10]


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(IST).strftime(
            "%d/%m/%Y %H:%M:%S"
        )
    except Exception:
        return str(value)


def rows_buttons(items, cols=2):
    return [items[i:i + cols] for i in range(0, len(items), cols)]


async def edit(q, text, kb=None):
    try:
        await q.edit_message_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("edit failed: %s", e)


# ============================================================
# CHANNEL AUTO-DETECTION
# ============================================================
async def save_detected_channel(bot, chat, bot_member=None):
    """Save/update a channel automatically when Telegram tells us
    that this bot is an administrator in that chat."""
    if not chat:
        return False

    chat_type = getattr(chat, "type", "")
    if chat_type not in ("channel", "supergroup"):
        return False

    title = getattr(chat, "title", None) or str(chat.id)
    username = getattr(chat, "username", None)

    status = "administrator"
    can_invite = 0
    can_ban = 0

    if bot_member:
        status = getattr(bot_member, "status", "administrator")
        can_invite = int(
            getattr(bot_member, "can_invite_users", False) is True
        )
        can_ban = int(
            getattr(bot_member, "can_restrict_members", False) is True
        )

    with db() as c:
        c.execute(
            """
            INSERT INTO channels(
                chat_id,title,username,chat_type,detected_at,
                bot_status,can_invite,can_ban
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                username=excluded.username,
                chat_type=excluded.chat_type,
                detected_at=excluded.detected_at,
                bot_status=excluded.bot_status,
                can_invite=excluded.can_invite,
                can_ban=excluded.can_ban
            """,
            (
                str(chat.id),
                title,
                username,
                chat_type,
                iso(now()),
                status,
                can_invite,
                can_ban,
            ),
        )
    return True


async def my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """THIS is the important auto-detect handler.

    When the bot is made admin in a channel, Telegram sends a
    my_chat_member update. No manual Add Course / channel ID is needed.
    """
    cm = update.my_chat_member
    if not cm:
        return

    chat = update.effective_chat
    new_status = getattr(cm.new_chat_member, "status", "")
    old_status = getattr(cm.old_chat_member, "status", "")

    if chat and new_status in ("administrator", "creator"):
        saved = await save_detected_channel(
            context.bot,
            chat,
            cm.new_chat_member,
        )
        if saved and OWNER_ID:
            try:
                await context.bot.send_message(
                    OWNER_ID,
                    "\u2705 <b>CHANNEL AUTO-DETECTED</b>\n\n"
                    f"\U0001f4da <b>{esc(getattr(chat, 'title', chat.id))}</b>\n"
                    f"\U0001f194 <code>{chat.id}</code>\n"
                    f"\U0001f916 Bot status: <b>{esc(new_status)}</b>\n\n"
                    "Channel \u0905\u092c bot \u092e\u0947\u0902 automatically \u0926\u093f\u0916\u093e\u0908 \u0926\u0947\u0917\u093e.\n"
                    "Manual <b>Add Course</b> \u0915\u0940 \u091c\u0930\u0942\u0930\u0924 \u0928\u0939\u0940\u0902 \u0939\u0948.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    elif chat and old_status in ("administrator", "creator") and new_status in (
        "left",
        "kicked",
    ):
        with db() as c:
            c.execute(
                "UPDATE channels SET bot_status=? WHERE chat_id=?",
                (new_status, str(chat.id)),
            )


async def refresh_channel_permissions(context, chat_id):
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(int(chat_id), me.id)
        chat = await context.bot.get_chat(int(chat_id))
        await save_detected_channel(context.bot, chat, member)
        return member
    except Exception as e:
        log.warning("refresh channel failed %s: %s", chat_id, e)
        return None


async def checkchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Use:\n<code>/checkchannel -1001234567890</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        chat_id = int(context.args[0])
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        chat = await context.bot.get_chat(chat_id)

        await save_detected_channel(context.bot, chat, member)

        invite = getattr(member, "can_invite_users", None)
        ban = getattr(member, "can_restrict_members", None)

        await update.message.reply_text(
            "\U0001f50e <b>CHANNEL CHECK</b>\n\n"
            f"\U0001f4da {esc(getattr(chat, 'title', chat_id))}\n"
            f"\U0001f194 <code>{chat_id}</code>\n"
            f"\U0001f916 Status: <b>{esc(member.status)}</b>\n"
            f"\U0001f517 Invite Users/Add Subscribers: <b>{invite}</b>\n"
            f"\U0001f6ab Ban Users: <b>{ban}</b>\n\n"
            "\u2705 Channel database \u092e\u0947\u0902 save/update \u0939\u094b \u0917\u092f\u093e.\n"
            "Demo auto-remove \u0915\u0947 \u0932\u093f\u090f Invite + Ban \u0926\u094b\u0928\u094b\u0902 ON \u0939\u094b\u0928\u0947 \u091a\u093e\u0939\u093f\u090f.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(
            "\u274c Channel check failed:\n"
            f"<code>{esc(e)}</code>",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# HOME
# ONLY OWNER PANEL + DAILY REPORT + AUTO-DETECTED CHANNELS
# ============================================================
def home_kb():
    # Channels are intentionally HIDDEN from /start.
    # They remain stored in SQLite and are available through Telegram
    # inline search: @YourBotUsername course-name
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "\u2699\ufe0f OWNER PANEL",
                callback_data="owner"
            ),
            InlineKeyboardButton(
                "\U0001f4ca DAILY REPORT",
                callback_data="report"
            ),
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin(uid):
        if update.message:
            await update.message.reply_text("\u274c Access denied.")
        return

    text = (
        "\U0001f338 <b>Hello Seller Family, Kese Ho</b>\n\n"
        "I AM WIZARD \U0001f338\U0001f495\n\n"
        "\U0001f50e <b>Course Search</b>\n"
        "Telegram ke kisi chat me likho:\n"
        "<code>@YourBotUsername course-name</code>\n\n"
        "Search result me apna course select karo.\n\n"
        "\U0001f4e1 <b>Channel Search</b>\n"
        "Bot ke inline search me course ka naam type karo aur result select karo."
    )

    if update.callback_query:
        await update.callback_query.answer()
        await edit(update.callback_query, text, home_kb())
    else:
        await update.message.reply_text(
            text,
            reply_markup=home_kb(),
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# AUTO-DETECTED CHANNEL PANEL
# ============================================================
async def channel_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    chat_id = q.data.split(":", 1)[1]

    with db() as c:
        r = c.execute(
            "SELECT * FROM channels WHERE chat_id=?",
            (chat_id,),
        ).fetchone()

    if not r:
        await edit(q, "\u274c Channel not found.", home_kb())
        return

    # Refresh permissions every time panel opens.
    await refresh_channel_permissions(context, chat_id)

    with db() as c:
        r = c.execute(
            "SELECT * FROM channels WHERE chat_id=?",
            (chat_id,),
        ).fetchone()

    invite_ok = bool(r["can_invite"])
    ban_ok = bool(r["can_ban"])

    text = (
        f"\U0001f4da <b>{esc(r['title'])}</b>\n\n"
        f"\U0001f194 <code>{esc(r['chat_id'])}</code>\n"
        f"\U0001f916 Bot: <b>{esc(r['bot_status'])}</b>\n"
        f"\U0001f517 Invite Users: <b>{'ON' if invite_ok else 'OFF'}</b>\n"
        f"\U0001f6ab Ban Users: <b>{'ON' if ban_ok else 'OFF'}</b>\n\n"
        "Choose an access link below:"
    )

    kb = []
    row = []
    if can(q.from_user.id, "demo"):
        row.append(
            InlineKeyboardButton(
                "\U0001f517 Demo Link",
                callback_data=f"link:demo:{chat_id}",
            )
        )
    if can(q.from_user.id, "perm"):
        row.append(
            InlineKeyboardButton(
                "\U0001f464 Permanent Link",
                callback_data=f"link:perm:{chat_id}",
            )
        )
    if row:
        kb.append(row)

    kb.append([
        InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"channel:{chat_id}"),
        InlineKeyboardButton("\u2b05\ufe0f Home", callback_data="home"),
    ])

    await edit(q, text, InlineKeyboardMarkup(kb))


# ============================================================
# LINK CREATION
# ============================================================
async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, typ, chat_id = q.data.split(":", 2)

    if not can(q.from_user.id, typ):
        await q.answer("\u274c No access", show_alert=True)
        return

    # Get current Telegram state. This also refreshes our detected channel.
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(int(chat_id), me.id)
        chat = await context.bot.get_chat(int(chat_id))
        await save_detected_channel(context.bot, chat, member)
    except Exception as e:
        await q.answer("\u274c Channel access error", show_alert=True)
        await edit(
            q,
            "\u274c <b>Channel access \u0928\u0939\u0940\u0902 \u092e\u093f\u0932 \u0930\u0939\u093e</b>\n\n"
            f"<code>{esc(e)}</code>\n\n"
            "Bot \u0915\u094b \u0907\u0938\u0940 channel \u092e\u0947\u0902 Administrator \u092c\u0928\u093e\u0913.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2b05\ufe0f Home", callback_data="home")]
            ]),
        )
        return

    if member.status not in ("administrator", "creator"):
        await q.answer("\u274c Bot is not admin", show_alert=True)
        return

    invite_ok = getattr(member, "can_invite_users", None) is True
    ban_ok = getattr(member, "can_restrict_members", None) is True

    if not invite_ok:
        await q.answer("\u274c Invite Users permission OFF", show_alert=True)
        await edit(
            q,
            "\u274c <b>Link create \u0928\u0939\u0940\u0902 \u0939\u0941\u0906</b>\n\n"
            f"\U0001f4da {esc(chat.title)}\n\n"
            "Telegram \u2192 Channel \u2192 Administrators \u2192 Bot \u2192\n"
            "<b>Add Subscribers / Invite Users via Link = ON</b>\n\n"
            "Save \u0915\u0930\u0915\u0947 \u092b\u093f\u0930 Demo/Permanent Link \u0926\u092c\u093e\u0913.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=f"channel:{chat_id}"
                )]
            ]),
        )
        return

    if typ == "demo" and not ban_ok:
        await q.answer("\u274c Ban Users permission OFF", show_alert=True)
        await edit(
            q,
            "\u274c <b>Demo link create \u0928\u0939\u0940\u0902 \u0939\u0941\u0906</b>\n\n"
            f"\U0001f4da {esc(chat.title)}\n\n"
            "Demo \u0915\u0947 \u0932\u093f\u090f Bot \u0915\u094b:\n"
            "\u2705 Add Subscribers / Invite Users\n"
            "\u2705 Ban Users\n"
            "\u0926\u094b\u0928\u094b\u0902 permissions \u091a\u093e\u0939\u093f\u090f.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=f"channel:{chat_id}"
                )]
            ]),
        )
        return

    try:
        created = now()
        creator = admin_name(q.from_user.id)

        # member_limit=1 ensures the generated link is single-use.
        link = await context.bot.create_chat_invite_link(
            chat_id=int(chat_id),
            name=f"{typ}_{q.from_user.id}_{created.strftime('%Y%m%d%H%M%S')}",
            member_limit=1,
            creates_join_request=False,
        )

        course_name = getattr(chat, "title", None) or str(chat_id)

        with db() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO links(
                    invite_link,chat_id,course_name,link_type,
                    creator_id,creator_name,created_at,used
                )
                VALUES(?,?,?,?,?,?,?,0)
                """,
                (
                    link.invite_link,
                    str(chat_id),
                    course_name,
                    typ,
                    q.from_user.id,
                    creator,
                    iso(created),
                ),
            )

        if typ == "demo":
            message = (
                "\U0001f393 <b>Access Granted: Demo Pass</b> \u23f3\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f3e2 <b>\u091a\u0948\u0928\u0932 / \u0915\u094b\u0930\u094d\u0938 \u0915\u093e \u0928\u093e\u092e:</b>\n"
                f"\u279c {esc(course_name)}\n\n"
                "\U0001f4e5 <b>\u092f\u0939\u093e\u0901 \u0938\u0947 \u091c\u094d\u0935\u093e\u0907\u0928 \u0915\u0930\u0947\u0902:</b>\n"
                f"\U0001f517 <a href=\"{esc(link.invite_link)}\">Open Demo Link</a>\n\n"
                "\U0001f4cc <b>\u092e\u0939\u0924\u094d\u0935\u092a\u0942\u0930\u094d\u0923 \u0928\u093f\u0930\u094d\u0926\u0947\u0936:</b>\n"
                "\u26a0\ufe0f \u092f\u0939 Demo Joining Link \u0915\u0947\u0935\u0932 1 \u092c\u093e\u0930 \u0915\u093e\u092e \u0915\u0930\u0947\u0917\u093e\u0964\n"
                f"\u23f1 \u0938\u093f\u0938\u094d\u091f\u092e join \u0939\u094b\u0928\u0947 \u0915\u0947 \u0920\u0940\u0915 <b>{demo_minutes()} \u092e\u093f\u0928\u091f</b> "
                "\u092c\u093e\u0926 member \u0915\u094b automatically remove \u0915\u0930\u0947\u0917\u093e."
            )
        else:
            message = (
                "\U0001f48e <b>Access Granted: Permanent Pass</b> \U0001f48e\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f3e2 <b>\u091a\u0948\u0928\u0932 / \u0915\u094b\u0930\u094d\u0938 \u0915\u093e \u0928\u093e\u092e:</b>\n"
                f"\u279c {esc(course_name)}\n\n"
                "\U0001f4e5 <b>\u092f\u0939\u093e\u0901 \u0938\u0947 \u091c\u094d\u0935\u093e\u0907\u0928 \u0915\u0930\u0947\u0902:</b>\n"
                f"\U0001f517 <a href=\"{esc(link.invite_link)}\">Open Permanent Link</a>\n\n"
                "\U0001f4cc <b>\u092e\u0939\u0924\u094d\u0935\u092a\u0942\u0930\u094d\u0923 \u0928\u093f\u0930\u094d\u0926\u0947\u0936:</b>\n"
                "\u26a0\ufe0f \u092f\u0939 Permanent Joining Link \u0915\u0947\u0935\u0932 1 \u092c\u093e\u0930 \u0915\u093e\u092e \u0915\u0930\u0947\u0917\u093e."
            )

        await edit(
            q,
            message,
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=f"channel:{chat_id}"
                )]
            ]),
        )

    except Exception as e:
        log.exception("invite link creation failed")
        await edit(
            q,
            "\u274c <b>Telegram link creation failed</b>\n\n"
            f"<code>{esc(e)}</code>\n\n"
            "Bot permissions \u0914\u0930 channel admin status check \u0915\u0930\u094b.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=f"channel:{chat_id}"
                )]
            ]),
        )


# ============================================================
# MEMBER JOIN TRACKING
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
            (invite_url,),
        ).fetchone()

    if not link:
        return

    u = cm.from_user
    joined = cm.date or now()
    expires = (
        joined + timedelta(minutes=demo_minutes())
        if link["link_type"] == "demo"
        else None
    )

    with db() as c:
        existing = c.execute(
            """
            SELECT id FROM members
            WHERE user_id=? AND invite_link=? AND chat_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (u.id, invite_url, str(cm.chat.id)),
        ).fetchone()

        if existing:
            c.execute(
                "UPDATE links SET used=1 WHERE invite_link=?",
                (invite_url,),
            )
            return

        c.execute(
            """
            INSERT INTO members(
                user_id,username,full_name,chat_id,course_name,
                invite_link,link_type,creator_id,creator_name,
                joined_at,expires_at,status
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                u.id,
                u.username,
                user_name(u),
                str(cm.chat.id),
                link["course_name"],
                invite_url,
                link["link_type"],
                link["creator_id"],
                link["creator_name"],
                iso(joined),
                iso(expires),
                "active",
            ),
        )

        c.execute(
            "UPDATE links SET used=1 WHERE invite_link=?",
            (invite_url,),
        )

    if OWNER_ID:
        kind = "\U0001f393 DEMO" if link["link_type"] == "demo" else "\U0001f48e PERMANENT"
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"{kind} <b>MEMBER JOINED</b>\n\n"
                f"\U0001f464 Seller/Admin: <b>{esc(link['creator_name'])}</b>\n"
                f"\U0001f4da Channel: <b>{esc(link['course_name'])}</b>\n"
                f"\U0001f464 Member: <b>{esc(user_name(u))}</b>\n"
                f"\U0001f194 ID: <code>{u.id}</code>\n"
                f"\U0001f550 Joined: {joined.astimezone(IST).strftime('%d/%m/%Y %H:%M:%S')}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ============================================================
# DEMO AUTO REMOVE
# ============================================================
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
            (iso(current),),
        ).fetchall()

    for r in rows:
        try:
            await context.bot.ban_chat_member(
                chat_id=int(r["chat_id"]),
                user_id=int(r["user_id"]),
            )

            # Remove from channel without keeping a permanent Telegram ban.
            try:
                await context.bot.unban_chat_member(
                    chat_id=int(r["chat_id"]),
                    user_id=int(r["user_id"]),
                    only_if_banned=True,
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
                    (iso(current), r["id"]),
                )

        except Exception as e:
            log.error(
                "AUTO REMOVE failed user=%s chat=%s: %s",
                r["user_id"],
                r["chat_id"],
                e,
            )


# ============================================================
# OWNER PANEL
# ============================================================
async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return

    await q.answer()

    with db() as c:
        admins = c.execute("SELECT * FROM admins").fetchall()
        channels = c.execute(
            "SELECT * FROM channels ORDER BY lower(title)"
        ).fetchall()

    text = (
        "\U0001f451 <b>OWNER PANEL</b>\n\n"
        f"\U0001f465 Admins: <b>{len(admins)}</b>\n"
        f"\U0001f4e1 Detected Channels: <b>{len(channels)}</b>\n"
        f"\u23f1 Demo Time: <b>{demo_minutes()} minutes</b>\n\n"
        "\U0001f4e1 Bot \u0915\u094b \u0915\u093f\u0938\u0940 channel \u092e\u0947\u0902 Administrator \u092c\u0928\u093e\u0913 \u2192 "
        "\u0935\u0939 channel automatically detect \u0939\u094b\u0915\u0930 Home \u092a\u0930 \u0926\u093f\u0916\u093e\u0908 \u0926\u0947\u0917\u093e."
    )

    kb = [
        [
            InlineKeyboardButton("\U0001f465 Admins", callback_data="admins"),
            InlineKeyboardButton("\u23f1 Demo Time", callback_data="showtime"),
        ],
        [
            InlineKeyboardButton("\U0001f4ca Daily Report", callback_data="report"),
            InlineKeyboardButton("\U0001f4e1 Channels", callback_data="channels"),
        ],
        [
            InlineKeyboardButton("\u2b05\ufe0f Home", callback_data="home")
        ],
    ]

    await edit(q, text, InlineKeyboardMarkup(kb))


async def channels_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return

    await q.answer()

    with db() as c:
        rows = c.execute(
            "SELECT * FROM channels ORDER BY lower(title)"
        ).fetchall()

    if not rows:
        text = (
            "\U0001f4e1 <b>DETECTED CHANNELS</b>\n\n"
            "\u0905\u092d\u0940 \u0915\u094b\u0908 channel detect \u0928\u0939\u0940\u0902 \u0939\u0941\u0906.\n\n"
            "Bot \u0915\u094b channel \u092e\u0947\u0902 Administrator \u092c\u0928\u093e\u0913.\n"
            "Telegram \u0915\u093e my_chat_member update \u0906\u0924\u0947 \u0939\u0940 channel \u0905\u092a\u0928\u0947 \u0906\u092a save \u0939\u094b\u0917\u093e."
        )
        kb = [[InlineKeyboardButton("\u2b05\ufe0f Owner Panel", callback_data="owner")]]
        await edit(q, text, InlineKeyboardMarkup(kb))
        return

    text = "\U0001f4e1 <b>DETECTED CHANNELS</b>\n\n"
    kb = []

    for r in rows:
        text += (
            f"\U0001f4da <b>{esc(r['title'])}</b>\n"
            f"\U0001f194 <code>{esc(r['chat_id'])}</code>\n"
            f"\U0001f517 Invite: {'ON' if r['can_invite'] else 'OFF'} | "
            f"\U0001f6ab Ban: {'ON' if r['can_ban'] else 'OFF'}\n\n"
        )
        kb.append([
            InlineKeyboardButton(
                f"\U0001f4da {r['title']}",
                callback_data=f"channel:{r['chat_id']}",
            )
        ])

    kb.append([
        InlineKeyboardButton("\u2b05\ufe0f Owner Panel", callback_data="owner")
    ])
    await edit(q, text, InlineKeyboardMarkup(kb))


# ============================================================
# ADMIN ACCESS
# ============================================================
async def admins_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return
    await q.answer()

    with db() as c:
        rows = c.execute(
            "SELECT * FROM admins ORDER BY uid"
        ).fetchall()

    text = "\U0001f465 <b>ADMINS</b>\n\n"
    kb = []

    for r in rows:
        text += (
            f"\U0001f464 <b>{esc(r['name'])}</b>\n"
            f"\U0001f194 <code>{r['uid']}</code>\n"
            f"\U0001f510 {esc(r['permissions'])}\n\n"
        )

    # No Add Course here.
    kb.append([
        InlineKeyboardButton("\u2795 Add Admin", callback_data="addadmin")
    ])
    kb.append([
        InlineKeyboardButton("\u2b05\ufe0f Owner Panel", callback_data="owner")
    ])

    await edit(q, text, InlineKeyboardMarkup(kb))


async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return

    await q.answer()
    context.user_data["waiting_admin"] = True

    await edit(
        q,
        "\u2795 <b>ADD ADMIN</b>\n\n"
        "Owner chat \u092e\u0947\u0902 numeric Telegram User ID \u092d\u0947\u091c\u094b.\n\n"
        "Example:\n<code>123456789</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\u274c Cancel", callback_data="owner")]
        ]),
    )


async def add_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return
    if not context.user_data.get("waiting_admin"):
        return

    try:
        uid = int(update.message.text.strip())
    except Exception:
        await update.message.reply_text("\u274c Numeric Telegram User ID \u092d\u0947\u091c\u094b.")
        return

    context.user_data.pop("waiting_admin", None)

    with db() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO admins(uid,name,permissions)
            VALUES(?,?,?)
            """,
            (uid, f"Admin {uid}", "demo"),
        )

    await update.message.reply_text(
        f"\u2705 Admin added: <code>{uid}</code>\n"
        "Default access: Demo",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "\U0001f517 Demo + Permanent",
                    callback_data=f"access:both:{uid}"
                )
            ],
            [
                InlineKeyboardButton(
                    "\U0001f517 Demo Only",
                    callback_data=f"access:demo:{uid}"
                )
            ],
        ]),
    )


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
            (permissions, uid),
        )

    await edit(
        q,
        "\u2705 <b>ACCESS UPDATED</b>\n\n"
        f"\U0001f194 <code>{uid}</code>\n"
        f"\U0001f510 {esc(permissions)}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b05\ufe0f Admins", callback_data="admins")]
        ]),
    )


# ============================================================
# DEMO TIME
# ============================================================
async def show_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return

    await q.answer()
    await edit(
        q,
        f"\u23f1 <b>Current Demo Time: {demo_minutes()} minutes</b>\n\n"
        "Owner chat \u092e\u0947\u0902 command \u092d\u0947\u091c\u094b:\n"
        "<code>/demotime 10</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b05\ufe0f Owner Panel", callback_data="owner")]
        ]),
    )


async def demotime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            f"Current Demo Time: {demo_minutes()} minutes\n"
            "Use: /demotime 10"
        )
        return

    try:
        n = int(context.args[0])
        if not 1 <= n <= 1440:
            raise ValueError

        with db() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO settings(key,value)
                VALUES('demo_minutes',?)
                """,
                (str(n),),
            )

        await update.message.reply_text(
            f"\u2705 Demo time \u0905\u092c <b>{n} minutes</b> \u0939\u0948.",
            parse_mode=ParseMode.HTML,
        )
    except ValueError:
        await update.message.reply_text(
            "\u274c Demo time 1 \u0938\u0947 1440 minutes \u0915\u0947 \u092c\u0940\u091a \u0939\u094b\u0928\u093e \u091a\u093e\u0939\u093f\u090f."
        )


# ============================================================
# DAILY REPORT
# ============================================================
def make_report(date_string=None):
    from collections import defaultdict
    """
    Complete owner daily report for one completed IST calendar day.

    Every generated link and every actual member join is kept separately.
    A demo member remains in the report after auto-removal; only the
    member status changes from active -> removed.
    """
    date_string = date_string or (
        ist_now().date() - timedelta(days=1)
    ).isoformat()

    with db() as c:
        links = c.execute(
            "SELECT * FROM links ORDER BY created_at"
        ).fetchall()

        members = c.execute(
            "SELECT * FROM members ORDER BY joined_at"
        ).fetchall()

    day_links = [
        r for r in links
        if local_date(r["created_at"]) == date_string
    ]

    day_members = [
        r for r in members
        if local_date(r["joined_at"]) == date_string
    ]

    demo_links = [
        r for r in day_links if r["link_type"] == "demo"
    ]
    perm_links = [
        r for r in day_links if r["link_type"] == "perm"
    ]

    demo_members = [
        r for r in day_members if r["link_type"] == "demo"
    ]
    perm_members = [
        r for r in day_members if r["link_type"] == "perm"
    ]

    # --------------------------------------------------------
    # SELLER-WISE TOTALS
    # --------------------------------------------------------
    seller_stats = defaultdict(lambda: {
        "demo_links": 0,
        "perm_links": 0,
        "demo_members": 0,
        "perm_members": 0,
    })

    for r in day_links:
        seller = r["creator_name"]
        if r["link_type"] == "demo":
            seller_stats[seller]["demo_links"] += 1
        else:
            seller_stats[seller]["perm_links"] += 1

    for r in day_members:
        seller = r["creator_name"]
        if r["link_type"] == "demo":
            seller_stats[seller]["demo_members"] += 1
        else:
            seller_stats[seller]["perm_members"] += 1

    text = (
        "\U0001f4ca <b>AUTOMATIC DAILY REPORT</b> \U0001f4ca\n"
        f"\U0001f4c5 Date: <b>{date_string}</b> (IST)\n"
        "\U0001f4cc This report contains the complete activity of the "
        "completed day.\n"
        "\u2501" * 18 + "\n\n"
    )

    # --------------------------------------------------------
    # SELLER SUMMARY
    # --------------------------------------------------------
    text += (
        "\U0001f465 <b>SELLER-WISE SUMMARY</b>\n\n"
    )

    if seller_stats:
        for seller, s in sorted(
            seller_stats.items(),
            key=lambda x: x[0].lower()
        ):
            total_members = (
                s["demo_members"] + s["perm_members"]
            )
            total_links = (
                s["demo_links"] + s["perm_links"]
            )

            text += (
                f"\U0001f464 <b>{esc(seller)}</b>\n"
                f"\U0001f393 Demo Links: <b>{s['demo_links']}</b> | "
                f"Members Added: <b>{s['demo_members']}</b>\n"
                f"\U0001f48e Permanent Links: <b>{s['perm_links']}</b> | "
                f"Members Added: <b>{s['perm_members']}</b>\n"
                f"\U0001f4ca Total Links: <b>{total_links}</b> | "
                f"Total Members: <b>{total_members}</b>\n\n"
            )
    else:
        text += "\u274c No seller activity recorded today.\n\n"

    text += "\u2501" * 18 + "\n\n"

    # --------------------------------------------------------
    # GENERATED LINKS
    # This tells owner who generated/sent the access link.
    # --------------------------------------------------------
    text += (
        "\U0001f517 <b>LINKS GENERATED / SENT</b>\n"
        f"\U0001f393 Demo Links: <b>{len(demo_links)}</b>\n"
        f"\U0001f48e Permanent Links: <b>{len(perm_links)}</b>\n\n"
    )

    if day_links:
        for i, r in enumerate(day_links, 1):
            kind = (
                "\U0001f393 DEMO LINK"
                if r["link_type"] == "demo"
                else "\U0001f48e PERMANENT LINK"
            )
            used = (
                "\U0001f7e2 USED"
                if r["used"]
                else "\u26aa UNUSED"
            )

            text += (
                f"<b>Link #{i}</b> \u2014 {kind}\n"
                f"\U0001f4da Course/Channel: "
                f"<b>{esc(r['course_name'])}</b>\n"
                f"\U0001f464 Created By: "
                f"<b>{esc(r['creator_name'])}</b>\n"
                f"\U0001f194 Creator ID: "
                f"<code>{r['creator_id']}</code>\n"
                f"\U0001f550 Link Created: "
                f"<b>{local_time(r['created_at'])}</b>\n"
                f"\U0001f517 Link: "
                f"<a href=\"{esc(r['invite_link'])}\">Open Link</a>\n"
                f"\U0001f4cc Status: <b>{used}</b>\n\n"
            )
    else:
        text += "\u274c No access link was generated today.\n\n"

    text += "\u2501" * 18 + "\n\n"

    # --------------------------------------------------------
    # DEMO MEMBER DETAIL
    # --------------------------------------------------------
    text += (
        f"\U0001f393 <b>DEMO MEMBERS \u2014 {len(demo_members)}</b>\n\n"
    )

    if demo_members:
        for i, r in enumerate(demo_members, 1):
            if r["status"] == "removed":
                status = "\U0001f6ab Auto Removed"
            elif r["status"] == "active":
                status = "\U0001f7e2 Active"
            else:
                status = esc(r["status"])

            username = (
                f"@{esc(r['username'])}"
                if r["username"]
                else "No username"
            )

            text += (
                f"<b>Demo #{i}</b>\n"
                f"\U0001f464 <b>Member Name:</b> "
                f"{esc(r['full_name'])}\n"
                f"\U0001f517 <b>Username:</b> {username}\n"
                f"\U0001f194 <b>Member ID:</b> "
                f"<code>{r['user_id']}</code>\n"
                f"\U0001f4da <b>Added In:</b> "
                f"{esc(r['course_name'])}\n"
                f"\U0001f393 <b>Access Type:</b> DEMO\n"
                f"\U0001f464 <b>Added By:</b> "
                f"{esc(r['creator_name'])}\n"
                f"\U0001f194 <b>Seller ID:</b> "
                f"<code>{r['creator_id']}</code>\n"
                f"\U0001f550 <b>Joined Time:</b> "
                f"{local_time(r['joined_at'])} IST\n"
                f"\u23f3 <b>Demo Duration:</b> "
                f"{demo_minutes()} minutes\n"
                f"\U0001f6ab <b>Status:</b> {status}\n"
            )

            if r["removed_at"]:
                text += (
                    f"\U0001f552 <b>Removed Time:</b> "
                    f"{local_time(r['removed_at'])} IST\n"
                )

            text += (
                f"\U0001f517 <b>Joining Link:</b> "
                f"<a href=\"{esc(r['invite_link'])}\">Open Link</a>\n\n"
            )
    else:
        text += "\u274c No Demo member joined today.\n\n"

    text += "\u2501" * 18 + "\n\n"

    # --------------------------------------------------------
    # PERMANENT MEMBER DETAIL
    # --------------------------------------------------------
    text += (
        f"\U0001f48e <b>PERMANENT MEMBERS \u2014 "
        f"{len(perm_members)}</b>\n\n"
    )

    if perm_members:
        for i, r in enumerate(perm_members, 1):
            username = (
                f"@{esc(r['username'])}"
                if r["username"]
                else "No username"
            )

            text += (
                f"<b>Permanent #{i}</b>\n"
                f"\U0001f464 <b>Member Name:</b> "
                f"{esc(r['full_name'])}\n"
                f"\U0001f517 <b>Username:</b> {username}\n"
                f"\U0001f194 <b>Member ID:</b> "
                f"<code>{r['user_id']}</code>\n"
                f"\U0001f4da <b>Added In:</b> "
                f"{esc(r['course_name'])}\n"
                f"\U0001f48e <b>Access Type:</b> PERMANENT\n"
                f"\U0001f464 <b>Added By:</b> "
                f"{esc(r['creator_name'])}\n"
                f"\U0001f194 <b>Seller ID:</b> "
                f"<code>{r['creator_id']}</code>\n"
                f"\U0001f550 <b>Joined Time:</b> "
                f"{local_time(r['joined_at'])} IST\n"
                f"\U0001f7e2 <b>Status:</b> Active/Permanent\n"
                f"\U0001f517 <b>Joining Link:</b> "
                f"<a href=\"{esc(r['invite_link'])}\">Open Link</a>\n\n"
            )
    else:
        text += "\u274c No Permanent member joined today.\n\n"

    text += "\u2501" * 18 + "\n\n"

    # --------------------------------------------------------
    # FINAL TOTALS
    # --------------------------------------------------------
    text += (
        "\U0001f4ca <b>FINAL TOTALS</b>\n"
        f"\U0001f517 Total Links Generated: "
        f"<b>{len(day_links)}</b>\n"
        f"\U0001f393 Demo Links: <b>{len(demo_links)}</b>\n"
        f"\U0001f48e Permanent Links: <b>{len(perm_links)}</b>\n"
        f"\U0001f393 Demo Members Added: "
        f"<b>{len(demo_members)}</b>\n"
        f"\U0001f48e Permanent Members Added: "
        f"<b>{len(perm_members)}</b>\n"
        f"\U0001f465 Total Members Added: "
        f"<b>{len(day_members)}</b>\n"
        "\n"
        "\u2705 Demo members are kept in the report even after "
        "automatic removal."
    )

    return text


def split_report_text(text, limit=3900):
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line

    if current:
        chunks.append(current)

    return chunks


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return

    d = (ist_now().date() - timedelta(days=1)).isoformat()
    for chunk in split_report_text(make_report(d)):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manual Daily Report button.

    Sends yesterday's completed IST report directly to the Owner.
    It does NOT depend on editing a potentially very large HTML message,
    so the button keeps working even when the report is split into many
    Telegram messages.
    """
    q = update.callback_query

    if not owner(q.from_user.id):
        await q.answer("\u274c Owner only", show_alert=True)
        return

    await q.answer("\U0001f4ca Report generating...")

    try:
        d = (ist_now().date() - timedelta(days=1)).isoformat()
        chunks = split_report_text(make_report(d))

        sent = 0
        for chunk in chunks:
            try:
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as html_error:
                # If any generated text contains an HTML edge case,
                # send that same report chunk as plain text instead of
                # making the button appear broken.
                log.warning(
                    "HTML report send failed; sending plain text: %s",
                    html_error,
                )
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=re.sub(r"<[^>]+>", "", chunk),
                    disable_web_page_preview=True,
                )
            sent += 1

        await edit(
            q,
            "\u2705 <b>Daily Report Sent</b>\n\n"
            f"\U0001f4c5 Report date: <b>{esc(d)}</b> (IST)\n"
            f"\U0001f4e8 Messages sent: <b>{sent}</b>\n\n"
            "The complete member list is in the messages above.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "\u2b05\ufe0f Owner Panel",
                        callback_data="owner"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "\U0001f4ca Send Again",
                        callback_data="report"
                    )
                ],
            ]),
        )

    except Exception as e:
        log.exception("Manual daily report failed")

        try:
            await q.edit_message_text(
                "\u274c <b>Daily Report Error</b>\n\n"
                f"<code>{esc(str(e))}</code>\n\n"
                "Bot logs me exact error check karo.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "\u2b05\ufe0f Owner Panel",
                            callback_data="owner"
                        )
                    ]
                ]),
            )
        except Exception:
            pass


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    """Automatically send the previous completed IST calendar day once."""
    local = ist_now()

    # Wait until 00:05 IST so the previous day is fully complete.
    if local.hour == 0 and local.minute < 5:
        return

    d = (local.date() - timedelta(days=1)).isoformat()

    with db() as c:
        already = c.execute(
            "SELECT 1 FROM reports WHERE report_date=?",
            (d,),
        ).fetchone()

    if already or not OWNER_ID:
        return

    text = make_report(d)

    try:
        for chunk in split_report_text(text):
            try:
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as html_error:
                log.warning(
                    "Automatic HTML report send failed; "
                    "sending plain text: %s",
                    html_error,
                )
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=re.sub(r"<[^>]+>", "", chunk),
                    disable_web_page_preview=True,
                )

    except Exception:
        log.exception("daily report send failed")
        return

    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO reports(report_date,sent_at) VALUES(?,?)",
            (d, iso(now())),
        )
        c.commit()


# ============================================================
# INLINE SEARCH
# ============================================================
async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram inline course search.

    Use from ANY Telegram chat:
        @YourBotUsername course-name

    The channel list is NOT shown on /start. Search results come from
    auto-detected channels stored in SQLite.
    """
    query = (update.inline_query.query or "").strip().lower()

    with db() as c:
        if query:
            rows = c.execute(
                """
                SELECT chat_id,title,username,chat_type
                FROM channels
                WHERE bot_status IN ('administrator','creator')
                  AND (
                      lower(title) LIKE ?
                      OR lower(COALESCE(username,'')) LIKE ?
                      OR lower(chat_id) LIKE ?
                  )
                ORDER BY lower(title)
                LIMIT 50
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT chat_id,title,username,chat_type
                FROM channels
                WHERE bot_status IN ('administrator','creator')
                ORDER BY lower(title)
                LIMIT 50
                """
            ).fetchall()

    results = []

    for r in rows:
        title = r["title"] or r["chat_id"]
        username = r["username"]

        description = "Channel/Course"
        if username:
            description += f" \u2022 @{username}"

        message = (
            f"\U0001f4da <b>{esc(title)}</b>\n\n"
            "Choose Demo Link or Permanent Link below."
        )

        results.append(
            InlineQueryResultArticle(
                id=f"course_{r['chat_id']}",
                title=f"\U0001f4da {title}",
                description=description,
                input_message_content=InputTextMessageContent(
                    message,
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "\U0001f517 Demo Link",
                            callback_data=f"link:demo:{r['chat_id']}"
                        ),
                        InlineKeyboardButton(
                            "\U0001f464 Permanent Link",
                            callback_data=f"link:perm:{r['chat_id']}"
                        ),
                    ],
                ]),
            )
        )

    await update.inline_query.answer(
        results=results,
        cache_time=1,
        is_personal=True,
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data or ""

    if d == "home":
        await start(update, context)
    elif d == "owner":
        await owner_panel(update, context)
    elif d == "channels":
        await channels_page(update, context)
    elif d.startswith("channel:"):
        await channel_panel(update, context)
    elif d.startswith("link:"):
        await create_link(update, context)
    elif d == "admins":
        await admins_page(update, context)
    elif d.startswith("access:"):
        await set_access(update, context)
    elif d == "showtime":
        await show_time(update, context)
    elif d == "report":
        await report_button(update, context)
    elif d == "addadmin":
        await add_admin_start(update, context)
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

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("demotime", demotime))
    app.add_handler(CommandHandler("dailyreport", report))
    app.add_handler(CommandHandler("checkchannel", checkchannel))

    # Owner admin input
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            add_admin_message,
        )
    )

    # INLINE SEARCH
    app.add_handler(InlineQueryHandler(inline_search))

    # ========================================================
    # CRITICAL: MY_CHAT_MEMBER
    # This is what auto-detects a channel when the bot is made
    # administrator. Do NOT remove this handler.
    # ========================================================
    app.add_handler(
        ChatMemberHandler(
            my_chat_member_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # Actual users joining through generated invite links.
    app.add_handler(
        ChatMemberHandler(
            chat_member_update,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # Callback buttons
    app.add_handler(CallbackQueryHandler(callbacks))

    # Demo expiry: every 15 sec.
    app.job_queue.run_repeating(
        demo_job,
        interval=15,
        first=10,
    )

    # Daily report: every minute.
    app.job_queue.run_repeating(
        daily_job,
        interval=60,
        first=20,
    )

    log.info("Bot started. Auto channel detection = ON.")
    app.run_polling(
        allowed_updates=[
            "message",
            "callback_query",
            "inline_query",
            "my_chat_member",
            "chat_member",
        ],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
