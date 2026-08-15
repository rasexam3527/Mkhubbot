import os
import sqlite3
import logging
from urllib.parse import urlparse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB = "bot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(DB)


def column_exists(con, table, column):
    rows = con.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(row[1] == column for row in rows)


def init_db():
    con = db()
    cur = con.cursor()

    # USERS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            role TEXT DEFAULT 'user'
        )
    """)

    # SELLERS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sellers (
            user_id INTEGER PRIMARY KEY,
            permission TEXT DEFAULT 'demo'
        )
    """)

    # COURSES
    cur.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icon TEXT DEFAULT '📕',
            photo_file_id TEXT,
            demo_link TEXT,
            demo_material_link TEXT,
            permanent_link TEXT,
            permanent_material_link TEXT,
            channel_link TEXT,
            channel_id INTEGER,
            channel_name TEXT,
            channel_username TEXT
        )
    """)

    # CHANNELS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            username TEXT,
            invite_link TEXT,
            latest_message_id INTEGER,
            latest_message_link TEXT,
            first_material_link TEXT
        )
    """)

    # Migration for older DB
    migrations = [
        ("courses", "demo_material_link", "TEXT"),
        ("courses", "permanent_material_link", "TEXT"),
        ("courses", "channel_id", "INTEGER"),
        ("courses", "channel_name", "TEXT"),
        ("courses", "channel_username", "TEXT"),
    ]

    for table, column, typ in migrations:
        if not column_exists(con, table, column):
            try:
                cur.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {typ}"
                )
            except Exception:
                pass

    con.commit()
    con.close()


# =========================================================
# USER
# =========================================================

def save_user(user):
    if not user:
        return

    con = db()

    row = con.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user.id,),
    ).fetchone()

    if row:
        con.execute(
            """
            UPDATE users
            SET name = ?, username = ?
            WHERE user_id = ?
            """,
            (
                user.first_name or "",
                user.username or "",
                user.id,
            ),
        )
    else:
        con.execute(
            """
            INSERT INTO users
            (user_id, name, username, role)
            VALUES (?, ?, ?, 'user')
            """,
            (
                user.id,
                user.first_name or "",
                user.username or "",
            ),
        )

    con.commit()
    con.close()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def seller_permission(user_id):
    con = db()

    row = con.execute(
        """
        SELECT permission
        FROM sellers
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    con.close()

    return row[0] if row else None


# =========================================================
# TELEGRAM MESSAGE LINK
# =========================================================

def make_message_link(chat_id, message_id, username=None):
    """
    Public channel:
        https://t.me/channelusername/message_id

    Private channel:
        https://t.me/c/internal_id/message_id
    """

    if not chat_id or not message_id:
        return None

    if username:
        username = username.lstrip("@")

        return (
            f"https://t.me/{username}/{message_id}"
        )

    # Telegram private supergroup/channel IDs
    # usually look like -100xxxxxxxxxx
    chat_str = str(chat_id)

    if chat_str.startswith("-100"):
        internal_id = chat_str[4:]

        return (
            f"https://t.me/c/{internal_id}/{message_id}"
        )

    return None


# =========================================================
# CHANNEL SAVE
# =========================================================

def save_channel(
    chat_id,
    title=None,
    username=None,
    invite_link=None,
    latest_message_id=None,
):
    con = db()

    existing = con.execute(
        """
        SELECT first_material_link
        FROM channels
        WHERE chat_id = ?
        """,
        (chat_id,),
    ).fetchone()

    first_material = (
        existing[0]
        if existing and existing[0]
        else None
    )

    latest_link = make_message_link(
        chat_id,
        latest_message_id,
        username,
    )

    con.execute(
        """
        INSERT OR REPLACE INTO channels
        (
            chat_id,
            title,
            username,
            invite_link,
            latest_message_id,
            latest_message_link,
            first_material_link
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            title or "",
            username or "",
            invite_link or "",
            latest_message_id,
            latest_link,
            first_material,
        ),
    )

    con.commit()
    con.close()


# =========================================================
# AUTO CHANNEL POST DETECTION
# =========================================================

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post

    if not post:
        return

    chat = post.chat

    username = getattr(chat, "username", None)

    link = make_message_link(
        chat.id,
        post.message_id,
        username,
    )

    save_channel(
        chat_id=chat.id,
        title=chat.title,
        username=username,
        latest_message_id=post.message_id,
    )

    logger.info(
        "CHANNEL DETECTED: %s | %s | %s",
        chat.id,
        chat.title,
        link,
    )

    # Notify owner
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "📢 *CHANNEL POST DETECTED*\n\n"
                    f"📌 Channel: {chat.title}\n"
                    f"🆔 ID: `{chat.id}`\n"
                    f"🔗 Message: {link or 'Link unavailable'}\n\n"
                    "अगर यह batch का first message है तो "
                    "इसे Material Link बनाया जा सकता है।"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(
                "Owner notification failed: %s",
                e,
            )


# =========================================================
# BOT ADDED / ADMIN DETECTION
# =========================================================

async def my_chat_member(update, context):

    member_update = update.my_chat_member

    if not member_update:
        return

    chat = member_update.chat

    # Only channels
    if chat.type != "channel":
        return

    new_status = member_update.new_chat_member.status

    if new_status not in (
        "administrator",
        "creator",
    ):
        return

    username = getattr(chat, "username", None)

    save_channel(
        chat_id=chat.id,
        title=chat.title,
        username=username,
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "✅ *CHANNEL ADMIN DETECTED*\n\n"
                    f"📢 Channel: {chat.title}\n"
                    f"🆔 ID: `{chat.id}`\n"
                    f"👑 Bot Status: `{new_status}`\n\n"
                    "Bot channel me successfully add/admin hai.\n\n"
                    "⚠️ Purane messages Telegram Bot API "
                    "se automatically scan nahi ho sakte.\n"
                    "Naya channel post aate hi bot uska link "
                    "automatically capture karega."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(
                "Admin notification failed: %s",
                e,
            )


# =========================================================
# MAIN MENU
# =========================================================

def main_menu(user_id):

    buttons = [
        [
            InlineKeyboardButton(
                "📚 Courses",
                callback_data="courses",
            ),
            InlineKeyboardButton(
                "📢 Channels",
                callback_data="channels",
            ),
        ],
        [
            InlineKeyboardButton(
                "👤 My Account",
                callback_data="account",
            ),
        ],
    ]

    if is_admin(user_id):
        buttons.append([
            InlineKeyboardButton(
                "👑 Owner Panel",
                callback_data="admin",
            )
        ])

    return InlineKeyboardMarkup(buttons)


# =========================================================
# START
# =========================================================

async def start(update, context):

    save_user(update.effective_user)

    await update.message.reply_text(
        "🎓 *RAJ COURSE BOT*\n\n"
        f"Hello {update.effective_user.first_name or 'Friend'} ❤️\n\n"
        "Welcome! नीचे से अपना option चुनें 👇",
        reply_markup=main_menu(
            update.effective_user.id
        ),
        parse_mode="Markdown",
    )


# =========================================================
# COURSES
# =========================================================

async def show_courses(query):

    con = db()

    rows = con.execute(
        """
        SELECT id, name, icon
        FROM courses
        ORDER BY id DESC
        """
    ).fetchall()

    con.close()

    if not rows:

        await query.edit_message_text(
            "📚 अभी कोई course available नहीं है।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="back",
                    )
                ]
            ]),
        )

        return

    buttons = []

    for course_id, name, icon in rows:

        buttons.append([
            InlineKeyboardButton(
                f"{icon or '📕'} {name}",
                callback_data=f"course_{course_id}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="back",
        )
    ])

    await query.edit_message_text(
        "📚 *COURSE LIST*\n\n"
        "अपना course select करें 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# =========================================================
# COURSE DETAILS
# =========================================================

async def show_course(query, course_id):

    con = db()

    row = con.execute(
        """
        SELECT
            id,
            name,
            icon,
            photo_file_id,
            demo_link,
            demo_material_link,
            permanent_link,
            permanent_material_link,
            channel_link
        FROM courses
        WHERE id = ?
        """,
        (course_id,),
    ).fetchone()

    con.close()

    if not row:
        await query.answer(
            "Course नहीं मिला ❌",
            show_alert=True,
        )
        return

    (
        course_id,
        name,
        icon,
        photo,
        demo,
        demo_material,
        permanent,
        permanent_material,
        channel,
    ) = row

    user_id = query.from_user.id

    permission = seller_permission(user_id)

    buttons = []

    # -----------------------------------------------------
    # DEMO
    # -----------------------------------------------------

    if demo:
        buttons.append([
            InlineKeyboardButton(
                "🎬 Demo Link",
                url=demo,
            )
        ])

        if demo_material:
            buttons.append([
                InlineKeyboardButton(
                    "📚 Demo Material",
                    url=demo_material,
                )
            ])

    # -----------------------------------------------------
    # PERMANENT
    # -----------------------------------------------------

    if is_admin(user_id):

        if permanent:
            buttons.append([
                InlineKeyboardButton(
                    "🔐 Permanent Link",
                    url=permanent,
                )
            ])

        if permanent_material:
            buttons.append([
                InlineKeyboardButton(
                    "📚 Permanent Material",
                    url=permanent_material,
                )
            ])

    elif permission == "demo_permanent":

        if permanent:
            buttons.append([
                InlineKeyboardButton(
                    "🔐 Permanent Link",
                    url=permanent,
                )
            ])

        if permanent_material:
            buttons.append([
                InlineKeyboardButton(
                    "📚 Permanent Material",
                    url=permanent_material,
                )
            ])

    # -----------------------------------------------------
    # CHANNEL
    # -----------------------------------------------------

    if channel:
        buttons.append([
            InlineKeyboardButton(
                "📢 Course Channel",
                url=channel,
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Courses",
            callback_data="courses",
        )
    ])

    text = (
        f"{icon or '📕'} *{name}*\n\n"
        "नीचे से अपना option select करें 👇"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# =========================================================
# CHANNEL LIST
# =========================================================

async def show_channels(query):

    con = db()

    rows = con.execute(
        """
        SELECT
            chat_id,
            title,
            username,
            latest_message_link
        FROM channels
        ORDER BY title
        """
    ).fetchall()

    con.close()

    if not rows:

        await query.edit_message_text(
            "📢 अभी कोई channel detect नहीं हुआ।\n\n"
            "Bot को channel में Admin बनाओ और "
            "channel में नया post करो।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="back",
                    )
                ]
            ]),
        )

        return

    buttons = []

    for chat_id, title, username, latest_link in rows:

        buttons.append([
            InlineKeyboardButton(
                f"📢 {title or 'Channel'}",
                callback_data=f"detected_channel_{chat_id}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="back",
        )
    ])

    await query.edit_message_text(
        "📢 *DETECTED CHANNELS*\n\n"
        "Channel select करें 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# =========================================================
# DETECTED CHANNEL
# =========================================================

async def show_detected_channel(query, chat_id):

    con = db()

    row = con.execute(
        """
        SELECT
            title,
            username,
            latest_message_link,
            first_material_link
        FROM channels
        WHERE chat_id = ?
        """,
        (chat_id,),
    ).fetchone()

    con.close()

    if not row:
        await query.answer(
            "Channel नहीं मिला।",
            show_alert=True,
        )
        return

    title, username, latest_link, material = row

    buttons = []

    if username:
        buttons.append([
            InlineKeyboardButton(
                "📢 Open Channel",
                url=f"https://t.me/{username}",
            )
        ])

    if latest_link:
        buttons.append([
            InlineKeyboardButton(
                "📚 Latest Material",
                url=latest_link,
            )
        ])

    if material:
        buttons.append([
            InlineKeyboardButton(
                "📖 First / Batch Material",
                url=material,
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Channels",
            callback_data="channels",
        )
    ])

    await query.edit_message_text(
        "📢 *CHANNEL DETAILS*\n\n"
        f"Name: {title or 'Unknown'}\n"
        f"Username: @{username or 'Private'}\n\n"
        "नीचे से open करें 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# =========================================================
# ACCOUNT
# =========================================================

async def show_account(query):

    user = query.from_user

    permission = seller_permission(user.id)

    if is_admin(user.id):
        access = "👑 Owner / Admin"
    elif permission == "demo":
        access = "🟢 Seller - Demo Only"
    elif permission == "demo_permanent":
        access = "🔵 Seller - Demo + Permanent"
    else:
        access = "👤 User - Demo"

    await query.edit_message_text(
        "👤 *MY ACCOUNT*\n\n"
        f"Name: {user.first_name or 'None'}\n"
        f"User ID: `{user.id}`\n"
        f"Username: @{user.username or 'None'}\n\n"
        f"Access: {access}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="back",
                )
            ]
        ]),
        parse_mode="Markdown",
    )


# =========================================================
# ADMIN MENU
# =========================================================

def admin_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Add Course",
                callback_data="admin_add_course",
            ),
            InlineKeyboardButton(
                "📚 Courses",
                callback_data="admin_courses",
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 Detected Channels",
                callback_data="admin_channels",
            ),
        ],
        [
            InlineKeyboardButton(
                "👨‍💼 Sellers",
                callback_data="admin_sellers",
            ),
            InlineKeyboardButton(
                "➕ Add Seller",
                callback_data="admin_add_seller",
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑 Remove Seller",
                callback_data="admin_remove_seller",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Report",
                callback_data="admin_report",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 Main Menu",
                callback_data="back",
            )
        ],
    ])


async def admin_command(update, context):

    if not is_admin(update.effective_user.id):

        await update.message.reply_text(
            "❌ Admin access नहीं है।"
        )

        return

    await update.message.reply_text(
        "👑 *OWNER PANEL*\n\n"
        "नीचे से option चुनें 👇",
        reply_markup=admin_menu(),
        parse_mode="Markdown",
    )


async def show_admin(query):

    await query.edit_message_text(
        "👑 *OWNER PANEL*\n\n"
        "नीचे से option चुनें 👇",
        reply_markup=admin_menu(),
        parse_mode="Markdown",
    )


# =========================================================
# ADD COURSE
# =========================================================

async def start_add_course(query, context):

    if not is_admin(query.from_user.id):
        return

    context.user_data.clear()

    context.user_data["state"] = "course_name"
    context.user_data["course"] = {}

    await query.edit_message_text(
        "➕ *ADD COURSE*\n\n"
        "1️⃣ Course का नाम भेजें:",
        parse_mode="Markdown",
    )


# =========================================================
# ADD SELLER
# =========================================================

async def start_add_seller(query, context):

    if not is_admin(query.from_user.id):
        return

    context.user_data.clear()

    context.user_data["state"] = "seller_id"

    await query.edit_message_text(
        "➕ *ADD SELLER*\n\n"
        "Seller का numeric Telegram User ID भेजें:",
        parse_mode="Markdown",
    )


async def start_remove_seller(query, context):

    if not is_admin(query.from_user.id):
        return

    context.user_data.clear()

    context.user_data["state"] = "remove_seller"

    await query.edit_message_text(
        "🗑 *REMOVE SELLER*\n\n"
        "Seller का Telegram User ID भेजें:",
        parse_mode="Markdown",
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(update, context):

    user = update.effective_user

    if not is_admin(user.id):
        return

    state = context.user_data.get("state")

    if not state:
        return

    value = update.message.text.strip()

    # -----------------------------------------------------
    # SELLER ID
    # -----------------------------------------------------

    if state == "seller_id":

        if not value.isdigit():

            await update.message.reply_text(
                "❌ Numeric User ID भेजो।"
            )

            return

        context.user_data["seller_id"] = int(value)
        context.user_data["state"] = "seller_permission"

        await update.message.reply_text(
            "🔐 Access भेजो:\n\n"
            "`demo`\n"
            "सिर्फ Demo\n\n"
            "या\n\n"
            "`demo_permanent`\n"
            "Demo + Permanent",
            parse_mode="Markdown",
        )

        return

    # -----------------------------------------------------
    # SELLER PERMISSION
    # -----------------------------------------------------

    if state == "seller_permission":

        if value not in (
            "demo",
            "demo_permanent",
        ):

            await update.message.reply_text(
                "❌ सिर्फ `demo` या `demo_permanent` भेजो।",
                parse_mode="Markdown",
            )

            return

        seller_id = context.user_data["seller_id"]

        con = db()

        con.execute(
            """
            INSERT OR REPLACE INTO sellers
            (user_id, permission)
            VALUES (?, ?)
            """,
            (
                seller_id,
                value,
            ),
        )

        con.execute(
            """
            UPDATE users
            SET role = 'seller'
            WHERE user_id = ?
            """,
            (seller_id,),
        )

        con.commit()
        con.close()

        context.user_data.clear()

        access = (
            "🟢 Demo Only"
            if value == "demo"
            else "🔵 Demo + Permanent"
        )

        await update.message.reply_text(
            "✅ *SELLER ADDED*\n\n"
            f"👤 ID: `{seller_id}`\n"
            f"🔐 {access}",
            reply_markup=admin_menu(),
            parse_mode="Markdown",
        )

        return

    # -----------------------------------------------------
    # REMOVE SELLER
    # -----------------------------------------------------

    if state == "remove_seller":

        if not value.isdigit():

            await update.message.reply_text(
                "❌ Numeric User ID भेजो।"
            )

            return

        seller_id = int(value)

        con = db()

        con.execute(
            "DELETE FROM sellers WHERE user_id = ?",
            (seller_id,),
        )

        con.execute(
            """
            UPDATE users
            SET role = 'user'
            WHERE user_id = ?
            """,
            (seller_id,),
        )

        con.commit()
        con.close()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Seller access remove हो गया।",
            reply_markup=admin_menu(),
        )

        return

    # -----------------------------------------------------
    # COURSE NAME
    # -----------------------------------------------------

    if state == "course_name":

        context.user_data["course"]["name"] = value
        context.user_data["state"] = "course_icon"

        await update.message.reply_text(
            "2️⃣ Course के आगे icon भेजें:\n\n"
            "📕 📘 📗 📙 📔 ❤️ 🔥 🎓\n\n"
            "कोई एक भेजें:"
        )

        return

    # -----------------------------------------------------
    # ICON
    # -----------------------------------------------------

    if state == "course_icon":

        context.user_data["course"]["icon"] = value[:10]
        context.user_data["state"] = "course_photo"

        await update.message.reply_text(
            "3️⃣ Course की cover photo भेजें।\n\n"
            "Photo नहीं चाहिए तो `skip` भेजें।"
        )

        return

    # -----------------------------------------------------
    # PHOTO SKIP
    # -----------------------------------------------------

    if state == "course_photo":

        if value.lower() == "skip":

            context.user_data["course"]["photo_file_id"] = None
            context.user_data["state"] = "course_demo"

            await update.message.reply_text(
                "4️⃣ Demo Link भेजें।\n\n"
                "नहीं है तो `skip` भेजें।"
            )

            return

        await update.message.reply_text(
            "📸 Photo भेजो या `skip` लिखो।"
        )

        return

    # -----------------------------------------------------
    # DEMO LINK
    # -----------------------------------------------------

    if state == "course_demo":

        context.user_data["course"]["demo_link"] = (
            None
            if value.lower() == "skip"
            else value
        )

        context.user_data["state"] = "demo_material"

        await update.message.reply_text(
            "5️⃣ *Demo Material Link* भेजें।\n\n"
            "यानी जिस message से Demo Batch/Material शुरू हुआ है "
            "उसका Telegram message link.\n\n"
            "Example:\n"
            "`https://t.me/channel/123`\n\n"
            "नहीं है तो `skip`।",
            parse_mode="Markdown",
        )

        return

    # -----------------------------------------------------
    # DEMO MATERIAL
    # -----------------------------------------------------

    if state == "demo_material":

        context.user_data["course"]["demo_material_link"] = (
            None
            if value.lower() == "skip"
            else value
        )

        context.user_data["state"] = "permanent_link"

        await update.message.reply_text(
            "6️⃣ *Permanent Link* भेजें।\n\n"
            "नहीं है तो `skip`।",
            parse_mode="Markdown",
        )

        return

    # -----------------------------------------------------
    # PERMANENT
    # -----------------------------------------------------

    if state == "permanent_link":

        context.user_data["course"]["permanent_link"] = (
            None
            if value.lower() == "skip"
            else value
        )

        context.user_data["state"] = "permanent_material"

        await update.message.reply_text(
            "7️⃣ *Permanent Material Link* भेजें।\n\n"
            "जिस message से Permanent Batch/Material शुरू हुआ "
            "उसका Telegram message link भेजें.\n\n"
            "नहीं है तो `skip`।",
            parse_mode="Markdown",
        )

        return

    # -----------------------------------------------------
    # PERMANENT MATERIAL
    # -----------------------------------------------------

    if state == "permanent_material":

        context.user_data["course"]["permanent_material_link"] = (
            None
            if value.lower() == "skip"
            else value
        )

        context.user_data["state"] = "channel_link"

        await update.message.reply_text(
            "8️⃣ Course Channel का public link भेजें।\n\n"
            "Example:\n"
            "`https://t.me/yourchannel`\n\n"
            "नहीं है तो `skip`।",
            parse_mode="Markdown",
        )

        return

    # -----------------------------------------------------
    # CHANNEL LINK
    # -----------------------------------------------------

    if state == "channel_link":

        context.user_data["course"]["channel_link"] = (
            None
            if value.lower() == "skip"
            else value
        )

        await save_course(
            update,
            context,
        )

        return


# =========================================================
# COURSE PHOTO
# =========================================================

async def course_photo(update, context):

    if not is_admin(update.effective_user.id):
        return

    if context.user_data.get("state") != "course_photo":
        return

    if not update.message.photo:
        return

    photo = update.message.photo[-1]

    context.user_data["course"]["photo_file_id"] = (
        photo.file_id
    )

    context.user_data["state"] = "course_demo"

    await update.message.reply_text(
        "✅ Cover photo saved!\n\n"
        "4️⃣ Demo Link भेजें।\n"
        "नहीं है तो `skip`।"
    )


# =========================================================
# SAVE COURSE
# =========================================================

async def save_course(update, context):

    course = context.user_data["course"]

    con = db()

    con.execute(
        """
        INSERT INTO courses
        (
            name,
            icon,
            photo_file_id,
            demo_link,
            demo_material_link,
            permanent_link,
            permanent_material_link,
            channel_link
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            course.get("name"),
            course.get("icon") or "📕",
            course.get("photo_file_id"),
            course.get("demo_link"),
            course.get("demo_material_link"),
            course.get("permanent_link"),
            course.get("permanent_material_link"),
            course.get("channel_link"),
        ),
    )

    con.commit()
    con.close()

    name = course.get("name")

    context.user_data.clear()

    await update.message.reply_text(
        "✅ *COURSE SUCCESSFULLY ADDED!*\n\n"
        f"📕 {name}\n\n"
        "🎬 Demo Link: Saved\n"
        "📚 Demo Material: Saved\n"
        "🔐 Permanent Link: Saved\n"
        "📚 Permanent Material: Saved\n"
        "📢 Channel: Saved",
        reply_markup=admin_menu(),
        parse_mode="Markdown",
    )


# =========================================================
# ADMIN COURSES
# =========================================================

async def admin_courses(query):

    con = db()

    rows = con.execute(
        """
        SELECT id, name, icon
        FROM courses
        ORDER BY id DESC
        """
    ).fetchall()

    con.close()

    if not rows:

        await query.edit_message_text(
            "📚 कोई course नहीं है।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "➕ Add Course",
                        callback_data="admin_add_course",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Admin",
                        callback_data="admin",
                    )
                ],
            ]),
        )

        return

    buttons = []

    for course_id, name, icon in rows:

        buttons.append([
            InlineKeyboardButton(
                f"{icon or '📕'} {name}",
                callback_data=f"admin_course_{course_id}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Admin",
            callback_data="admin",
        )
    ])

    await query.edit_message_text(
        "📚 *MANAGE COURSES*\n\n"
        "Course select करें:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# =========================================================
# ADMIN CHANNELS
# =========================================================

async def admin_channels(query):

    con = db()

    rows = con.execute(
        """
        SELECT
            chat_id,
            title,
            username,
            latest_message_link,
            first_material_link
        FROM channels
        ORDER BY title
        """
    ).fetchall()

    con.close()

    if not rows:

        text = (
            "📢 *DETECTED CHANNELS*\n\n"
            "अभी कोई channel detect नहीं हुआ।"
        )

    else:

        text = "📢 *DETECTED CHANNELS*\n\n"

        for chat_id, title, username, latest, first in rows:

            text += (
                f"📢 *{title or 'Channel'}*\n"
                f"🆔 `{chat_id}`\n"
                f"👤 @{username or 'Private'}\n"
                f"🔗 Latest: {'✅' if latest else '❌'}\n"
                f"📚 Material: {'✅' if first else '❌'}\n\n"
            )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Admin",
                    callback_data="admin",
                )
            ]
        ]),
        parse_mode="Markdown",
    )


# =========================================================
# ADMIN COURSE DETAIL
# =========================================================

async def admin_course_detail(query, course_id):

    con = db()

    row = con.execute(
        """
        SELECT
            name,
            demo_link,
            demo_material_link,
            permanent_link,
            permanent_material_link,
            channel_link
        FROM courses
        WHERE id = ?
        """,
        (course_id,),
    ).fetchone()

    con.close()

    if not row:
        await query.answer(
            "Course नहीं मिला।",
            show_alert=True,
        )
        return

    (
        name,
        demo,
        demo_material,
        permanent,
        permanent_material,
        channel,
    ) = row

    text = (
        f"📚 *{name}*\n\n"
        f"🎬 Demo: {'✅' if demo else '❌'}\n"
        f"📚 Demo Material: {'✅' if demo_material else '❌'}\n"
        f"🔐 Permanent: {'✅' if permanent else '❌'}\n"
        f"📚 Permanent Material: {'✅' if permanent_material else '❌'}\n"
        f"📢 Channel: {'✅' if channel else '❌'}"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🗑 Delete Course",
                    callback_data=f"delete_course_{course_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Courses",
                    callback_data="admin_courses",
                )
            ],
        ]),
        parse_mode="Markdown",
    )


# =========================================================
# DELETE COURSE
# =========================================================

async def delete_course(query, course_id):

    if not is_admin(query.from_user.id):
        return

    con = db()

    con.execute(
        "DELETE FROM courses WHERE id = ?",
        (course_id,),
    )

    con.commit()
    con.close()

    await query.answer(
        "Course deleted ✅",
        show_alert=True,
    )

    await admin_courses(query)


# =========================================================
# SELLERS
# =========================================================

async def admin_sellers(query):

    con = db()

    rows = con.execute(
        """
        SELECT user_id, permission
        FROM sellers
        ORDER BY user_id
        """
    ).fetchall()

    con.close()

    text = "👨‍💼 *SELLERS*\n\n"

    if not rows:

        text += "अभी कोई seller नहीं है।"

    else:

        for user_id, permission in rows:

            access = (
                "🟢 Demo Only"
                if permission == "demo"
                else "🔵 Demo + Permanent"
            )

            text += (
                f"👤 `{user_id}`\n"
                f"🔐 {access}\n\n"
            )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "➕ Add Seller",
                    callback_data="admin_add_seller",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Remove Seller",
                    callback_data="admin_remove_seller",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Admin",
                    callback_data="admin",
                )
            ],
        ]),
        parse_mode="Markdown",
    )


# =========================================================
# REPORT
# =========================================================

async def admin_report(query):

    con = db()

    users = con.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    courses = con.execute(
        "SELECT COUNT(*) FROM courses"
    ).fetchone()[0]

    sellers = con.execute(
        "SELECT COUNT(*) FROM sellers"
    ).fetchone()[0]

    channels = con.execute(
        "SELECT COUNT(*) FROM channels"
    ).fetchone()[0]

    con.close()

    await query.edit_message_text(
        "📊 *BOT REPORT*\n\n"
        f"👥 Users: `{users}`\n"
        f"📚 Courses: `{courses}`\n"
        f"👨‍💼 Sellers: `{sellers}`\n"
        f"📢 Detected Channels: `{channels}`",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Admin",
                    callback_data="admin",
                )
            ]
        ]),
        parse_mode="Markdown",
    )


# =========================================================
# CALLBACKS
# =========================================================

async def buttons(update, context):

    query = update.callback_query

    await query.answer()

    data = query.data
    user_id = query.from_user.id

    # BACK
    if data == "back":

        await query.edit_message_text(
            "🎓 *RAJ COURSE BOT*\n\n"
            "Welcome! नीचे से अपना option चुनें 👇",
            reply_markup=main_menu(user_id),
            parse_mode="Markdown",
        )

        return

    # COURSES
    if data == "courses":

        await show_courses(query)

        return

    # COURSE
    if data.startswith("course_"):

        course_id = int(
            data.split("_")[1]
        )

        await show_course(
            query,
            course_id,
        )

        return

    # CHANNELS
    if data == "channels":

        await show_channels(query)

        return

    # DETECTED CHANNEL
    if data.startswith("detected_channel_"):

        chat_id = int(
            data.split("_")[2]
        )

        await show_detected_channel(
            query,
            chat_id,
        )

        return

    # ACCOUNT
    if data == "account":

        await show_account(query)

        return

    # ADMIN
    if data == "admin":

        if is_admin(user_id):
            await show_admin(query)

        return

    # ADD COURSE
    if data == "admin_add_course":

        if is_admin(user_id):
            await start_add_course(
                query,
                context,
            )

        return

    # COURSES ADMIN
    if data == "admin_courses":

        if is_admin(user_id):
            await admin_courses(query)

        return

    # ADMIN COURSE DETAIL
    if data.startswith("admin_course_"):

        if is_admin(user_id):

            course_id = int(
                data.split("_")[2]
            )

            await admin_course_detail(
                query,
                course_id,
            )

        return

    # DELETE COURSE
    if data.startswith("delete_course_"):

        if is_admin(user_id):

            course_id = int(
                data.split("_")[2]
            )

            await delete_course(
                query,
                course_id,
            )

        return

    # ADMIN CHANNELS
    if data == "admin_channels":

        if is_admin(user_id):
            await admin_channels(query)

        return

    # SELLERS
    if data == "admin_sellers":

        if is_admin(user_id):
            await admin_sellers(query)

        return

    # ADD SELLER
    if data == "admin_add_seller":

        if is_admin(user_id):
            await start_add_seller(
                query,
                context,
            )

        return

    # REMOVE SELLER
    if data == "admin_remove_seller":

        if is_admin(user_id):
            await start_remove_seller(
                query,
                context,
            )

        return

    # REPORT
    if data == "admin_report":

        if is_admin(user_id):
            await admin_report(query)

        return


# =========================================================
# CANCEL
# =========================================================

async def cancel(update, context):

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=main_menu(
            update.effective_user.id
        ),
    )


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):

    logger.error(
        "Exception:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:
        raise ValueError(
            "BOT_TOKEN is missing"
        )

    if not ADMIN_IDS:
        raise ValueError(
            "ADMIN_IDS is missing"
        )

    init_db()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel,
        )
    )

    # Channel posts
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            channel_post,
        )
    )

    # Bot added/admin in channel
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.MY_CHAT_MEMBER,
            my_chat_member,
        )
    )

    # Course photo
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            course_photo,
        )
    )

    # Admin text
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            admin_text,
        )
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(
            buttons,
        )
    )

    app.add_error_handler(
        error_handler
    )

    print(
        "RAJ COURSE BOT is running..."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
