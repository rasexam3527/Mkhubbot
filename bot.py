import os
import sqlite3
import logging

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


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            role TEXT DEFAULT 'user'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sellers (
            user_id INTEGER PRIMARY KEY,
            permission TEXT DEFAULT 'demo'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icon TEXT DEFAULT '📕',
            photo_file_id TEXT,
            demo_link TEXT,
            permanent_link TEXT,
            channel_link TEXT
        )
    """)

    con.commit()
    con.close()


def save_user(user):
    if not user:
        return

    con = db()

    existing = con.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user.id,),
    ).fetchone()

    if existing:
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    save_user(user)

    await update.message.reply_text(
        "🎓 *RAJ COURSE BOT*\n\n"
        f"Hello {user.first_name or 'Friend'} ❤️\n\n"
        "Welcome! नीचे से अपना option चुनें 👇",
        reply_markup=main_menu(user.id),
        parse_mode="Markdown",
    )


# =========================================================
# COURSE LIST
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
        "📚 *OUR COURSES*\n\n"
        "अपना course select करें 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# =========================================================
# COURSE DETAIL
# =========================================================

async def show_course(query, context, course_id):
    con = db()

    row = con.execute(
        """
        SELECT
            id,
            name,
            icon,
            photo_file_id,
            demo_link,
            permanent_link,
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
        photo_file_id,
        demo_link,
        permanent_link,
        channel_link,
    ) = row

    user_id = query.from_user.id

    buttons = []

    # Demo
    if demo_link:
        buttons.append([
            InlineKeyboardButton(
                "🎁 Demo Link",
                url=demo_link,
            )
        ])

    # Permanent
    permission = seller_permission(user_id)

    if is_admin(user_id):
        if permanent_link:
            buttons.append([
                InlineKeyboardButton(
                    "🔒 Permanent Link",
                    url=permanent_link,
                )
            ])

    elif permission == "demo_permanent":
        if permanent_link:
            buttons.append([
                InlineKeyboardButton(
                    "🔒 Permanent Link",
                    url=permanent_link,
                )
            ])

    # Channel
    if channel_link:
        buttons.append([
            InlineKeyboardButton(
                "📢 Course Channel",
                url=channel_link,
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

    # Course photo
    if photo_file_id:
        try:
            await query.message.delete()

            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_file_id,
                caption=text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )

            return

        except Exception as e:
            logger.warning(
                "Photo send failed: %s",
                e,
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
        SELECT id, name, icon
        FROM courses
        WHERE channel_link IS NOT NULL
        AND channel_link != ''
        ORDER BY id DESC
        """
    ).fetchall()

    con.close()

    if not rows:
        await query.edit_message_text(
            "📢 अभी कोई channel available नहीं है।",
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
                f"{icon or '📢'} {name}",
                callback_data=f"channel_{course_id}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="back",
        )
    ])

    await query.edit_message_text(
        "📢 *COURSE CHANNELS*\n\n"
        "अपना channel select करें 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def open_channel(query, course_id):
    con = db()

    row = con.execute(
        """
        SELECT name, channel_link
        FROM courses
        WHERE id = ?
        """,
        (course_id,),
    ).fetchone()

    con.close()

    if not row or not row[1]:
        await query.answer(
            "Channel available नहीं है ❌",
            show_alert=True,
        )
        return

    name, link = row

    await query.edit_message_text(
        f"📢 *{name}*\n\n"
        "नीचे channel open करें 👇",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📢 Open Channel",
                    url=link,
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Channels",
                    callback_data="channels",
                )
            ],
        ]),
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
        access = "👤 Normal User - Demo Only"

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
                "📚 Manage Courses",
                callback_data="admin_courses",
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
            ),
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
# ADD COURSE START
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
        "Seller का Telegram numeric User ID भेजें:\n\n"
        "Example:\n"
        "`123456789`",
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
# ADMIN TEXT INPUT
# =========================================================

async def handle_admin_input(update, context):
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
                "❌ सही numeric User ID भेजो।"
            )
            return

        context.user_data["seller_id"] = int(value)
        context.user_data["state"] = "seller_permission"

        await update.message.reply_text(
            "🔐 Seller permission भेजें:\n\n"
            "`demo`\n"
            "➡️ सिर्फ Demo\n\n"
            "या\n\n"
            "`demo_permanent`\n"
            "➡️ Demo + Permanent",
            parse_mode="Markdown",
        )

        return

    # -----------------------------------------------------
    # SELLER PERMISSION
    # -----------------------------------------------------

    if state == "seller_permission":

        if value not in [
            "demo",
            "demo_permanent",
        ]:
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
            f"👤 User ID: `{seller_id}`\n"
            f"🔐 Access: {access}",
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
            "✅ Seller access remove कर दिया गया।",
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
    # COURSE ICON
    # -----------------------------------------------------

    if state == "course_icon":

        context.user_data["course"]["icon"] = value[:10]
        context.user_data["state"] = "course_photo"

        await update.message.reply_text(
            "3️⃣ अब course की cover photo भेजें।\n\n"
            "Photo नहीं चाहिए तो `skip` भेजें।"
        )

        return

    # -----------------------------------------------------
    # COURSE PHOTO SKIP
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

        context.user_data["state"] = "course_permanent"

        await update.message.reply_text(
            "5️⃣ Permanent Link भेजें।\n\n"
            "नहीं है तो `skip` भेजें।"
        )

        return

    # -----------------------------------------------------
    # PERMANENT LINK
    # -----------------------------------------------------

    if state == "course_permanent":

        context.user_data["course"]["permanent_link"] = (
            None
            if value.lower() == "skip"
            else value
        )

        context.user_data["state"] = "course_channel"

        await update.message.reply_text(
            "6️⃣ Telegram Channel Link भेजें।\n\n"
            "Example:\n"
            "https://t.me/yourchannel\n\n"
            "नहीं है तो `skip` भेजें।"
        )

        return

    # -----------------------------------------------------
    # CHANNEL LINK
    # -----------------------------------------------------

    if state == "course_channel":

        context.user_data["course"]["channel_link"] = (
            None
            if value.lower() == "skip"
            else value
        )

        await save_course(update, context)

        return


# =========================================================
# PHOTO HANDLER
# =========================================================

async def handle_course_photo(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    state = context.user_data.get("state")

    if state != "course_photo":
        return

    if not update.message.photo:
        return

    photo = update.message.photo[-1]

    context.user_data["course"]["photo_file_id"] = photo.file_id
    context.user_data["state"] = "course_demo"

    await update.message.reply_text(
        "✅ Cover photo saved!\n\n"
        "4️⃣ Demo Link भेजें।\n\n"
        "नहीं है तो `skip` भेजें।"
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
            permanent_link,
            channel_link
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            course.get("name"),
            course.get("icon") or "📕",
            course.get("photo_file_id"),
            course.get("demo_link"),
            course.get("permanent_link"),
            course.get("channel_link"),
        ),
    )

    con.commit()
    con.close()

    name = course.get("name")
    icon = course.get("icon") or "📕"

    context.user_data.clear()

    await update.message.reply_text(
        "✅ *COURSE ADDED SUCCESSFULLY!*\n\n"
        f"{icon} {name}\n\n"
        "📚 अब यह Course List में दिखाई देगा।\n"
        "📢 Channel link दिया है तो Channels में भी दिखाई देगा।",
        reply_markup=admin_menu(),
        parse_mode="Markdown",
    )


# =========================================================
# MANAGE COURSES
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
            "🔙 Admin Panel",
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
# ADMIN COURSE DETAIL
# =========================================================

async def admin_course_detail(query, course_id):

    con = db()

    row = con.execute(
        """
        SELECT
            name,
            icon,
            demo_link,
            permanent_link,
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

    name, icon, demo, permanent, channel = row

    text = (
        f"{icon or '📕'} *{name}*\n\n"
        f"🎁 Demo: {'✅' if demo else '❌'}\n"
        f"🔒 Permanent: {'✅' if permanent else '❌'}\n"
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
# SELLER LIST
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

    if not rows:

        text = (
            "👨‍💼 *SELLERS*\n\n"
            "अभी कोई seller नहीं है।"
        )

    else:

        text = "👨‍💼 *SELLERS*\n\n"

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
        """
        SELECT COUNT(*)
        FROM courses
        WHERE channel_link IS NOT NULL
        AND channel_link != ''
        """
    ).fetchone()[0]

    con.close()

    await query.edit_message_text(
        "📊 *BOT REPORT*\n\n"
        f"👥 Users: `{users}`\n"
        f"📚 Courses: `{courses}`\n"
        f"👨‍💼 Sellers: `{sellers}`\n"
        f"📢 Channels: `{channels}`",
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
# CALLBACK HANDLER
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
            context,
            course_id,
        )

        return

    # CHANNELS
    if data == "channels":

        await show_channels(query)

        return

    # CHANNEL
    if data.startswith("channel_"):

        course_id = int(
            data.split("_")[1]
        )

        await open_channel(
            query,
            course_id,
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

    # MANAGE COURSES
    if data == "admin_courses":

        if is_admin(user_id):
            await admin_courses(query)

        return

    # ADMIN COURSE
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
        "Exception while handling update:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:
        raise ValueError(
            "BOT_TOKEN environment variable is missing."
        )

    if not ADMIN_IDS:
        raise ValueError(
            "ADMIN_IDS environment variable is missing."
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

    # Course photo
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_course_photo,
        )
    )

    # Admin text input
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_admin_input,
        )
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(
            buttons
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
