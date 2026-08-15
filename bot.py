import os
import html
import logging
import sqlite3
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
                    "â <b>CHANNEL AUTO-DETECTED</b>\n\n"
                    f"ð <b>{esc(getattr(chat, 'title', chat.id))}</b>\n"
                    f"ð <code>{chat.id}</code>\n"
                    f"ð¤ Bot status: <b>{esc(new_status)}</b>\n\n"
                    "Channel à¤à¤¬ bot à¤®à¥à¤ automatically à¤¦à¤¿à¤à¤¾à¤ à¤¦à¥à¤à¤¾.\n"
                    "Manual <b>Add Course</b> à¤à¥ à¤à¤°à¥à¤°à¤¤ à¤¨à¤¹à¥à¤ à¤¹à¥.",
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
            "ð <b>CHANNEL CHECK</b>\n\n"
            f"ð {esc(getattr(chat, 'title', chat_id))}\n"
            f"ð <code>{chat_id}</code>\n"
            f"ð¤ Status: <b>{esc(member.status)}</b>\n"
            f"ð Invite Users/Add Subscribers: <b>{invite}</b>\n"
            f"ð« Ban Users: <b>{ban}</b>\n\n"
            "â Channel database à¤®à¥à¤ save/update à¤¹à¥ à¤à¤¯à¤¾.\n"
            "Demo auto-remove à¤à¥ à¤²à¤¿à¤ Invite + Ban à¤¦à¥à¤¨à¥à¤ ON à¤¹à¥à¤¨à¥ à¤à¤¾à¤¹à¤¿à¤.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(
            "â Channel check failed:\n"
            f"<code>{esc(e)}</code>",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# HOME
# ONLY OWNER PANEL + DAILY REPORT + AUTO-DETECTED CHANNELS
# ============================================================
def home_kb():
    with db() as c:
        channels = c.execute(
            """
            SELECT * FROM channels
            WHERE bot_status IN ('administrator','creator')
            ORDER BY lower(title)
            """
        ).fetchall()

    buttons = [
        InlineKeyboardButton(
            f"ð {r['title']}",
            callback_data=f"channel:{r['chat_id']}"
        )
        for r in channels
    ]

    kb = rows_buttons(buttons, 2)

    # NO category buttons
    # NO Add Course button
    # NO New Category button
    kb.append([
        InlineKeyboardButton("âï¸ OWNER PANEL", callback_data="owner"),
        InlineKeyboardButton("ð DAILY REPORT", callback_data="report"),
    ])

    return InlineKeyboardMarkup(kb)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin(uid):
        if update.message:
            await update.message.reply_text("â Access denied.")
        return

    text = (
        "ð¸ <b>Hello Seller Family, Kese Ho</b>\n\n"
        "I AM WIZARD ð¸ð\n\n"
        "ð <b>Course Search</b>\n"
        "Telegram ke kisi chat me likho:\n"
        "<code>@YourBotUsername course-name</code>\n\n"
        "Search result me apna course select karo.\n\n"
        "ð¡ <b>Channel Auto Detect</b>\n"
        "Bot ko channel me admin banao â channel automatically yahan aa jayega."
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
        await edit(q, "â Channel not found.", home_kb())
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
        f"ð <b>{esc(r['title'])}</b>\n\n"
        f"ð <code>{esc(r['chat_id'])}</code>\n"
        f"ð¤ Bot: <b>{esc(r['bot_status'])}</b>\n"
        f"ð Invite Users: <b>{'ON' if invite_ok else 'OFF'}</b>\n"
        f"ð« Ban Users: <b>{'ON' if ban_ok else 'OFF'}</b>\n\n"
        "Choose an access link below:"
    )

    kb = []
    row = []
    if can(q.from_user.id, "demo"):
        row.append(
            InlineKeyboardButton(
                "ð Demo Link",
                callback_data=f"link:demo:{chat_id}",
            )
        )
    if can(q.from_user.id, "perm"):
        row.append(
            InlineKeyboardButton(
                "ð¤ Permanent Link",
                callback_data=f"link:perm:{chat_id}",
            )
        )
    if row:
        kb.append(row)

    kb.append([
        InlineKeyboardButton("ð Refresh", callback_data=f"channel:{chat_id}"),
        InlineKeyboardButton("â¬ï¸ Home", callback_data="home"),
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
        await q.answer("â No access", show_alert=True)
        return

    # Get current Telegram state. This also refreshes our detected channel.
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(int(chat_id), me.id)
        chat = await context.bot.get_chat(int(chat_id))
        await save_detected_channel(context.bot, chat, member)
    except Exception as e:
        await q.answer("â Channel access error", show_alert=True)
        await edit(
            q,
            "â <b>Channel access à¤¨à¤¹à¥à¤ à¤®à¤¿à¤² à¤°à¤¹à¤¾</b>\n\n"
            f"<code>{esc(e)}</code>\n\n"
            "Bot à¤à¥ à¤à¤¸à¥ channel à¤®à¥à¤ Administrator à¤¬à¤¨à¤¾à¤.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("â¬ï¸ Home", callback_data="home")]
            ]),
        )
        return

    if member.status not in ("administrator", "creator"):
        await q.answer("â Bot is not admin", show_alert=True)
        return

    invite_ok = getattr(member, "can_invite_users", None) is True
    ban_ok = getattr(member, "can_restrict_members", None) is True

    if not invite_ok:
        await q.answer("â Invite Users permission OFF", show_alert=True)
        await edit(
            q,
            "â <b>Link create à¤¨à¤¹à¥à¤ à¤¹à¥à¤</b>\n\n"
            f"ð {esc(chat.title)}\n\n"
            "Telegram â Channel â Administrators â Bot â\n"
            "<b>Add Subscribers / Invite Users via Link = ON</b>\n\n"
            "Save à¤à¤°à¤à¥ à¤«à¤¿à¤° Demo/Permanent Link à¤¦à¤¬à¤¾à¤.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "â¬ï¸ Back",
                    callback_data=f"channel:{chat_id}"
                )]
            ]),
        )
        return

    if typ == "demo" and not ban_ok:
        await q.answer("â Ban Users permission OFF", show_alert=True)
        await edit(
            q,
            "â <b>Demo link create à¤¨à¤¹à¥à¤ à¤¹à¥à¤</b>\n\n"
            f"ð {esc(chat.title)}\n\n"
            "Demo à¤à¥ à¤²à¤¿à¤ Bot à¤à¥:\n"
            "â Add Subscribers / Invite Users\n"
            "â Ban Users\n"
            "à¤¦à¥à¤¨à¥à¤ permissions à¤à¤¾à¤¹à¤¿à¤.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "â¬ï¸ Back",
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
                "ð <b>Access Granted: Demo Pass</b> â³\n"
                "ââââââââââââââââââ\n\n"
                f"ð¢ <b>à¤à¥à¤¨à¤² / à¤à¥à¤°à¥à¤¸ à¤à¤¾ à¤¨à¤¾à¤®:</b>\n"
                f"â {esc(course_name)}\n\n"
                "ð¥ <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤:</b>\n"
                f"ð <a href=\"{esc(link.invite_link)}\">Open Demo Link</a>\n\n"
                "ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶:</b>\n"
                "â ï¸ à¤¯à¤¹ Demo Joining Link à¤à¥à¤µà¤² 1 à¤¬à¤¾à¤° à¤à¤¾à¤® à¤à¤°à¥à¤à¤¾à¥¤\n"
                f"â± à¤¸à¤¿à¤¸à¥à¤à¤® join à¤¹à¥à¤¨à¥ à¤à¥ à¤ à¥à¤ <b>{demo_minutes()} à¤®à¤¿à¤¨à¤</b> "
                "à¤¬à¤¾à¤¦ member à¤à¥ automatically remove à¤à¤°à¥à¤à¤¾."
            )
        else:
            message = (
                "ð <b>Access Granted: Permanent Pass</b> ð\n"
                "ââââââââââââââââââ\n\n"
                f"ð¢ <b>à¤à¥à¤¨à¤² / à¤à¥à¤°à¥à¤¸ à¤à¤¾ à¤¨à¤¾à¤®:</b>\n"
                f"â {esc(course_name)}\n\n"
                "ð¥ <b>à¤¯à¤¹à¤¾à¤ à¤¸à¥ à¤à¥à¤µà¤¾à¤à¤¨ à¤à¤°à¥à¤:</b>\n"
                f"ð <a href=\"{esc(link.invite_link)}\">Open Permanent Link</a>\n\n"
                "ð <b>à¤®à¤¹à¤¤à¥à¤µà¤ªà¥à¤°à¥à¤£ à¤¨à¤¿à¤°à¥à¤¦à¥à¤¶:</b>\n"
                "â ï¸ à¤¯à¤¹ Permanent Joining Link à¤à¥à¤µà¤² 1 à¤¬à¤¾à¤° à¤à¤¾à¤® à¤à¤°à¥à¤à¤¾."
            )

        await edit(
            q,
            message,
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "â¬ï¸ Back",
                    callback_data=f"channel:{chat_id}"
                )]
            ]),
        )

    except Exception as e:
        log.exception("invite link creation failed")
        await edit(
            q,
            "â <b>Telegram link creation failed</b>\n\n"
            f"<code>{esc(e)}</code>\n\n"
            "Bot permissions à¤à¤° channel admin status check à¤à¤°à¥.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "â¬ï¸ Back",
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
        kind = "ð DEMO" if link["link_type"] == "demo" else "ð PERMANENT"
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"{kind} <b>MEMBER JOINED</b>\n\n"
                f"ð¤ Seller/Admin: <b>{esc(link['creator_name'])}</b>\n"
                f"ð Channel: <b>{esc(link['course_name'])}</b>\n"
                f"ð¤ Member: <b>{esc(user_name(u))}</b>\n"
                f"ð ID: <code>{u.id}</code>\n"
                f"ð Joined: {joined.astimezone(IST).strftime('%d/%m/%Y %H:%M:%S')}",
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
        await q.answer("â Owner only", show_alert=True)
        return

    await q.answer()

    with db() as c:
        admins = c.execute("SELECT * FROM admins").fetchall()
        channels = c.execute(
            "SELECT * FROM channels ORDER BY lower(title)"
        ).fetchall()

    text = (
        "ð <b>OWNER PANEL</b>\n\n"
        f"ð¥ Admins: <b>{len(admins)}</b>\n"
        f"ð¡ Detected Channels: <b>{len(channels)}</b>\n"
        f"â± Demo Time: <b>{demo_minutes()} minutes</b>\n\n"
        "ð¡ Bot à¤à¥ à¤à¤¿à¤¸à¥ channel à¤®à¥à¤ Administrator à¤¬à¤¨à¤¾à¤ â "
        "à¤µà¤¹ channel automatically detect à¤¹à¥à¤à¤° Home à¤ªà¤° à¤¦à¤¿à¤à¤¾à¤ à¤¦à¥à¤à¤¾."
    )

    kb = [
        [
            InlineKeyboardButton("ð¥ Admins", callback_data="admins"),
            InlineKeyboardButton("â± Demo Time", callback_data="showtime"),
        ],
        [
            InlineKeyboardButton("ð Daily Report", callback_data="report"),
            InlineKeyboardButton("ð¡ Channels", callback_data="channels"),
        ],
        [
            InlineKeyboardButton("â¬ï¸ Home", callback_data="home")
        ],
    ]

    await edit(q, text, InlineKeyboardMarkup(kb))


async def channels_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("â Owner only", show_alert=True)
        return

    await q.answer()

    with db() as c:
        rows = c.execute(
            "SELECT * FROM channels ORDER BY lower(title)"
        ).fetchall()

    if not rows:
        text = (
            "ð¡ <b>DETECTED CHANNELS</b>\n\n"
            "à¤à¤­à¥ à¤à¥à¤ channel detect à¤¨à¤¹à¥à¤ à¤¹à¥à¤.\n\n"
            "Bot à¤à¥ channel à¤®à¥à¤ Administrator à¤¬à¤¨à¤¾à¤.\n"
            "Telegram à¤à¤¾ my_chat_member update à¤à¤¤à¥ à¤¹à¥ channel à¤à¤ªà¤¨à¥ à¤à¤ª save à¤¹à¥à¤à¤¾."
        )
        kb = [[InlineKeyboardButton("â¬ï¸ Owner Panel", callback_data="owner")]]
        await edit(q, text, InlineKeyboardMarkup(kb))
        return

    text = "ð¡ <b>DETECTED CHANNELS</b>\n\n"
    kb = []

    for r in rows:
        text += (
            f"ð <b>{esc(r['title'])}</b>\n"
            f"ð <code>{esc(r['chat_id'])}</code>\n"
            f"ð Invite: {'ON' if r['can_invite'] else 'OFF'} | "
            f"ð« Ban: {'ON' if r['can_ban'] else 'OFF'}\n\n"
        )
        kb.append([
            InlineKeyboardButton(
                f"ð {r['title']}",
                callback_data=f"channel:{r['chat_id']}",
            )
        ])

    kb.append([
        InlineKeyboardButton("â¬ï¸ Owner Panel", callback_data="owner")
    ])
    await edit(q, text, InlineKeyboardMarkup(kb))


# ============================================================
# ADMIN ACCESS
# ============================================================
async def admins_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("â Owner only", show_alert=True)
        return
    await q.answer()

    with db() as c:
        rows = c.execute(
            "SELECT * FROM admins ORDER BY uid"
        ).fetchall()

    text = "ð¥ <b>ADMINS</b>\n\n"
    kb = []

    for r in rows:
        text += (
            f"ð¤ <b>{esc(r['name'])}</b>\n"
            f"ð <code>{r['uid']}</code>\n"
            f"ð {esc(r['permissions'])}\n\n"
        )

    # No Add Course here.
    kb.append([
        InlineKeyboardButton("â Add Admin", callback_data="addadmin")
    ])
    kb.append([
        InlineKeyboardButton("â¬ï¸ Owner Panel", callback_data="owner")
    ])

    await edit(q, text, InlineKeyboardMarkup(kb))


async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("â Owner only", show_alert=True)
        return

    await q.answer()
    context.user_data["waiting_admin"] = True

    await edit(
        q,
        "â <b>ADD ADMIN</b>\n\n"
        "Owner chat à¤®à¥à¤ numeric Telegram User ID à¤­à¥à¤à¥.\n\n"
        "Example:\n<code>123456789</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("â Cancel", callback_data="owner")]
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
        await update.message.reply_text("â Numeric Telegram User ID à¤­à¥à¤à¥.")
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
        f"â Admin added: <code>{uid}</code>\n"
        "Default access: Demo",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "ð Demo + Permanent",
                    callback_data=f"access:both:{uid}"
                )
            ],
            [
                InlineKeyboardButton(
                    "ð Demo Only",
                    callback_data=f"access:demo:{uid}"
                )
            ],
        ]),
    )


async def set_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("â Owner only", show_alert=True)
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
        "â <b>ACCESS UPDATED</b>\n\n"
        f"ð <code>{uid}</code>\n"
        f"ð {esc(permissions)}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("â¬ï¸ Admins", callback_data="admins")]
        ]),
    )


# ============================================================
# DEMO TIME
# ============================================================
async def show_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("â Owner only", show_alert=True)
        return

    await q.answer()
    await edit(
        q,
        f"â± <b>Current Demo Time: {demo_minutes()} minutes</b>\n\n"
        "Owner chat à¤®à¥à¤ command à¤­à¥à¤à¥:\n"
        "<code>/demotime 10</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("â¬ï¸ Owner Panel", callback_data="owner")]
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
            f"â Demo time à¤à¤¬ <b>{n} minutes</b> à¤¹à¥.",
            parse_mode=ParseMode.HTML,
        )
    except ValueError:
        await update.message.reply_text(
            "â Demo time 1 à¤¸à¥ 1440 minutes à¤à¥ à¤¬à¥à¤ à¤¹à¥à¤¨à¤¾ à¤à¤¾à¤¹à¤¿à¤."
        )


# ============================================================
# DAILY REPORT
# ============================================================
def make_report(date_string=None):
    date_string = date_string or (ist_now().date() - timedelta(days=1)).isoformat()

    with db() as c:
        links = c.execute(
            "SELECT * FROM links ORDER BY created_at"
        ).fetchall()
        members = c.execute(
            "SELECT * FROM members ORDER BY joined_at"
        ).fetchall()

    day_links = [
        r for r in links if local_date(r["created_at"]) == date_string
    ]
    day_members = [
        r for r in members if local_date(r["joined_at"]) == date_string
    ]

    demo_links = [r for r in day_links if r["link_type"] == "demo"]
    perm_links = [r for r in day_links if r["link_type"] == "perm"]
    demo_members = [r for r in day_members if r["link_type"] == "demo"]
    perm_members = [r for r in day_members if r["link_type"] == "perm"]

    text = (
        "ð <b>AUTOMATIC DAILY REPORT</b> ð\n"
        f"ð Date: <b>{date_string}</b> (IST)\n"
        "ââââââââââââââââââ\n\n"
        f"ð <b>LINKS CREATED</b>\n"
        f"ð Demo links: <b>{len(demo_links)}</b>\n"
        f"ð Permanent links: <b>{len(perm_links)}</b>\n\n"
    )

    if day_links:
        for r in day_links:
            kind = "ð DEMO" if r["link_type"] == "demo" else "ð PERMANENT"
            text += (
                f"{kind}\n"
                f"ð Channel: <b>{esc(r['course_name'])}</b>\n"
                f"ð¤ Seller/Admin: <b>{esc(r['creator_name'])}</b>\n"
                f"ð Created: {local_time(r['created_at'])}\n"
                f"ð {'USED' if r['used'] else 'UNUSED'}\n\n"
            )
    else:
        text += "â Aaj koi link create nahi hui.\n\n"

    text += "ââââââââââââââââââ\n\n"
    text += f"ð <b>DEMO MEMBERS â {len(demo_members)}</b>\n\n"

    if demo_members:
        for r in demo_members:
            status = "ð« Auto Removed" if r["status"] == "removed" else "ð¢ Active"
            text += (
                f"ð¤ Seller/Admin: <b>{esc(r['creator_name'])}</b>\n"
                f"ð Channel: <b>{esc(r['course_name'])}</b>\n"
                f"ð¤ Member: <b>{esc(r['full_name'])}</b>\n"
                f"ð ID: <code>{r['user_id']}</code>\n"
                f"ð Joined: {local_time(r['joined_at'])}\n"
                f"{status}\n\n"
            )
    else:
        text += "â No Demo member joined.\n\n"

    text += "ââââââââââââââââââ\n\n"
    text += f"ð <b>PERMANENT MEMBERS â {len(perm_members)}</b>\n\n"

    if perm_members:
        for r in perm_members:
            text += (
                f"ð¤ Seller/Admin: <b>{esc(r['creator_name'])}</b>\n"
                f"ð Channel: <b>{esc(r['course_name'])}</b>\n"
                f"ð¤ Member: <b>{esc(r['full_name'])}</b>\n"
                f"ð ID: <code>{r['user_id']}</code>\n"
                f"ð Joined: {local_time(r['joined_at'])}\n"
                "ð Permanent\n\n"
            )
    else:
        text += "â No Permanent member joined.\n\n"

    text += (
        "ââââââââââââââââââ\n"
        f"ð Links: <b>{len(day_links)}</b> | "
        f"Demo Members: <b>{len(demo_members)}</b> | "
        f"Permanent Members: <b>{len(perm_members)}</b>"
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
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("â Owner only", show_alert=True)
        return

    await q.answer()
    d = (ist_now().date() - timedelta(days=1)).isoformat()
    chunks = split_report_text(make_report(d))

    await edit(
        q,
        chunks[0],
        InlineKeyboardMarkup([
            [InlineKeyboardButton("â¬ï¸ Owner Panel", callback_data="owner")]
        ]),
    )

    for chunk in chunks[1:]:
        await context.bot.send_message(
            OWNER_ID,
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    """Send previous completed IST calendar day exactly once."""
    local = ist_now()

    # Wait until 00:05 IST.
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
            await context.bot.send_message(
                OWNER_ID,
                chunk,
                parse_mode=ParseMode.HTML,
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


# ============================================================
# INLINE SEARCH
# ============================================================
async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip().lower()

    with db() as c:
        rows = c.execute(
            """
            SELECT chat_id,title
            FROM channels
            WHERE bot_status IN ('administrator','creator')
              AND lower(title) LIKE ?
            ORDER BY lower(title)
            LIMIT 50
            """,
            (f"%{query}%",),
        ).fetchall()

    results = []

    for r in rows:
        message = (
            f"ð <b>{esc(r['title'])}</b>\n\n"
            "Tap to open access control."
        )

        results.append(
            InlineQueryResultArticle(
                id=f"channel_{r['chat_id']}",
                title=f"ð {r['title']}",
                description="Auto-detected channel",
                input_message_content=InputTextMessageContent(
                    message,
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "ð Open Control Panel",
                        callback_data=f"channel:{r['chat_id']}",
                    )]
                ]),
            )
        )

    await update.inline_query.answer(
        results,
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
