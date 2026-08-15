import os
import html
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

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
# CONFIG
# ============================================================
# GitHub/hosting Secrets में ये दो values रखो:
# BOT_TOKEN = Telegram bot token
# OWNER_ID  = अपना numeric Telegram user ID
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "bot.db")

ADD_ADMIN, ADD_CATEGORY, ADD_COURSE = range(3)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)


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
            emoji TEXT NOT NULL DEFAULT '📚',
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS courses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            tg_id TEXT NOT NULL UNIQUE
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

        CREATE TABLE IF NOT EXISTS reports(
            report_date TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        );
        """)

        defaults = [
            ("📚", "Teaching Exam's"),
            ("🚩", "Ras/Psi"),
            ("📝", "EO-Ro/bstc/cet"),
            ("🎓", "Net-Jrf"),
            ("✨", "Other Exam's"),
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


def iso(x):
    return x.isoformat() if x else None


def esc(x):
    return html.escape(str(x or ""))


def demo_minutes():
    with db() as c:
        r = c.execute(
            "SELECT value FROM settings WHERE key='demo_minutes'"
        ).fetchone()
    try:
        return int(r["value"])
    except Exception:
        return 5


def owner(uid):
    return OWNER_ID and uid == OWNER_ID


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


def can(uid, p):
    pset = perms(uid)
    return "all" in pset or p in pset


def admin_name(uid):
    if owner(uid):
        return "Owner"
    with db() as c:
        r = c.execute(
            "SELECT name FROM admins WHERE uid=?", (uid,)
        ).fetchone()
    return r["name"] if r else f"Admin {uid}"


def user_name(u):
    name = " ".join(x for x in [u.first_name, u.last_name] if x).strip()
    return name or (f"@{u.username}" if u.username else str(u.id))


def member_label(r):
    n = esc(r["full_name"])
    return f"{n} (@{esc(r['username'])})" if r["username"] else n


def rows_buttons(items, cols=2):
    return [
        items[i:i + cols] for i in range(0, len(items), cols)
    ]


async def edit(q, text, kb=None):
    try:
        await q.edit_message_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        log.warning("edit failed: %s", e)


# ============================================================
# HOME / COURSE LIST
# ============================================================
def home_kb():
    with db() as c:
        rows = c.execute(
            "SELECT id,emoji,name FROM categories ORDER BY id"
        ).fetchall()

    b = [
        InlineKeyboardButton(
            f"{r['emoji']} {r['name']}",
            callback_data=f"cat:{r['id']}"
        ) for r in rows
    ]
    kb = rows_buttons(b, 2)

    if OWNER_ID:
        kb.append([
            InlineKeyboardButton("⚙️ Owner Panel", callback_data="owner")
        ])
    return InlineKeyboardMarkup(kb)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin(uid):
        if update.message:
            await update.message.reply_text("❌ Access denied.")
        return

    text = (
        "👑 <b>RAJ COURSE BOT</b>\n\n"
        "🌸 <b>Hello Seller Family, Kese Ho</b>\n\n"
        "I AM WIZARD 🌸💕\n\n"
        "🔎 <b>Course Search</b>\n"
        "Telegram ke kisi chat me likho:\n"
        "<code>@YourBotUsername course-name</code>\n\n"
        "Search result me course select karo."
    )

    if update.callback_query:
        await update.callback_query.answer()
        await edit(update.callback_query, text, home_kb())
    else:
        await update.message.reply_text(
            text, reply_markup=home_kb(), parse_mode=ParseMode.HTML
        )


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

    # Course names are clean inline buttons with the correct emoji.
    b = [
        InlineKeyboardButton(
            f"📚 {r['name']}",
            callback_data=f"course:{r['id']}"
        ) for r in courses
    ]
    kb = rows_buttons(b, 2)

    if owner(q.from_user.id):
        kb.append([
            InlineKeyboardButton(
                "➕ Add Course",
                callback_data=f"addcourse:{cid}"
            )
        ])

    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])

    await edit(
        q,
        f"{cat['emoji']} <b>{esc(cat['name'])}</b>\n\n"
        f"Courses: {len(courses)}",
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
        return

    buttons = []
    if can(q.from_user.id, "demo"):
        buttons.append(
            InlineKeyboardButton(
                "🔗 Demo Link",
                callback_data=f"link:demo:{course_id}"
            )
        )
    if can(q.from_user.id, "perm"):
        buttons.append(
            InlineKeyboardButton(
                "👤 Permanent Link",
                callback_data=f"link:perm:{course_id}"
            )
        )

    if not buttons:
        await q.answer("❌ No access", show_alert=True)
        return

    kb = rows_buttons(buttons, 2)
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=f"cat:{r['category_id']}")])

    await edit(
        q,
        f"📚 <b>{esc(r['name'])}</b>\n\n"
        "Choose an access link below:",
        InlineKeyboardMarkup(kb)
    )


# ============================================================
# LINK CREATION
# ============================================================
async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, typ, course_id_s = q.data.split(":")
    course_id = int(course_id_s)

    if not can(q.from_user.id, typ):
        await q.answer("❌ Permission denied", show_alert=True)
        return

    with db() as c:
        r = c.execute(
            "SELECT * FROM courses WHERE id=?", (course_id,)
        ).fetchone()

    if not r:
        return

    try:
        chat_id = int(r["tg_id"])
        created = now()
        creator = admin_name(q.from_user.id)

        # IMPORTANT:
        # creates_join_request=False = user joins directly.
        # member_limit=1 = every generated link is single-use.
        link = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=False,
            member_limit=1,
            name=f"{typ}_{q.from_user.id}_{created.strftime('%Y%m%d%H%M%S')}"
        )

        with db() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO links
                VALUES(?,?,?,?,?,?,?,?,0)
                """,
                (
                    link.invite_link, str(chat_id), course_id,
                    r["name"], typ, q.from_user.id, creator,
                    iso(created)
                )
            )

        if typ == "demo":
            title = "🎓 <b>Access Granted: Demo Pass</b> ⏳"
            body = (
                f"━━━━━━━━━━━━━━\n\n"
                f"🏢 <b>चैनल / कोर्स का नाम:</b>\n"
                f"➜ {esc(r['name'])}\n\n"
                f"📥 <b>यहाँ से ज्वाइन करें:</b>\n"
                f"🔗 <a href=\"{esc(link.invite_link)}\">{esc(link.invite_link)}</a>\n\n"
                f"📌 <b>महत्वपूर्ण निर्देश:</b>\n"
                f"⚠️ यह Demo Joining Link केवल 1 बार काम करेगा।\n"
                f"⏱ सिस्टम आपको चैनल में जुड़ने के ठीक "
                f"<b>{demo_minutes()} मिनट</b> बाद automatic remove करेगा।"
            )
        else:
            title = "💎 <b>Access Granted: Permanent Pass</b> 💎"
            body = (
                f"━━━━━━━━━━━━━━\n\n"
                f"🏢 <b>चैनल / कोर्स का नाम:</b>\n"
                f"➜ {esc(r['name'])}\n\n"
                f"📥 <b>यहाँ से ज्वाइन करें:</b>\n"
                f"🔗 <a href=\"{esc(link.invite_link)}\">{esc(link.invite_link)}</a>\n\n"
                f"📌 <b>महत्वपूर्ण निर्देश:</b>\n"
                f"⚠️ यह Permanent Joining Link केवल 1 बार काम करेगा।"
            )

        await edit(
            q,
            f"{title}\n\n{body}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Back", callback_data=f"course:{course_id}"
                )]
            ])
        )

    except Exception:
        log.exception("create_link")
        await q.answer(
            "❌ Link create nahi hua. Channel me bot ki admin permissions check karo.",
            show_alert=True
        )


# ============================================================
# ACTUAL MEMBER JOIN TRACKING
# ============================================================
async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm:
        return

    old = cm.old_chat_member.status
    new = cm.new_chat_member.status

    # Real join only.
    if new not in ("member", "administrator"):
        return
    if old not in ("left", "kicked"):
        return

    invite = getattr(cm, "invite_link", None)
    invite_url = invite.invite_link if invite else None

    # We only report joins that came through one of our generated links.
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
        c.execute(
            """
            INSERT INTO members
            (user_id,username,full_name,chat_id,course_id,course_name,
             invite_link,link_type,creator_id,creator_name,joined_at,
             expires_at,status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                u.id, u.username, user_name(u), str(cm.chat.id),
                link["course_id"], link["course_name"], invite_url,
                link["link_type"], link["creator_id"], link["creator_name"],
                iso(joined), iso(expires), "active"
            )
        )
        c.execute(
            "UPDATE links SET used=1 WHERE invite_link=?",
            (invite_url,)
        )

    # Owner gets a simple live notification.
    if OWNER_ID:
        kind = "🎓 DEMO" if link["link_type"] == "demo" else "💎 PERMANENT"
        msg = (
            f"{kind} <b>MEMBER JOINED</b>\n\n"
            f"👤 Admin: <b>{esc(link['creator_name'])}</b>\n"
            f"📚 Course: <b>{esc(link['course_name'])}</b>\n"
            f"👤 Member: <b>{esc(user_name(u))}</b>\n"
            f"🆔 ID: <code>{u.id}</code>\n"
            f"🕐 Joined: {joined.strftime('%d/%m/%Y %H:%M:%S')}"
        )
        try:
            await context.bot.send_message(
                OWNER_ID, msg, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


# ============================================================
# AUTO REMOVE AFTER DEMO TIME
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
            (iso(current),)
        ).fetchall()

    for r in rows:
        try:
            # Ban removes the member immediately.
            await context.bot.ban_chat_member(
                chat_id=int(r["chat_id"]),
                user_id=int(r["user_id"])
            )

            # Unban immediately: member is removed but can use a future
            # new link again. If you want a permanent ban, delete this block.
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
            log.error(
                "AUTO REMOVE failed: user=%s chat=%s error=%s",
                r["user_id"], r["chat_id"], e
            )


# ============================================================
# OWNER PANEL / ACCESS
# ============================================================
async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return
    await q.answer()

    with db() as c:
        a = c.execute("SELECT * FROM admins").fetchall()
        cats = c.execute("SELECT * FROM categories").fetchall()
        courses = c.execute("SELECT * FROM courses").fetchall()

    text = (
        "👑 <b>OWNER PANEL</b>\n\n"
        f"👥 Admins: {len(a)}\n"
        f"📁 Categories: {len(cats)}\n"
        f"📚 Courses: {len(courses)}\n"
        f"⏱ Demo Time: {demo_minutes()} minutes"
    )

    kb = [
        [
            InlineKeyboardButton("➕ Add Admin", callback_data="addadmin"),
            InlineKeyboardButton("✏️ Edit Access", callback_data="editadmins")
        ],
        [
            InlineKeyboardButton("➕ New Category", callback_data="addcat"),
            InlineKeyboardButton("➕ Add Course", callback_data="pickcat")
        ],
        [
            InlineKeyboardButton("⏱ Demo Time", callback_data="showtime"),
            InlineKeyboardButton("📊 Daily Report", callback_data="report")
        ],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")]
    ]
    await edit(q, text, InlineKeyboardMarkup(kb))


async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return ConversationHandler.END
    await q.answer()

    await edit(
        q,
        "➕ <b>ADD ADMIN</b>\n\n"
        "Sirf User ID bhejo:\n"
        "<code>123456789</code>\n\n"
        "Uske baad Demo Only ya Both access choose karo.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="owner")]
        ])
    )
    return ADD_ADMIN


async def add_admin_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return ConversationHandler.END

    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ User ID number hona chahiye.")
        return ADD_ADMIN

    if uid == OWNER_ID:
        await update.message.reply_text("❌ Owner already exists.")
        return ConversationHandler.END

    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO admins(uid,name,permissions) VALUES(?,?,?)",
            (uid, f"Admin {uid}", "demo")
        )

    await update.message.reply_text(
        f"✅ Admin added: <code>{uid}</code>\n\nAccess choose karo:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔗 Demo Only", callback_data=f"access:demo:{uid}"
                ),
                InlineKeyboardButton(
                    "🔗+👤 Both", callback_data=f"access:both:{uid}"
                )
            ],
            [InlineKeyboardButton("⬅️ Owner", callback_data="owner")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END


async def set_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return
    await q.answer()

    _, access, uid_s = q.data.split(":")
    uid = int(uid_s)
    p = "demo" if access == "demo" else "demo,perm"

    with db() as c:
        c.execute(
            "UPDATE admins SET permissions=? WHERE uid=?",
            (p, uid)
        )

    await edit(
        q,
        f"✅ <b>Access Updated</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"🔐 Access: {'Demo Only' if access == 'demo' else 'Demo + Permanent'}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner")]
        ])
    )


async def edit_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
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
                f"{r['uid']} — {r['permissions']}",
                callback_data=f"chooseaccess:{r['uid']}"
            )
        ])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="owner")])
    await edit(q, "✏️ <b>EDIT ACCESS</b>", InlineKeyboardMarkup(kb))


async def choose_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return
    await q.answer()
    uid = int(q.data.split(":")[1])

    await edit(
        q,
        f"🆔 Admin: <code>{uid}</code>\n\nSelect access:",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔗 Demo Only", callback_data=f"access:demo:{uid}"
                ),
                InlineKeyboardButton(
                    "🔗+👤 Both", callback_data=f"access:both:{uid}"
                )
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="editadmins")]
        ])
    )


async def show_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return
    await q.answer()
    await edit(
        q,
        f"⏱ <b>Current Demo Time: {demo_minutes()} minutes</b>\n\n"
        "Owner chat me command bhejo:\n"
        "<code>/demotime 10</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner")]
        ])
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
                "INSERT OR REPLACE INTO settings(key,value) VALUES('demo_minutes',?)",
                (str(n),)
            )
        await update.message.reply_text(
            f"✅ Demo time अब {n} minutes है."
        )
    except ValueError:
        await update.message.reply_text("❌ 1 से 1440 के बीच number दो.")


# ============================================================
# CATEGORY / COURSE ADD
# ============================================================
async def add_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return ConversationHandler.END
    await q.answer()

    await edit(
        q,
        "➕ <b>NEW CATEGORY</b>\n\n"
        "Format:\n<code>📚 | Category Name</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="owner")]
        ])
    )
    return ADD_CATEGORY


async def add_category_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return ConversationHandler.END

    p = [x.strip() for x in update.message.text.split("|", 1)]
    if len(p) != 2:
        await update.message.reply_text("❌ Format: 📚 | Category Name")
        return ADD_CATEGORY

    try:
        with db() as c:
            c.execute(
                "INSERT INTO categories(emoji,name) VALUES(?,?)",
                (p[0], p[1])
            )
        await update.message.reply_text(f"✅ {p[0]} {p[1]} added.")
        return ConversationHandler.END
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Category already exists.")
        return ADD_CATEGORY


async def pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return
    await q.answer()

    with db() as c:
        rows = c.execute(
            "SELECT * FROM categories ORDER BY id"
        ).fetchall()

    kb = [
        [InlineKeyboardButton(
            f"{r['emoji']} {r['name']}",
            callback_data=f"addcourse:{r['id']}"
        )]
        for r in rows
    ]
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="owner")])
    await edit(q, "📚 Select category:", InlineKeyboardMarkup(kb))


async def add_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return ConversationHandler.END
    await q.answer()

    context.user_data["category_id"] = int(q.data.split(":")[1])
    await edit(
        q,
        "➕ <b>ADD COURSE</b>\n\n"
        "Format:\n<code>Course Name | -1001234567890</code>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="owner")]
        ])
    )
    return ADD_COURSE


async def add_course_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return ConversationHandler.END

    cid = context.user_data.get("category_id")
    p = [x.strip() for x in update.message.text.split("|", 1)]

    if not cid or len(p) != 2:
        await update.message.reply_text(
            "❌ Format: Course Name | -1001234567890"
        )
        return ADD_COURSE

    if not p[1].startswith("-100"):
        await update.message.reply_text("❌ Channel ID -100 से शुरू होना चाहिए.")
        return ADD_COURSE

    try:
        await context.bot.get_chat(int(p[1]))
        with db() as c:
            c.execute(
                "INSERT INTO courses(category_id,name,tg_id) VALUES(?,?,?)",
                (cid, p[0], p[1])
            )
        await update.message.reply_text(f"✅ 📚 {p[0]} added.")
        return ConversationHandler.END
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ यह channel/course पहले से है.")
        return ADD_COURSE
    except Exception:
        await update.message.reply_text(
            "❌ Bot channel access नहीं कर पा रहा. Bot को channel में admin बनाओ."
        )
        return ADD_COURSE


# ============================================================
# DAILY REPORT
# ============================================================
def make_report(date_string):
    with db() as c:
        demo = c.execute(
            """
            SELECT * FROM members
            WHERE date(joined_at)=? AND link_type='demo'
            ORDER BY joined_at
            """, (date_string,)
        ).fetchall()

        perm = c.execute(
            """
            SELECT * FROM members
            WHERE date(joined_at)=? AND link_type='perm'
            ORDER BY joined_at
            """, (date_string,)
        ).fetchall()

    text = (
        "📊 <b>AUTOMATIC DAILY REPORT</b> 📊\n"
        f"📅 Date: {date_string}\n"
        "━━━━━━━━━━━━━━\n\n"
        f"⏳ <b>[ DEMO ADD REPORT ]</b> ({len(demo)})\n\n"
    )

    if not demo:
        text += "❌ Aaj koi Demo member add nahi hua.\n"
    else:
        for r in demo:
            text += (
                f"👤 <b>Admin:</b> {esc(r['creator_name'])}\n"
                f"📚 <b>Course:</b> {esc(r['course_name'])}\n"
                f"👤 <b>Member:</b> {member_label(r)}\n"
                f"🆔 <b>ID:</b> <code>{r['user_id']}</code>\n"
                f"🕐 <b>Joined:</b> {r['joined_at']}\n"
                f"🚫 <b>Status:</b> {esc(r['status'])}\n\n"
            )

    text += (
        "━━━━━━━━━━━━━━\n\n"
        f"💎 <b>[ PERMANENT ADD REPORT ]</b> ({len(perm)})\n\n"
    )

    if not perm:
        text += "❌ Aaj koi Permanent member add nahi hua.\n"
    else:
        for r in perm:
            text += (
                f"👤 <b>Admin:</b> {esc(r['creator_name'])}\n"
                f"📚 <b>Course:</b> {esc(r['course_name'])}\n"
                f"👤 <b>Member:</b> {member_label(r)}\n"
                f"🆔 <b>ID:</b> <code>{r['user_id']}</code>\n"
                f"🕐 <b>Joined:</b> {r['joined_at']}\n"
                "✅ <b>Status:</b> Permanent\n\n"
            )

    return text


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update.effective_user.id):
        return
    await update.message.reply_text(
        make_report(now().date().isoformat()),
        parse_mode=ParseMode.HTML
    )


async def report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not owner(q.from_user.id):
        await q.answer("❌ Owner only", show_alert=True)
        return
    await q.answer()
    await edit(
        q,
        make_report(now().date().isoformat()),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner")]
        ])
    )


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    # UTC daily report for the previous date.
    d = (now() - timedelta(days=1)).date().isoformat()

    with db() as c:
        if c.execute(
            "SELECT 1 FROM reports WHERE report_date=?", (d,)
        ).fetchone():
            return
        c.execute(
            "INSERT INTO reports(report_date,sent_at) VALUES(?,?)",
            (d, iso(now()))
        )

    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID, make_report(d), parse_mode=ParseMode.HTML
            )
        except Exception:
            log.exception("daily report send failed")


# ============================================================
# INLINE SEARCH
# ============================================================
async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.inline_query.query or "").strip().lower()

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
            (f"%{q}%",)
        ).fetchall()

    results = []
    for r in rows:
        title = f"{r['emoji']} {r['name']}"
        message = (
            f"{r['emoji']} <b>{esc(r['name'])}</b>\n\n"
            "Tap to open control panel."
        )
        results.append(
            InlineQueryResultArticle(
                id=f"course_{r['id']}",
                title=title,
                description=f"{r['cat']} • Course",
                input_message_content=InputTextMessageContent(
                    message,
                    parse_mode=ParseMode.HTML
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "📚 Open Control Panel",
                        callback_data=f"course:{r['id']}"
                    )]
                ])
            )
        )

    await update.inline_query.answer(
        results=results,
        cache_time=1,
        is_personal=True
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data or ""

    if d == "home":
        await start(update, context)
    elif d.startswith("cat:"):
        await category(update, context)
    elif d.startswith("course:"):
        await course(update, context)
    elif d.startswith("link:"):
        await create_link(update, context)
    elif d == "owner":
        await owner_panel(update, context)
    elif d == "editadmins":
        await edit_admins(update, context)
    elif d.startswith("chooseaccess:"):
        await choose_access(update, context)
    elif d.startswith("access:"):
        await set_access(update, context)
    elif d == "showtime":
        await show_time(update, context)
    elif d == "report":
        await report_button(update, context)
    elif d == "addcat":
        await add_category_start(update, context)
    elif d == "pickcat":
        await pick_category(update, context)
    elif d.startswith("addcourse:"):
        await add_course_start(update, context)
    else:
        await q.answer()


# ============================================================
# MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN missing. GitHub Secrets में BOT_TOKEN set करो."
        )
    if not OWNER_ID:
        raise RuntimeError(
            "OWNER_ID missing. GitHub Secrets में OWNER_ID set करो."
        )

    app = Application.builder().token(BOT_TOKEN).build()

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

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(add_course_start, pattern=r"^addcourse:\d+$")
            ],
            states={
                ADD_COURSE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_save)
                ]
            },
            fallbacks=[]
        )
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("demotime", demotime))
    app.add_handler(CommandHandler("dailyreport", report))
    app.add_handler(InlineQueryHandler(inline_search))

    # Required for actual member-join tracking.
    app.add_handler(
        ChatMemberHandler(
            chat_member_update,
            ChatMemberHandler.CHAT_MEMBER
        )
    )

    app.add_handler(CallbackQueryHandler(callbacks))

    # Demo expiry check every 15 seconds.
    app.job_queue.run_repeating(demo_job, interval=15, first=10)

    # Daily report check every minute.
    app.job_queue.run_repeating(daily_job, interval=60, first=20)

    log.info("Bot started")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
