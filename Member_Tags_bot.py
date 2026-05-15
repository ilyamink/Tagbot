import asyncio
import os
import sqlite3
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ==========================================================
# Telegram Member Tags Bot
# ==========================================================
# Что нужно добавить на хостинге в Environment Variables:
# BOT_TOKEN=твой_токен_от_BotFather
# ADMIN_IDS=твой_telegram_id
#
# Если админов несколько:
# ADMIN_IDS=123456789,987654321
#
# Команда запуска:
# python3 main.py
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
DB_PATH = os.getenv("DB_PATH", "member_tags_bot.db")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Добавь переменную окружения BOT_TOKEN на хостинге."
    )

ADMIN_IDS = {
    int(item.strip())
    for item in ADMIN_IDS_RAW.split(",")
    if item.strip().isdigit()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Временное состояние для сценария:
# переслали сообщение в личку -> выбрали группу -> написали тег.
pending: dict[int, dict] = {}


# ==========================================================
# Database
# ==========================================================

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
        return conn.execute(
            "SELECT chat_id, title FROM chats ORDER BY title"
        ).fetchall()


def get_chat_title(chat_id: int) -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT title FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    return row[0] if row else str(chat_id)


def log_action(
    *,
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
                created_at, actor_id, actor_name, chat_id, chat_title,
                target_user_id, target_name, action, tag, result, details
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
            SELECT created_at, actor_name, chat_title, target_name,
                   action, tag, result, details
            FROM action_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


# ==========================================================
# Helpers
# ==========================================================

def full_name(user) -> str:
    if not user:
        return "неизвестно"

    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()

    if user.username:
        return f"{name} (@{user.username})" if name else f"@{user.username}"

    return name or str(user.id)


def clean_tag(tag: str) -> str:
    tag = tag.strip()

    if tag.startswith("#"):
        tag = tag[1:]

    # Telegram Member Tag: максимум 16 символов.
    return tag[:16]


def get_forwarded_user_id(message: Message) -> Optional[int]:
    """
    Пытаемся получить user_id автора пересланного сообщения.
    Если у пользователя закрыта приватность пересылки, Telegram не отдаст ID.
    Тогда нужно использовать /settag ответом на сообщение в группе.
    """
    origin = getattr(message, "forward_origin", None)
    if origin:
        sender_user = getattr(origin, "sender_user", None)
        if sender_user:
            return sender_user.id

    forward_from = getattr(message, "forward_from", None)
    if forward_from:
        return forward_from.id

    return None


async def notify_admin_or_group(message: Message, text: str):
    """
    Сначала пытаемся отправить ответ админу в личку.
    Если админ не нажимал /start у бота, пишем в группу.
    """
    try:
        await bot.send_message(message.from_user.id, text)
    except Exception:
        await message.answer(text)


async def delete_command_safely(message: Message):
    """
    Удаляет команду /settag из группы.
    Нужно право Delete Messages / Удаление сообщений у бота.
    """
    try:
        await message.delete()
    except Exception:
        pass


def groups_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for chat_id, title in get_chats():
        rows.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=f"select_chat:{chat_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==========================================================
# Telegram API raw calls
# ==========================================================

async def telegram_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            return await response.json()


async def get_chat_member(chat_id: int, user_id: int) -> dict:
    return await telegram_api(
        "getChatMember",
        {
            "chat_id": chat_id,
            "user_id": user_id,
        },
    )


async def set_chat_member_tag(chat_id: int, user_id: int, tag: str) -> dict:
    return await telegram_api(
        "setChatMemberTag",
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "tag": tag,
        },
    )


# ==========================================================
# Permissions
# ==========================================================

async def user_can_manage_tags(chat_id: int, user_id: int) -> tuple[bool, str]:
    """
    Если ADMIN_IDS задан, разрешаем только этим ID.
    Если ADMIN_IDS пустой, разрешаем всем админам группы.
    """
    if ADMIN_IDS:
        if user_id in ADMIN_IDS:
            return True, "Пользователь есть в ADMIN_IDS."
        return False, "Тебя нет в списке ADMIN_IDS, поэтому менять теги нельзя."

    member = await get_chat_member(chat_id, user_id)
    if not member.get("ok"):
        return False, f"Не могу проверить твои права: {member.get('description')}"

    status = member["result"].get("status")
    if status in {"creator", "administrator"}:
        return True, f"Ты админ группы, статус: