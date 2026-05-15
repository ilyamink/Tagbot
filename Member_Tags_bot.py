import asyncio
import os
import sqlite3
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ============================================================
# Member Tags Bot for Telegram
# ============================================================
# Environment variables on hosting:
#
# BOT_TOKEN=your_bot_token_from_BotFather
# ADMIN_IDS=your_telegram_id
#
# Several admins example:
# ADMIN_IDS=123456789,987654321
#
# Start command:
# python3 main.py
#
# requirements.txt:
# aiogram
# aiohttp
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
DB_PATH = os.getenv("DB_PATH", "member_tags_bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add BOT_TOKEN to environment variables.")

ADMIN_IDS = set()
for item in ADMIN_IDS_RAW.split(","):
    item = item.strip()
    if item.isdigit():
        ADMIN_IDS.add(int(item))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Temporary state for private flow:
# forwarded message -> choose group -> send tag
pending = {}


# ============================================================
# Database
# ============================================================

def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor_id INTEGER,
                actor_name TEXT,
                chat_id INTEGER,
                chat_title TEXT,
                target_user_id INTEGER,
                target_name TEXT,
                action TEXT NOT NULL,
                tag TEXT,
                result TEXT NOT NULL,
                details TEXT
            )
            """
        )

        conn.commit()


def save_chat(chat_id: int, title: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chats (chat_id, title) VALUES (?, ?)",
            (chat_id, title),
        )
        conn.commit()


def get_chats():
    with db_connect() as conn:
        return conn.execute("SELECT chat_id, title FROM chats ORDER BY title").fetchall()


def get_chat_title(chat_id: int) -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT title FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
    if row:
        return row[0]
    return str(chat_id)


def write_log(
    actor_id: Optional[int],
    actor_name: str,
    chat_id: Optional[int],
    chat_title: str,
    target_user_id: Optional[int],
    target_name: str,
    action: str,
    tag: Optional[str],
    result: str,
    details: str = "",
):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO action_logs (
                created_at,
                actor_id,
                actor_name,
                chat_id,
                chat_title,
                target_user_id,
                target_name,
                action,
                tag,
                result,
                details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                actor_id,
                actor_name,
                chat_id,
                chat_title,
                target_user_id,
                target_name,
                action,
                tag,
                result,
                details,
            ),
        )
        conn.commit()


def get_last_logs(limit: int = 20):
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT created_at, actor_name, chat_title, target_name, action, tag, result, details
            FROM action_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


# ============================================================
# Helpers
# ============================================================

def user_name(user) -> str:
    if not user:
        return "unknown"

    first = user.first_name or ""
    last = user.last_name or ""
    name = (first + " " + last).strip()

    if user.username:
        username = "@" + user.username
        if name:
            return name + " (" + username + ")"
        return username

    if name:
        return name

    return str(user.id)


def clean_tag(text: str) -> str:
    tag = (text or "").strip()

    if tag.startswith("#"):
        tag = tag[1:]

    # Telegram member tags are limited. Keep it simple and safe.
    tag = tag.strip()
    tag = tag[:16]
    return tag


def get_forwarded_user_id(message: Message) -> Optional[int]:
    # New Bot API style
    origin = getattr(message, "forward_origin", None)
    if origin:
        sender_user = getattr(origin, "sender_user", None)
        if sender_user:
            return sender_user.id

    # Old Bot API style
    forward_from = getattr(message, "forward_from", None)
    if forward_from:
        return forward_from.id

    return None


def groups_keyboard() -> InlineKeyboardMarkup:
    buttons = []

    for chat_id, title in get_chats():
        button = InlineKeyboardButton(
            text=title,
            callback_data="select_chat:" + str(chat_id),
        )
        buttons.append([button])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_private_or_group(message: Message, text: str):
    # Try private message first.
    # If user did not press /start in bot private chat, Telegram may reject it.
    try:
        await bot.send_message(message.from_user.id, text)
    except Exception:
        await message.answer(text)


async def delete_message_safely(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


# ============================================================
# Raw Telegram Bot API methods
# ============================================================

async def telegram_api(method: str, payload: dict) -> dict:
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/" + method

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            return await response.json()


async def api_get_chat_member(chat_id: int, user_id: int) -> dict:
    return await telegram_api(
        "getChatMember",
        {
            "chat_id": chat_id,
            "user_id": user_id,
        },
    )


async def api_set_chat_member_tag(chat_id: int, user_id: int, tag: str) -> dict:
    return await telegram_api(
        "setChatMemberTag",
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "tag": tag,
        },
    )


# ============================================================
# Permission checks
# ============================================================

async def user_is_allowed(chat_id: int, user_id: int) -> tuple[bool, str]:
    # If ADMIN_IDS is set, only these Telegram IDs are allowed.
    if ADMIN_IDS:
        if user_id in ADMIN_IDS:
            return True, "User is allowed by ADMIN_IDS."
        return False, "Your Telegram ID is not in ADMIN_IDS."

    # If ADMIN_IDS is empty, any group admin is allowed.
    member = await api_get_chat_member(chat_id, user_id)

    if not member.get("ok"):
        reason = member.get("description", "unknown error")
        return False, "Cannot check your group rights: " + reason

    status = member.get("result", {}).get("status")

    if status == "creator" or status == "administrator":
        return True, "You are group admin. Status: " + str(status)

    return False, "You are not group admin."


async def bot_is_allowed(chat_id: int) -> tuple[bool, str]:
    me = await bot.get_me()
    member = await api_get_chat_member(chat_id, me.id)

    if not member.get("ok"):
        reason = member.get("description", "unknown error")
        return False, "Cannot check bot rights: " + reason

    result = member.get("result", {})
    status = result.get("status")
    can_manage_tags = result.get("can_manage_tags")

    if status != "administrator" and status != "creator":
        return False, "Bot is not admin. Status: " + str(status)

    if can_manage_tags is not True:
        return False, "Bot has no can_manage_tags permission. Give bot Manage Tags rights."

    return True, "Bot can manage tags."


async def build_diagnostics(chat_id: int, target_user_id: int) -> str:
    me = await bot.get_me()
    bot_member = await api_get_chat_member(chat_id, me.id)
    target_member = await api_get_chat_member(chat_id, target_user_id)

    lines = []
    lines.append("Diagnostics:")

    if bot_member.get("ok"):
        data = bot_member.get("result", {})
        lines.append("Bot status: " + str(data.get("status")))
        lines.append("Bot can_manage_tags: " + str(data.get("can_manage_tags")))
        lines.append("Bot can_delete_messages: " + str(data.get("can_delete_messages")))
    else:
        lines.append("Bot check error: " + str(bot_member.get("description")))

    if target_member.get("ok"):
        data = target_member.get("result", {})
        lines.append("Target status: " + str(data.get("status")))
        if data.get("status") == "administrator" or data.get("status") == "creator":
            lines.append("Important: Telegram tags can be set only for regular members, not admins.")
    else:
        lines.append("Target check error: " + str(target_member.get("description")))

    return "\n".join(lines)


# ============================================================
# Commands
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "Bot is running.\n\n"
        "How to use:\n"
        "1. Add bot to group as admin.\n"
        "2. In group, send /register.\n"
        "3. To set tag in group: reply to user message with /settag WB.\n"
        "4. To set tag via private chat: forward user message to bot, choose group, send tag.\n\n"
        "Commands:\n"
        "/whoami - show your Telegram ID\n"
        "/checkme - check your rights in group\n"
        "/logs - show action log"
    )
    await message.answer(text)


@dp.message(Command("whoami"))
async def cmd_whoami(message: Message):
    await message.answer("Your Telegram ID: " + str(message.from_user.id))


@dp.message(Command("register"))
async def cmd_register(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Use /register inside a group.")
        return

    title = message.chat.title or "Untitled group"
    save_chat(message.chat.id, title)
    await message.answer("Group registered: " + title)


@dp.message(Command("checkme"))
async def cmd_checkme(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        lines = []
        lines.append("This is private chat.")
        lines.append("Your ID: " + str(message.from_user.id))
        if ADMIN_IDS:
            lines.append("ADMIN_IDS: " + str(sorted(ADMIN_IDS)))
        else:
            lines.append("ADMIN_IDS: not set")
        await message.answer("\n".join(lines))
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    member = await api_get_chat_member(chat_id, user_id)
    me = await bot.get_me()
    bot_member = await api_get_chat_member(chat_id, me.id)

    lines = []
    lines.append("Rights check:")
    lines.append("Your ID: " + str(user_id))
    lines.append("Chat ID: " + str(chat_id))
    lines.append("Group: " + str(message.chat.title))

    if message.from_user.username:
        lines.append("Username: @" + message.from_user.username)
    else:
        lines.append("Username: none")

    if ADMIN_IDS:
        lines.append("ADMIN_IDS: " + str(sorted(ADMIN_IDS)))
    else:
        lines.append("ADMIN_IDS: not set")

    if member.get("ok"):
        data = member.get("result", {})
        lines.append("Your status: " + str(data.get("status")))
        lines.append("Your can_manage_chat: " + str(data.get("can_manage_chat")))
        lines.append("Your can_delete_messages: " + str(data.get("can_delete_messages")))
        lines.append("Your can_manage_tags: " + str(data.get("can_manage_tags")))
    else:
        lines.append("Your check error: " + str(member.get("description")))

    if bot_member.get("ok"):
        data = bot_member.get("result", {})
        lines.append("Bot status: " + str(data.get("status")))
        lines.append("Bot can_delete_messages: " + str(data.get("can_delete_messages")))
        lines.append("Bot can_manage_tags: " + str(data.get("can_manage_tags")))
    else:
        lines.append("Bot check error: " + str(bot_member.get("description")))

    await send_private_or_group(message, "\n".join(lines))


@dp.message(Command("logs"))
async def cmd_logs(message: Message):
    if message.chat.type == "private":
        if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
            await message.answer("Access denied. Your ID is not in ADMIN_IDS.")
            return
        if not ADMIN_IDS:
            await message.answer("Logs in private chat require ADMIN_IDS to be set.")
            return
    else:
        allowed, reason = await user_is_allowed(message.chat.id, message.from_user.id)
        if not allowed:
            await send_private_or_group(message, "Access denied. " + reason)
            return

    rows = get_last_logs(20)

    if not rows:
        await send_private_or_group(message, "Log is empty.")
        return

    lines = []
    lines.append("Last actions:")

    for row in rows:
        created_at, actor_name, chat_title, target_name, action, tag, result, details = row
        line = created_at + " | " + actor_name + " | " + action
        if tag:
            line += " | tag: " + tag
        line += " | target: " + target_name
        line += " | group: " + chat_title
        line += " | result: " + result
        if details:
            line += " | " + details
        lines.append(line)

    await send_private_or_group(message, "\n".join(lines))


@dp.message(Command("settag"))
async def cmd_settag(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Use /settag in group as reply to user message.")
        return

    await delete_message_safely(message)

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await send_private_or_group(
            message,
            "Reply to user message with command. Example: /settag WB",
        )
        return

    tag_text = message.text.replace("/settag", "", 1)
    tag = clean_tag(tag_text)

    if not tag:
        await send_private_or_group(message, "Tag is empty. Example: /settag WB")
        return

    actor = message.from_user
    target = message.reply_to_message.from_user
    chat_id = message.chat.id
    chat_title = message.chat.title or "Untitled group"

    allowed, reason = await user_is_allowed(chat_id, actor.id)
    if not allowed:
        write_log(
            actor.id,
            user_name(actor),
            chat_id,
            chat_title,
            target.id,
            user_name(target),
            "settag_reply",
            tag,
            "denied",
            reason,
        )
        await send_private_or_group(message, "Access denied. " + reason)
        return

    bot_allowed, bot_reason = await bot_is_allowed(chat_id)
    if not bot_allowed:
        write_log(
            actor.id,
            user_name(actor),
            chat_id,
            chat_title,
            target.id,
            user_name(target),
            "settag_reply",
            tag,
            "bot_denied",
            bot_reason,
        )
        await send_private_or_group(message, "Bot cannot set tags. " + bot_reason)
        return

    result = await api_set_chat_member_tag(chat_id, target.id, tag)

    if result.get("ok"):
        write_log(
            actor.id,
            user_name(actor),
            chat_id,
            chat_title,
            target.id,
            user_name(target),
            "settag_reply",
            tag,
            "success",
            "",
        )
        await send_private_or_group(message, "Done. Tag set: " + tag)
    else:
        error = result.get("description", "unknown error")
        write_log(
            actor.id,
            user_name(actor),
            chat_id,
            chat_title,
            target.id,
            user_name(target),
            "settag_reply",
            tag,
            "error",
            error,
        )
        await send_private_or_group(message, "Telegram error: " + error)


# ============================================================
# Private flow
# ============================================================

@dp.message(F.chat.type == "private")
async def private_flow(message: Message):
    admin_id = message.from_user.id

    forwarded_user_id = get_forwarded_user_id(message)

    if forwarded_user_id:
        chats = get_chats()

        if not chats:
            await message.answer(
                "User ID detected, but no groups are registered.\n"
                "Add bot to group as admin and send /register in group."
            )
            return

        pending[admin_id] = {
            "target_user_id": forwarded_user_id,
            "step": "select_chat",
        }

        await message.answer(
            "User detected. ID: " + str(forwarded_user_id) + "\nChoose group:",
            reply_markup=groups_keyboard(),
        )
        return

    state = pending.get(admin_id)

    if state and state.get("step") == "write_tag":
        tag = clean_tag(message.text or "")

        if not tag:
            await message.answer("Send tag text. Example: WB")
            return

        chat_id = state["chat_id"]
        target_user_id = state["target_user_id"]
        chat_title = get_chat_title(chat_id)

        allowed, reason = await user_is_allowed(chat_id, admin_id)
        if not allowed:
            await message.answer("Access denied. " + reason)
            pending.pop(admin_id, None)
            return

        bot_allowed, bot_reason = await bot_is_allowed(chat_id)
        if not bot_allowed:
            await message.answer("Bot cannot set tags. " + bot_reason)
            return

        diagnostics = await build_diagnostics(chat_id, target_user_id)
        await message.answer(diagnostics)

        result = await api_set_chat_member_tag(chat_id, target_user_id, tag)

        if result.get("ok"):
            write_log(
                message.from_user.id,
                user_name(message.from_user),
                chat_id,
                chat_title,
                target_user_id,
                str(target_user_id),
                "settag_forward",
                tag,
                "success",
                "",
            )
            await message.answer("Done. Tag set: " + tag)
            pending.pop(admin_id, None)
        else:
            error = result.get("description", "unknown error")
            write_log(
                message.from_user.id,
                user_name(message.from_user),
                chat_id,
                chat_title,
                target_user_id,
                str(target_user_id),
                "settag_forward",
                tag,
                "error",
                error,
            )
            await message.answer("Telegram error: " + error)

        return

    await message.answer(
        "Forward me a user message to set tag.\n"
        "If Telegram hides forwarded user ID, use group method: reply /settag WB."
    )


@dp.callback_query(F.data.startswith("select_chat:"))
async def select_chat(callback: CallbackQuery):
    admin_id = callback.from_user.id
    state = pending.get(admin_id)

    if not state:
        await callback.answer("First forward user message to bot.", show_alert=True)
        return

    chat_id_text = callback.data.split(":", 1)[1]
    chat_id = int(chat_id_text)

    allowed, reason = await user_is_allowed(chat_id, admin_id)
    if not allowed:
        pending.pop(admin_id, None)
        await callback.message.answer("Access denied. " + reason)
        await callback.answer()
        return

    state["chat_id"] = chat_id
    state["step"] = "write_tag"
    pending[admin_id] = state

    await callback.message.answer(
        "Group selected. Send tag text.\n"
        "Examples: WB, Ozon, Check, Night"
    )
    await callback.answer()


# ============================================================
# Main
# ============================================================

async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
