import os
import sqlite3
import logging

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError


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


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            status TEXT DEFAULT 'normal'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,

            demo_chat_id INTEGER,
            demo_chat_username TEXT,
            demo_message_id INTEGER,

            permanent_chat_id INTEGER,
            permanent_chat_username TEXT,
            permanent_message_id INTEGER
        )
    """)

    con.commit()
    con.close()


def save_user(user):
    con = db()

    con.execute("""
        INSERT OR IGNORE INTO users
        (user_id, name, username, status)
        VALUES (?, ?, ?, 'normal')
    """, (
        user.id,
        user.first_name or "",
        user.username or "",
    ))

    con.execute("""
        UPDATE users
        SET name = ?, username = ?
        WHERE user_id = ?
    """, (
        user.first_name or "",
        user.username or "",
        user.id,
    ))

    con.commit()
    con.close()


def get_user_status(user_id):
    con = db()

    row = con.execute(
        "SELECT status FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    con.close()

    return row[0] if row else "normal"


def is_admin(user_id):
    return user_id in ADMIN_IDS


# =========================================================
# MAIN MENU
# =========================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📚 Courses",
                callback_data="courses"
            ),
            InlineKeyboardButton(
                "👤 My Account",
                callback_data="account"
            ),
        ],
        [
            InlineKeyboardButton(
                "🎁 Gift",
                callback_data="gift"
            ),
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)

    await update.message.reply_text(
        "🎓 *RAJ COURSE BOT*\n\n"
        "Welcome Seller Family ❤️\n\n"
        "नीचे से option चुनें 👇",
        reply_markup=main_menu(),
        parse_mode="Markdown",
    )


# =========================================================
# CHANNEL LINK HELPERS
# =========================================================

async def check_channel_admin(bot, user_id, chat_id):
    """
    Check whether bot is administrator in channel.
    """

    try:
        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=bot.id
        )

        if member.status != ChatMemberStatus.ADMINISTRATOR:
            return False, "Bot channel ka administrator nahi hai."

        # Check invite permission
        if hasattr(member, "can_invite_users"):
            if member.can_invite_users is False:
                return False, (
                    "Bot admin hai, lekin "
                    "'Invite Users via Link' permission OFF hai."
                )

        return True, None

    except TelegramError as e:
        logger.exception(e)

        return False, (
            "Channel access check nahi ho saka.\n\n"
            "Bot ko channel me administrator rakho."
        )


async def create_channel_link(bot, chat_id):
    """
    Public channel:
        https://t.me/username

    Private channel:
        Creates invite link.
    """

    try:
        chat = await bot.get_chat(chat_id)

        # PUBLIC CHANNEL
        if chat.username:
            return f"https://t.me/{chat.username}"

        # PRIVATE CHANNEL
        invite = await bot.create_chat_invite_link(
            chat_id=chat.id,
            name="RAJ COURSE BOT",
            creates_join_request=False,
        )

        return invite.invite_link

    except TelegramError as e:
        logger.exception(e)
        return None


def material_link(chat_id, username, message_id):
    """
    Creates link to the first/material message.
    """

    if not message_id:
        return None

    # Public channel
    if username:
        return f"https://t.me/{username}/{message_id}"

    # Private channel
    if chat_id:
        clean_id = str(chat_id)

        if clean_id.startswith("-100"):
            clean_id = clean_id[4:]

        return f"https://t.me/c/{clean_id}/{message_id}"

    return None


# =========================================================
# COURSES
# =========================================================

async def show_courses(query):
    con = db()

    rows = con.execute("""
        SELECT id, name
        FROM courses
        ORDER BY id DESC
    """).fetchall()

    con.close()

    if not rows:
        await query.edit_message_text(
            "📚 अभी कोई course available नहीं है।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="back"
                    )
                ]
            ])
        )
        return

    buttons = []

    for course_id, name in rows:
        buttons.append([
            InlineKeyboardButton(
                f"📕 {name}",
                callback_data=f"course_{course_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="back"
        )
    ])

    await query.edit_message_text(
        "📚 *COURSE LIST*\n\n"
        "अपना course select करें 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def show_course(query, course_id):
    con = db()

    row = con.execute("""
        SELECT
            name,
            demo_chat_id,
            demo_chat_username,
            demo_message_id,
            permanent_chat_id,
            permanent_chat_username,
            permanent_message_id
        FROM courses
        WHERE id = ?
    """, (course_id,)).fetchone()

    con.close()

    if not row:
        await query.answer(
            "Course नहीं मिला.",
            show_alert=True
        )
        return

    (
        name,
        demo_chat_id,
        demo_username,
        demo_message_id,
        permanent_chat_id,
        permanent_username,
        permanent_message_id,
    ) = row

    status = get_user_status(query.from_user.id)

    buttons = []

    # -----------------------------------------------------
    # DEMO
    # -----------------------------------------------------

    if demo_chat_id:
        buttons.append([
            InlineKeyboardButton(
                "🎬 Demo Link",
                callback_data=f"demo_{course_id}"
            )
        ])

        demo_material = material_link(
            demo_chat_id,
            demo_username,
            demo_message_id
        )

        if demo_material:
            buttons.append([
                InlineKeyboardButton(
                    "📚 Demo Material",
                    url=demo_material
                )
            ])

    # -----------------------------------------------------
    # PERMANENT
    # -----------------------------------------------------

    if status == "full" or is_admin(query.from_user.id):

        if permanent_chat_id:
            buttons.append([
                InlineKeyboardButton(
                    "🔐 Permanent Link",
                    callback_data=f"permanent_{course_id}"
                )
            ])

            permanent_material = material_link(
                permanent_chat_id,
                permanent_username,
                permanent_message_id
            )

            if permanent_material:
                buttons.append([
                    InlineKeyboardButton(
                        "📚 Permanent Material",
                        url=permanent_material
                    )
                ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Courses",
            callback_data="courses"
        )
    ])

    await query.edit_message_text(
        f"📕 *{name}*\n\n"
        "नीचे अपना access चुनें 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# =========================================================
# ACCESS LINK
# =========================================================

async def send_demo_link(query, course_id):
    con = db()

    row = con.execute("""
        SELECT
            name,
            demo_chat_id,
            demo_chat_username
        FROM courses
        WHERE id = ?
    """, (course_id,)).fetchone()

    con.close()

    if not row:
        await query.answer(
            "Course नहीं मिला.",
            show_alert=True
        )
        return

    name, chat_id, username = row

    if not chat_id:
        await query.answer(
            "Demo channel set नहीं है.",
            show_alert=True
        )
        return

    link = await create_channel_link(
        query.get_bot(),
        chat_id
    )

    if not link:
        await query.answer(
            "Demo link create नहीं हुआ.\n"
            "Bot ki channel permissions check karo.",
            show_alert=True
        )
        return

    await query.message.reply_text(
        f"🎬 *{name} - DEMO ACCESS*\n\n"
        "नीचे से Demo Channel join करें 👇",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔗 Open Demo",
                    url=link
                )
            ]
        ]),
        parse_mode="Markdown",
    )


async def send_permanent_link(query, course_id):
    status = get_user_status(query.from_user.id)

    if status != "full" and not is_admin(query.from_user.id):
        await query.answer(
            "❌ Permanent access available नहीं है.",
            show_alert=True
        )
        return

    con = db()

    row = con.execute("""
        SELECT
            name,
            permanent_chat_id,
            permanent_chat_username
        FROM courses
        WHERE id = ?
    """, (course_id,)).fetchone()

    con.close()

    if not row:
        await query.answer(
            "Course नहीं मिला.",
            show_alert=True
        )
        return

    name, chat_id, username = row

    if not chat_id:
        await query.answer(
            "Permanent channel set नहीं है.",
            show_alert=True
        )
        return

    link = await create_channel_link(
        query.get_bot(),
        chat_id
    )

    if not link:
        await query.answer(
            "Permanent link create नहीं हुआ.",
            show_alert=True
        )
        return

    await query.message.reply_text(
        f"🔐 *{name} - PERMANENT ACCESS*\n\n"
        "नीचे से Permanent Channel join करें 👇",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔗 Open Permanent",
                    url=link
                )
            ]
        ]),
        parse_mode="Markdown",
    )


# =========================================================
# ACCOUNT
# =========================================================

async def show_account(query):
    user = query.from_user
    status = get_user_status(user.id)

    if status == "demo":
        access = "🎬 Demo Only"
    elif status == "full":
        access = "🎬 Demo + 🔐 Permanent"
    elif is_admin(user.id):
        access = "👑 Owner"
    else:
        access = "Normal"

    await query.edit_message_text(
        "👤 *MY ACCOUNT*\n\n"
        f"Name: {user.first_name}\n"
        f"User ID: `{user.id}`\n"
        f"Username: @{user.username or 'None'}\n\n"
        f"Access: *{access}*",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="back"
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
                callback_data="admin_add"
            )
        ],
        [
            InlineKeyboardButton(
                "📚 Courses",
                callback_data="admin_courses"
            ),
            InlineKeyboardButton(
                "👥 Users",
                callback_data="admin_users"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Report",
                callback_data="admin_report"
            )
        ]
    ])


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin access नहीं है."
        )
        return

    await update.message.reply_text(
        "👑 *OWNER PANEL*\n\n"
        "नीचे से option चुनें 👇",
        reply_markup=admin_menu(),
        parse_mode="Markdown",
    )


# =========================================================
# ADD COURSE
# =========================================================

ADD_NAME, ADD_DEMO, ADD_PERMANENT = range(3)


async def admin_add_start(update, context):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "Admin only.",
            show_alert=True
        )
        return ConversationHandler.END

    await query.answer()

    context.user_data["new_course"] = {}

    await query.edit_message_text(
        "➕ *ADD COURSE*\n\n"
        "Course का नाम भेजो:",
        parse_mode="Markdown"
    )

    return ADD_NAME


async def add_name(update, context):
    context.user_data["new_course"]["name"] = (
        update.message.text.strip()
    )

    await update.message.reply_text(
        "🎬 *Demo Channel*\n\n"
        "Demo channel से कोई भी message इस bot को "
        "*Forward* करो.\n\n"
        "⚠️ Message channel का होना चाहिए."
        ,
        parse_mode="Markdown"
    )

    return ADD_DEMO


async def get_forwarded_channel(message):
    """
    Get channel information from forwarded channel message.
    """

    origin = getattr(message, "forward_origin", None)

    if not origin:
        return None

    # telegram MessageOriginChannel
    if hasattr(origin, "chat") and hasattr(origin, "message_id"):
        return origin.chat, origin.message_id

    return None


async def add_demo(update, context):
    info = await get_forwarded_channel(update.message)

    if not info:
        await update.message.reply_text(
            "❌ Channel message detect नहीं हुआ.\n\n"
            "Demo Channel का कोई message इस bot को "
            "*Forward* करो."
        )
        return ADD_DEMO

    chat, message_id = info

    ok, error = await check_channel_admin(
        context.bot,
        update.effective_user.id,
        chat.id
    )

    if not ok:
        await update.message.reply_text(
            "❌ Demo Channel Error\n\n"
            f"{error}"
        )
        return ADD_DEMO

    context.user_data["new_course"]["demo_chat_id"] = chat.id
    context.user_data["new_course"]["demo_username"] = (
        chat.username
    )
    context.user_data["new_course"]["demo_message_id"] = message_id

    await update.message.reply_text(
        "✅ Demo Channel detected successfully!\n\n"
        f"📢 {chat.title}\n"
        f"🆔 `{chat.id}`\n\n"
        "अब *Permanent Channel* से कोई message "
        "Forward करो.\n\n"
        "अगर Permanent Channel नहीं है तो `skip` लिखो.",
        parse_mode="Markdown"
    )

    return ADD_PERMANENT


async def add_permanent(update, context):
    text = (update.message.text or "").strip()

    # No permanent channel
    if text.lower() == "skip":
        context.user_data["new_course"]["permanent_chat_id"] = None
        context.user_data["new_course"]["permanent_username"] = None
        context.user_data["new_course"]["permanent_message_id"] = None

        return await save_course(update, context)

    info = await get_forwarded_channel(update.message)

    if not info:
        await update.message.reply_text(
            "❌ Permanent channel detect नहीं हुआ.\n\n"
            "Permanent Channel का message Forward करो."
        )
        return ADD_PERMANENT

    chat, message_id = info

    ok, error = await check_channel_admin(
        context.bot,
        update.effective_user.id,
        chat.id
    )

    if not ok:
        await update.message.reply_text(
            "❌ Permanent Channel Error\n\n"
            f"{error}"
        )
        return ADD_PERMANENT

    context.user_data["new_course"]["permanent_chat_id"] = chat.id
    context.user_data["new_course"]["permanent_username"] = (
        chat.username
    )
    context.user_data["new_course"]["permanent_message_id"] = message_id

    return await save_course(update, context)


async def save_course(update, context):
    data = context.user_data["new_course"]

    con = db()

    con.execute("""
        INSERT INTO courses (
            name,

            demo_chat_id,
            demo_chat_username,
            demo_message_id,

            permanent_chat_id,
            permanent_chat_username,
            permanent_message_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data["name"],

        data.get("demo_chat_id"),
        data.get("demo_username"),
        data.get("demo_message_id"),

        data.get("permanent_chat_id"),
        data.get("permanent_username"),
        data.get("permanent_message_id"),
    ))

    con.commit()
    con.close()

    context.user_data.pop("new_course", None)

    await update.message.reply_text(
        "✅ *COURSE ADDED SUCCESSFULLY!*\n\n"
        f"📕 {data['name']}\n\n"
        "🎬 Demo Link ✅\n"
        "📚 Demo Material ✅\n"
        "🔐 Permanent Link ✅\n"
        "📚 Permanent Material ✅",
        reply_markup=admin_menu(),
        parse_mode="Markdown",
    )

    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.pop("new_course", None)

    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=admin_menu()
    )

    return ConversationHandler.END


# =========================================================
# SELLER MANAGEMENT
# =========================================================

async def seller_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Use:\n\n"
            "/seller USER_ID demo\n"
            "/seller USER_ID full\n\n"
            "demo = Demo only\n"
            "full = Demo + Permanent"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid User ID."
        )
        return

    access = context.args[1].lower()

    if access not in ("demo", "full"):
        await update.message.reply_text(
            "❌ Access सिर्फ `demo` या `full` हो सकता है."
        )
        return

    con = db()

    con.execute("""
        INSERT INTO users (user_id, name, username, status)
        VALUES (?, '', '', ?)
        ON CONFLICT(user_id)
        DO UPDATE SET status = excluded.status
    """, (
        user_id,
        access,
    ))

    con.commit()
    con.close()

    await update.message.reply_text(
        "✅ Seller access updated.\n\n"
        f"User ID: `{user_id}`\n"
        f"Access: `{access}`",
        parse_mode="Markdown"
    )


# =========================================================
# ADMIN USERS
# =========================================================

async def admin_users(query):
    con = db()

    rows = con.execute("""
        SELECT user_id, name, username, status
        FROM users
        ORDER BY user_id DESC
        LIMIT 50
    """).fetchall()

    con.close()

    if not rows:
        text = "👥 कोई users नहीं हैं."
    else:
        text = "👥 *USERS*\n\n"

        for user_id, name, username, status in rows:
            text += (
                f"👤 {name or 'Unknown'}\n"
                f"🆔 `{user_id}`\n"
                f"@{username or 'None'}\n"
                f"Access: `{status}`\n\n"
            )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Owner Panel",
                    callback_data="admin"
                )
            ]
        ]),
        parse_mode="Markdown"
    )


# =========================================================
# ADMIN REPORT
# =========================================================

async def admin_report(query):
    con = db()

    users = con.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    courses = con.execute(
        "SELECT COUNT(*) FROM courses"
    ).fetchone()[0]

    demo_sellers = con.execute(
        "SELECT COUNT(*) FROM users WHERE status = 'demo'"
    ).fetchone()[0]

    full_sellers = con.execute(
        "SELECT COUNT(*) FROM users WHERE status = 'full'"
    ).fetchone()[0]

    con.close()

    await query.edit_message_text(
        "📊 *BOT REPORT*\n\n"
        f"👥 Users: `{users}`\n"
        f"📚 Courses: `{courses}`\n"
        f"🎬 Demo Sellers: `{demo_sellers}`\n"
        f"🔐 Full Sellers: `{full_sellers}`",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Owner Panel",
                    callback_data="admin"
                )
            ]
        ]),
        parse_mode="Markdown"
    )


# =========================================================
# CALLBACKS
# =========================================================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    data = query.data

    if data == "back":
        await query.answer()

        await query.edit_message_text(
            "🎓 *RAJ COURSE BOT*\n\n"
            "Welcome Seller Family ❤️\n\n"
            "नीचे से option चुनें 👇",
            reply_markup=main_menu(),
            parse_mode="Markdown"
        )

    elif data == "courses":
        await query.answer()
        await show_courses(query)

    elif data.startswith("course_"):
        await query.answer()

        course_id = int(
            data.split("_", 1)[1]
        )

        await show_course(
            query,
            course_id
        )

    elif data.startswith("demo_"):
        await query.answer()

        course_id = int(
            data.split("_", 1)[1]
        )

        await send_demo_link(
            query,
            course_id
        )

    elif data.startswith("permanent_"):
        await query.answer()

        course_id = int(
            data.split("_", 1)[1]
        )

        await send_permanent_link(
            query,
            course_id
        )

    elif data == "account":
        await query.answer()
        await show_account(query)

    elif data == "gift":
        await query.answer()

        await query.edit_message_text(
            "🎁 Gift system जल्द add किया जाएगा.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="back"
                    )
                ]
            ])
        )

    elif data == "admin":
        await query.answer()

        if not is_admin(query.from_user.id):
            return

        await query.edit_message_text(
            "👑 *OWNER PANEL*\n\n"
            "नीचे से option चुनें 👇",
            reply_markup=admin_menu(),
            parse_mode="Markdown"
        )

    elif data == "admin_report":
        await query.answer()

        if is_admin(query.from_user.id):
            await admin_report(query)

    elif data == "admin_users":
        await query.answer()

        if is_admin(query.from_user.id):
            await admin_users(query)

    elif data == "admin_courses":
        await query.answer()

        if is_admin(query.from_user.id):
            await show_courses(query)


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    logger.exception(
        "Exception while handling update:",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():
    if not TOKEN:
        raise ValueError(
            "BOT_TOKEN environment variable missing."
        )

    init_db()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    # Basic
    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("admin", admin_command)
    )

    app.add_handler(
        CommandHandler("seller", seller_command)
    )

    # Add course conversation
    add_course_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_add_start,
                pattern=r"^admin_add$"
            )
        ],

        states={
            ADD_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_name
                )
            ],

            ADD_DEMO: [
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    add_demo
                )
            ],

            ADD_PERMANENT: [
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    add_permanent
                )
            ],
        },

        fallbacks=[
            CommandHandler("cancel", cancel)
        ],

        per_user=True,
        per_chat=True,
    )

    app.add_handler(add_course_handler)

    # Buttons
    app.add_handler(
        CallbackQueryHandler(buttons)
    )

    app.add_error_handler(error_handler)

    print("RAJ COURSE BOT is running...")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
