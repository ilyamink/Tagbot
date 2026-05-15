import asyncio
import os
import sqlite3
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# Установка библиотек:
# pip3 install aiogram aiohttp

# Безопасный запуск через терминал:
# export BOT_TOKEN="твой_токен"
# export ADMIN_IDS="твой_telegram_id"
# python3 Hellp.py

# Для быстрого локального теста можно временно заменить строку ниже на:
# BOT_TOKEN = "твой_токен"
BOT_TOKEN = "8269383078:AAFbz_GAr_tVDCTcD3fxNcm5XJKocm0sRsY"

DB_PATH = "member_tags_bot.db"

# Узнать свой ID можно командой /whoami в личке боту.
# Если ADMIN_IDS пустой, бот будет разрешать /settag всем админам выбранной группы.
# Если ADMIN_IDS задан, бот будет разрешать /settag только указанным ID.
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Укажи токен: export BOT_TOKEN='твой_токен'")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pending = {}


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
    return row[0] if row else str(chat_id)


def full_name(user) -> str:
    if not user:
        return "неизвестно"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    if user.username:
        name = f"{name} (@{user.username})" if name else f"@{user.username}"
    return name or str(user.id)


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
    from datetime import datetime

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
            SELECT created_at, actor_name, chat_title, target_name, action, tag, result, details
            FROM action_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_forwarded_user_id(message: Message) -> Optional[int]:
    origin = getattr(message, "forward_origin", None)
    if origin:
        sender_user = getattr(origin, "sender_user", None)
        if sender_user:
            return sender_user.id

    forward_from = getattr(message, "forward_from", None)
    if forward_from:
        return forward_from.id

    return None


def clean_tag(tag: str) -> str:
    tag = tag.strip()
    if tag.startswith("#"):
        tag = tag[1:]
    return tag[:16]


async def telegram_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            return await response.json()


async def set_chat_member_tag(chat_id: int, user_id: int, tag: str) -> dict:
    return await telegram_api(
        "setChatMemberTag",
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "tag": tag,
        },
    )


async def get_chat_member(chat_id: int, user_id: int) -> dict:
    return await telegram_api(
        "getChatMember",
        {
            "chat_id": chat_id,
            "user_id": user_id,
        },
    )


async def user_can_manage_tags(chat_id: int, user_id: int) -> tuple[bool, str]:
    if ADMIN_IDS:
        if user_id in ADMIN_IDS:
            return True, "Пользователь есть в ADMIN_IDS."
        return False, "Тебя нет в списке ADMIN_IDS, поэтому менять теги нельзя."

    member = await get_chat_member(chat_id, user_id)
    if not member.get("ok"):
        return False, f"Не могу проверить твои права в группе: {member.get('description')}"

    status = member["result"].get("status")
    if status in {"creator", "administrator"}:
        return True, f"Ты админ выбранной группы, статус: {status}."

    return False, "Ты не админ выбранной группы, поэтому менять теги нельзя."


async def bot_can_manage_tags(chat_id: int) -> tuple[bool, str]:
    me = await bot.get_me()
    bot_member = await get_chat_member(chat_id, me.id)

    if not bot_member.get("ok"):
        return False, f"Не могу проверить права бота: {bot_member.get('description')}"

    bm = bot_member["result"]
    status = bm.get("status")
    can_manage_tags = bm.get("can_manage_tags")

    if status not in {"administrator", "creator"}:
        return False, f"Бот не админ в группе. Текущий статус: {status}"

    if can_manage_tags is not True:
        return False, (
            "У бота нет права can_manage_tags. "
            "Если в Telegram нет галочки «Управление тегами / Manage Tags», "
            "попробуй обновить Telegram или выдать боту максимум прав админа."
        )

    return True, "У бота есть право can_manage_tags."


async def diagnose_before_tag(chat_id: int, target_user_id: int) -> str:
    me = await bot.get_me()
    bot_member = await get_chat_member(chat_id, me.id)
    target_member = await get_chat_member(chat_id, target_user_id)

    lines = ["Диагностика перед установкой тега:"]

    if bot_member.get("ok"):
        bm = bot_member["result"]
        lines.append(f"Бот в группе: да, статус: {bm.get('status')}")
        lines.append(f"can_manage_tags: {bm.get('can_manage_tags')}")
        lines.append(f"can_delete_messages: {bm.get('can_delete_messages')}")
    else:
        lines.append(f"Бот в группе: ошибка — {bot_member.get('description')}")

    if target_member.get("ok"):
        tm = target_member["result"]
        lines.append(f"Пользователь в группе: да, статус: {tm.get('status')}")
        if tm.get("status") in {"administrator", "creator"}:
            lines.append("Важно: Telegram разрешает ставить Member Tag только обычным участникам, не админам.")
    else:
        lines.append(f"Пользователь в группе: ошибка — {target_member.get('description')}")

    return "\n".join(lines)


async def notify_admin_or_group(message: Message, text: str):
    try:
        await bot.send_message(message.from_user.id, text)
    except Exception:
        await message.answer(text)


async def delete_command_safely(message: Message):
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


@dp.message(Command("whoami"))
async def whoami(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@dp.message(Command("logs"))
async def show_logs(message: Message):
    if message.chat.type == "private":
        if not ADMIN_IDS or message.from_user.id not in ADMIN_IDS:
            await message.answer("Журнал доступен только владельцу/админам из ADMIN_IDS.")
            return
    else:
        allowed, reason = await user_can_manage_tags(message.chat.id, message.from_user.id)
        if not allowed:
            await notify_admin_or_group(message, "Отказано.\n" + reason)
            return

    rows = get_last_logs(20)
    if not rows:
        await notify_admin_or_group(message, "Журнал пока пустой.")
        return

    lines = ["Последние действия:"]
    for row in rows:
        created_at, actor_name, chat_title, target_name, action, tag, result, details = row
        tag_text = f" | тег: {tag}" if tag else ""
        details_text = f" | {details}" if details else ""
        lines.append(
            f"{created_at} | {actor_name} | {action}{tag_text} | "
            f"цель: {target_name} | группа: {chat_title} | {result}{details_text}"
        )

    await notify_admin_or_group(message, "\n".join(lines[:21]))


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Бот работает.\n\n"
        "Как пользоваться:\n"
        "1. Добавь меня в группу админом.\n"
        "2. В группе напиши /register.\n"
        "3. Перешли мне в личку сообщение от нужного пользователя.\n"
        "4. Выбери группу.\n"
        "5. Напиши тег, например: Проверен или WB.\n\n"
        "Если при пересылке Telegram не отдает ID пользователя:\n"
        "ответь на сообщение человека в группе командой /settag WB\n\n"
        "Журнал действий: /logs"
    )


@dp.message(Command("settag"))
async def settag_by_reply(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Команда /settag работает только в группе и только ответом на сообщение пользователя.")
        return

    await delete_command_safely(message)

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await notify_admin_or_group(
            message,
            "Ответь командой на сообщение человека, которому нужно поставить тег.\n\n"
            "Пример: ответ на сообщение пользователя → /settag WB",
        )
        return

    tag = clean_tag(message.text.replace("/settag", "", 1).strip())
    if not tag:
        await notify_admin_or_group(message, "Укажи тег. Пример: /settag WB")
        return

    actor = message.from_user
    target = message.reply_to_message.from_user
    chat_id = message.chat.id
    chat_title = message.chat.title or "Без названия"

    user_allowed, user_reason = await user_can_manage_tags(chat_id, actor.id)
    if not user_allowed:
        log_action(
            actor_id=actor.id,
            actor_name=full_name(actor),
            chat_id=chat_id,
            chat_title=chat_title,
            target_user_id=target.id,
            target_name=full_name(target),
            action="settag_by_reply",
            tag=tag,
            result="denied",
            details=user_reason,
        )
        await notify_admin_or_group(message, "Отказано.\n" + user_reason)
        return

    bot_allowed, bot_reason = await bot_can_manage_tags(chat_id)
    if not bot_allowed:
        log_action(
            actor_id=actor.id,
            actor_name=full_name(actor),
            chat_id=chat_id,
            chat_title=chat_title,
            target_user_id=target.id,
            target_name=full_name(target),
            action="settag_by_reply",
            tag=tag,
            result="bot_denied",
            details=bot_reason,
        )
        await notify_admin_or_group(message, "Бот не может ставить теги.\n" + bot_reason)
        return

    result = await set_chat_member_tag(chat_id, target.id, tag)

    if result.get("ok"):
        log_action(
            actor_id=actor.id,
            actor_name=full_name(actor),
            chat_id=chat_id,
            chat_title=chat_title,
            target_user_id=target.id,
            target_name=full_name(target),
            action="settag_by_reply",
            tag=tag,
            result="success",
            details="",
        )
        await notify_admin_or_group(message, f"Готово. Пользователю {full_name(target)} установлен тег: {tag}")
    else:
        error = result.get("description", "unknown error")
        log_action(
            actor_id=actor.id,
            actor_name=full_name(actor),
            chat_id=chat_id,
            chat_title=chat_title,
            target_user_id=target.id,
            target_name=full_name(target),
            action="settag_by_reply",
            tag=tag,
            result="error",
            details=error,
        )
        await notify_admin_or_group(message, "Telegram не дал поставить тег.\nОшибка: " + error)


@dp.message(Command("register"))
async def register_chat(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Команду /register нужно писать именно в группе, где я админ.")
        return

    save_chat(message.chat.id, message.chat.title or "Без названия")
    await message.answer(
        "Группа сохранена.\n"
        "Теперь можно переслать мне сообщение пользователя в личку и выбрать эту группу для тега."
    )


@dp.message(F.chat.type == "private")
async def private_message(message: Message):
    admin_id = message.from_user.id

    forwarded_user_id = get_forwarded_user_id(message)
    if forwarded_user_id:
        chats = get_chats()
        if not chats:
            await message.answer(
                "Я определил пользователя, но у меня пока нет сохраненных групп.\n\n"
                "Добавь меня админом в нужную группу и напиши там /register."
            )
            return

        pending[admin_id] = {
            "target_user_id": forwarded_user_id,
            "step": "select_chat",
        }

        await message.answer(
            f"Пользователь определен. ID: {forwarded_user_id}\n"
            "Теперь выбери группу, где нужно поставить тег:",
            reply_markup=groups_keyboard(),
        )
        return

    state = pending.get(admin_id)
    if state and state.get("step") == "write_tag":
        tag = clean_tag(message.text or "")

        if not tag:
            await message.answer("Напиши тег текстом, например: Проверен")
            return

        if len(tag) > 16:
            await message.answer("Тег слишком длинный. Максимум 16 символов.")
            return

        chat_id = state["chat_id"]
        target_user_id = state["target_user_id"]
        chat_title = get_chat_title(chat_id)

        user_allowed, user_reason = await user_can_manage_tags(chat_id, admin_id)
        if not user_allowed:
            await message.answer("Отказано.\n" + user_reason)
            pending.pop(admin_id, None)
            return

        bot_allowed, bot_reason = await bot_can_manage_tags(chat_id)
        if not bot_allowed:
            await message.answer("Бот не может ставить теги.\n" + bot_reason)
            return

        diagnosis = await diagnose_before_tag(chat_id, target_user_id)
        await message.answer(diagnosis)

        result = await set_chat_member_tag(chat_id, target_user_id, tag)

        if result.get("ok"):
            log_action(
                actor_id=message.from_user.id,
                actor_name=full_name(message.from_user),
                chat_id=chat_id,
                chat_title=chat_title,
                target_user_id=target_user_id,
                target_name=str(target_user_id),
                action="settag_by_forward",
                tag=tag,
                result="success",
                details="",
            )
            await message.answer(f"Готово. Тег установлен: {tag}")
            pending.pop(admin_id, None)
        else:
            error = result.get("description", "unknown error")
            log_action(
                actor_id=message.from_user.id,
                actor_name=full_name(message.from_user),
                chat_id=chat_id,
                chat_title=chat_title,
                target_user_id=target_user_id,
                target_name=str(target_user_id),
                action="settag_by_forward",
                tag=tag,
                result="error",
                details=error,
            )
            await message.answer(
                "Telegram не дал поставить тег.\n\n"
                f"Ошибка: {error}\n\n"
                "Проверь:\n"
                "1. Бот админ в этой группе.\n"
                "2. У бота есть право Manage Tags / Управление тегами.\n"
                "3. Пользователь реально состоит в этой группе.\n"
                "4. Пользователь является обычным участником, а не админом."
            )
        return

    await message.answer(
        "Перешли мне сообщение от пользователя, которому нужно поставить тег.\n\n"
        "Если Telegram не даст определить автора пересланного сообщения, используй способ в группе: ответь на сообщение командой /settag WB."
    )


@dp.callback_query(F.data.startswith("select_chat:"))
async def select_chat(callback: CallbackQuery):
    admin_id = callback.from_user.id
    state = pending.get(admin_id)

    if not state:
        await callback.answer("Сначала перешли мне сообщение пользователя.", show_alert=True)
        return

    chat_id = int(callback.data.split(":", 1)[1])

    user_allowed, user_reason = await user_can_manage_tags(chat_id, admin_id)
    if not user_allowed:
        await callback.message.answer("Отказано.\n" + user_reason)
        pending.pop(admin_id, None)
        await callback.answer()
        return

    state["chat_id"] = chat_id
    state["step"] = "write_tag"
    pending[admin_id] = state

    await callback.message.answer(
        "Группа выбрана.\n"
        "Теперь напиши тег, который нужно поставить.\n\n"
        "Примеры:\n"
        "Проверен\n"
        "WB\n"
        "Ozon\n"
        "Ночь"
    )
    await callback.answer()


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
