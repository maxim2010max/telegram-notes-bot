# 🤖 Мой Telegram-бот v2.2 — заметки, напоминания и мини-приложение
# Умеет: /start → меню, эхо-режим, случайное число, заметки (/note, /notes,
#        /find, голосовые), напоминания (/remind), Mini App (калькулятор + погода)

# ---------- Импорты (наши инструменты) ----------
import asyncio
import json
import os
import random                                   # для случайных чисел 🎲
import re
import sys
import tempfile
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F          # F — фильтр, помогает различать кнопки
from aiogram.filters import Command             # для команды /start
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    WebAppInfo,                                  # для кнопки Mini App
)

from aiohttp import ClientSession, web

import storage                                     # заметки в SQLite
from stt import STTError, transcribe               # голос → текст (Groq Whisper)
from webapp_server import create_app             # локальный сервер мини-приложения
from tunnel import start_quick_tunnel, stop_tunnel  # автоматический HTTPS-туннель

# Чтобы emoji в print не роняли бота, если вывод перенаправлен в файл (Windows)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Все пути считаем от папки бота, а не от текущей папки запуска
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- Настройки и .env ----------
def load_env(path=None):
    """Читает .env в словарь: {"BOT_TOKEN": "...", "WEBAPP_URL": "..."}."""
    path = path or os.path.join(BASE_DIR, ".env")
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return data


ENV = load_env()
TOKEN = ENV.get("BOT_TOKEN") or os.getenv("BOT_TOKEN")
WEBAPP_URL = (ENV.get("WEBAPP_URL") or os.getenv("WEBAPP_URL") or "").strip()
GROQ_API_KEY = (ENV.get("GROQ_API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
STT_LANGUAGE = (ENV.get("STT_LANGUAGE") or os.getenv("STT_LANGUAGE") or "ru").strip()

if not TOKEN or TOKEN.startswith("ВСТАВЬ"):
    print("❌ Токен не настроен! Открой файл .env и вставь токен от @BotFather")
    exit()

bot = Bot(token=TOKEN)
dp = Dispatcher()

MAX_NOTE_LEN = 3000  # больше — уже не «быстрая заметка»

# ---------- Состояния в памяти ----------
user_echo_state = {}         # {user_id: True/False} — включён ли эхо-режим
pending_note_remind = {}     # {user_id: note_id} — ждём своё время для напоминания

# ---------- Напоминания: хранение в файле ----------
REMINDERS_FILE = os.path.join(BASE_DIR, "reminders.json")
USER_TIME_FORMAT = "%Y-%m-%d %H:%M"  # как храним время внутри


def load_reminders():
    """Читает список напоминаний из файла. Если файла нет — пустой список."""
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_reminders(items):
    """Сохраняет список напоминаний в файл (чтобы не пропали после перезапуска)."""
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def add_reminder(user_id, chat_id, text, due):
    """Добавляет напоминание и возвращает его id. Используется и заметками."""
    items = load_reminders()
    new_id = max([r.get("id", 0) for r in items], default=0) + 1
    items.append({
        "id": new_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "time": due.strftime(USER_TIME_FORMAT),
        "done": False,
    })
    save_reminders(items)
    return new_id


def reminder_due_preset(preset):
    """Готовые варианты времени для кнопки «Напомнить» у заметки."""
    now = datetime.now()

    if preset == "1h":
        return now + timedelta(hours=1)

    if preset == "tomorrow9":
        return (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )

    if preset == "today20":
        due = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if due <= now:                 # уже поздно → напомним завтра
            due += timedelta(days=1)
        return due

    return None


def parse_reminder(text):
    """
    Разбирает строку после /remind.
    Поддерживаем три формата:
      10:00 Купить молоко            → сегодня или завтра (если время прошло)
      08.11 10:00 Купить молоко      → конкретный день (этот год)
      2026-11-08 10:00 Купить молоко → конкретная дата
    Возвращает (datetime, текст) или (None, текст_ошибки).
    """
    text = text.strip()

    # 1) Формат с полной датой: 2026-11-08 10:00 текст
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)$", text)
    if m:
        year, month, day, hour, minute = map(int, m.groups()[:5])
        body = m.group(6).strip()
        try:
            due = datetime(year, month, day, hour, minute)
        except ValueError:
            return None, "🤔 Такой даты не существует. Проверь число и месяц."
        return due, body

    # 2) Формат с датой без года: 08.11 10:00 текст
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)$", text)
    if m:
        day, month, hour, minute = map(int, m.groups()[:4])
        body = m.group(5).strip()
        now = datetime.now()
        try:
            due = datetime(now.year, month, day, hour, minute)
        except ValueError:
            return None, "🤔 Такой даты не существует. Проверь число и месяц."
        if due <= now:
            return None, "⏳ Это время уже прошло. Укажи будущую дату."
        return due, body

    # 3) Простой формат: 10:00 текст
    m = re.match(r"^(\d{1,2}):(\d{2})\s+(.+)$", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        body = m.group(3).strip()
        if hour > 23 or minute > 59:
            return None, "⏰ Не бывает такого времени. Часы 0–23, минуты 0–59."
        now = datetime.now()
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:                     # время уже прошло сегодня → значит завтра
            due += timedelta(days=1)
        return due, body

    return None, (
        "🤔 Не понял время.\n"
        "Пиши так: `/remind 10:00 Купить молоко`\n"
        "Или так: `/remind 08.11 10:00 Поздравить маму`"
    )


def reminder_text(r):
    """Красиво показывает одно напоминание в списке."""
    try:
        due = datetime.strptime(r["time"], USER_TIME_FORMAT)
    except (KeyError, ValueError):
        return "❓ Напоминание с битой датой"
    return f"📅 {due.strftime('%d.%m в %H:%M')} — {r['text']}"


def user_reminders(user_id):
    """Активные (не отправленные) напоминания конкретного пользователя."""
    items = [r for r in load_reminders() if r.get("user_id") == user_id and not r.get("done")]
    items.sort(key=lambda r: r.get("time", ""))
    return items


async def reminder_loop():
    """Фоновая задача: раз в 15 секунд проверяем, не пора ли что-то напомнить."""
    while True:
        now = datetime.now()
        items = load_reminders()
        changed = False

        for r in items:
            if r.get("done"):
                continue
            try:
                due = datetime.strptime(r["time"], USER_TIME_FORMAT)
            except (KeyError, ValueError):
                continue

            if due <= now:
                try:
                    await bot.send_message(
                        r["chat_id"],
                        f"⏰ Напоминание: {r['text']}",
                    )
                except Exception as e:
                    print(f"⚠️ Не смог отправить напоминание: {e}")
                r["done"] = True
                changed = True

        if changed:
            save_reminders(items)

        await asyncio.sleep(15)


# ---------- ШАГ 1: строим главное меню ----------
# Кнопки — это список списков: каждый внутренний список = ряд кнопок
def main_menu():
    rows = [
        [
            InlineKeyboardButton(text="🦜 Эхо-режим", callback_data="echo"),
            InlineKeyboardButton(text="🎲 Случайное число", callback_data="dice"),
        ],
        [
            InlineKeyboardButton(text="📝 Заметки", callback_data="notes_menu"),
            InlineKeyboardButton(text="⏰ Напоминания", callback_data="reminders"),
        ],
        [
            # Если WEBAPP_URL настроен — показываем настоящую кнопку Mini App.
            # Если нет — кнопка-подсказка, как его включить.
            InlineKeyboardButton(text="🧮 Мини-приложение", callback_data="webapp_help")
            if not WEBAPP_URL
            else InlineKeyboardButton(text="🧮 Мини-приложение", web_app=WebAppInfo(url=WEBAPP_URL)),
        ],
        [
            InlineKeyboardButton(text="ℹ️ О боте", callback_data="about"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# callback_data — это "секретная метка" кнопки.
# Пользователь видит "🎲 Случайное число", а бот получает "dice".
# По этой метке мы и понимаем, куда нажали.

# ---------- ШАГ 2: команда /start ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "Привет! 👋 Я бот с меню.\n"
        "Выбери, что тебе нужно:\n\n"
        "📝 Заметки: /note Купить молоко\n"
        "🔎 Поиск: /find молоко\n"
        "🎤 Голосовое — сохраню как заметку\n"
        "⏰ Напоминания: /remind 10:00 Купить молоко\n"
        "🧮 Калькулятор и погода — в мини-приложении"
    )
    await message.answer(text, reply_markup=main_menu())


# ---------- ШАГ 3: обработка кликов по кнопкам ----------
@dp.callback_query(F.data == "echo")
async def btn_echo(callback: CallbackQuery):
    user_id = callback.from_user.id
    new_state = not user_echo_state.get(user_id, False)
    user_echo_state[user_id] = new_state

    if new_state:
        await callback.message.answer("🦜 Эхо-режим ВКЛЮЧЕН! Теперь я буду повторять твои сообщения.")
    else:
        await callback.message.answer("🔇 Эхо-режим ВЫКЛЮЧЕН. Я больше не буду повторять твои сообщения.")
    await callback.answer()


@dp.callback_query(F.data == "dice")
async def btn_dice(callback: CallbackQuery):
    number = random.randint(1, 100)
    await callback.message.answer(f"🎲 Твоё число: {number}")
    await callback.answer()


@dp.callback_query(F.data == "reminders")
async def btn_reminders(callback: CallbackQuery):
    items = user_reminders(callback.from_user.id)

    if not items:
        await callback.message.answer(
            "⏰ У тебя пока нет напоминаний.\n\n"
            "Добавить очень просто:\n"
            "`/remind 10:00 Купить молоко`\n"
            "`/remind 08.11 18:30 Позвонить бабушке`",
            parse_mode="Markdown",
        )
        await callback.answer()
        return

    lines = ["⏰ Твои напоминания:\n"]
    for i, r in enumerate(items, start=1):
        lines.append(f"{i}. {reminder_text(r)}")

    buttons = [
        [InlineKeyboardButton(text=f"❌ {r['text'][:25]}", callback_data=f"del:{r['id']}")]
        for r in items[:15]
    ]
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("del:"))
async def btn_delete_reminder(callback: CallbackQuery):
    try:
        reminder_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Что-то не так с кнопкой 😅", show_alert=True)
        return

    items = load_reminders()
    before = len(items)
    # Удаляем только своё напоминание (чужое по id удалить нельзя)
    items = [
        r for r in items
        if not (r.get("id") == reminder_id and r.get("user_id") == callback.from_user.id)
    ]

    if len(items) == before:
        await callback.answer("Уже удалено 🤷", show_alert=True)
        return

    save_reminders(items)
    await callback.message.edit_text("✅ Напоминание удалено.")
    await callback.answer("Удалено")


@dp.callback_query(F.data == "webapp_help")
async def btn_webapp_help(callback: CallbackQuery):
    await callback.message.answer(
        "🧮 Мини-приложение сейчас недоступно.\n\n"
        "Бот поднимает туннель автоматически при запуске, "
        "но в этот раз что-то пошло не так. Проверь:\n"
        "1. Есть ли интернет\n"
        "2. Не блокирует ли VPN соединение (попробуй выключить)\n"
        "3. Перезапусти бота\n\n"
        "Пока можно открыть http://localhost:8080 в браузере — всё работает и так 😉",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "about")
async def btn_about(callback: CallbackQuery):
    await callback.message.answer(
        "ℹ️ Обо мне:\n"
        "Я бот v2.2 🤖\n"
        "Сделан на Python + aiogram 3 + SQLite.\n"
        "Умею хранить заметки, напоминать о делах "
        "и открывать мини-приложение.\n"
        "Мой автор учится в IT школе Samsung 💪"
    )
    await callback.answer()


# ---------- ШАГ 4: напоминания ----------
@dp.message(Command("remind"))
async def cmd_remind(message: Message):
    # message.text = "/remind 10:00 Купить молоко"
    parts = (message.text or "").split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "⏰ Как добавить напоминание:\n\n"
            "`/remind 10:00 Купить молоко`\n"
            "`/remind 08.11 18:30 Позвонить бабушке`\n"
            "`/remind 2026-12-31 23:59 Загадать желание`\n\n"
            "Список: /reminders",
            parse_mode="Markdown",
        )
        return

    due, body = parse_reminder(parts[1])
    if due is None:
        await message.answer(body, parse_mode="Markdown")
        return

    add_reminder(message.from_user.id, message.chat.id, body, due)

    await message.answer(
        f"✅ Записал!\n"
        f"📅 {due.strftime('%d.%m.%Y в %H:%M')}\n"
        f"📝 {body}"
    )


@dp.message(Command("reminders"))
async def cmd_reminders(message: Message):
    items = user_reminders(message.from_user.id)
    if not items:
        await message.answer("⏰ Напоминаний пока нет. Добавь: /remind 10:00 Купить молоко")
        return

    lines = ["⏰ Твои напоминания:\n"]
    for i, r in enumerate(items, start=1):
        lines.append(f"{i}. {reminder_text(r)}")

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"❌ {r['text'][:25]}", callback_data=f"del:{r['id']}")]
                for r in items[:15]
            ]
        ),
    )


# ---------- 📝 ЗАМЕТКИ ----------
def note_preview(text, limit=60):
    """Одна строка для списка: убираем переносы и обрезаем."""
    one_line = " ".join((text or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def note_date(note):
    """Дата заметки в коротком виде: 07.11 23:40."""
    try:
        return datetime.strptime(
            note["created_at"], "%Y-%m-%d %H:%M:%S"
        ).strftime("%d.%m %H:%M")
    except (KeyError, ValueError):
        return "—"


def note_tags_line(note):
    if not note.get("tags"):
        return ""
    tags = ", ".join("#" + tag for tag in note["tags"].split())
    return f"\n🏷 Теги: {tags}"


def note_detail_text(note):
    return (
        f"📝 Заметка #{note['id']}\n"
        f"🕒 {note_date(note)}\n\n"
        f"{note['text']}{note_tags_line(note)}"
    )


def notes_keyboard(notes):
    """Кнопки с номерами заметок — по 5 в ряд."""
    rows, row = [], []
    for i, note in enumerate(notes, start=1):
        row.append(InlineKeyboardButton(
            text=str(i), callback_data=f"note:view:{note['id']}"
        ))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notes_list_text(notes, title):
    lines = [title, ""]
    for i, note in enumerate(notes, start=1):
        lines.append(f"{i}. {note_date(note)} — {note_preview(note['text'])}")
    lines.append("")
    lines.append("Нажми номер, чтобы открыть заметку.")
    return "\n".join(lines)


async def notes_payload(user_id, title="📝 Твои заметки"):
    """Текст + кнопки для /notes и кнопки меню."""
    total = await storage.count_notes(user_id)
    notes = await storage.list_notes(user_id, limit=10)

    if not notes:
        return (
            "📝 Заметок пока нет.\n\n"
            "Сохранить: /note Купить молоко\n"
            "С тегом: /note #дом Купить молоко\n"
            "Найти: /find молоко\n"
            "Или отправь голосовое 🎤",
            None,
        )

    title = f"{title} (всего {total}, показаны последние {len(notes)}):"
    return notes_list_text(notes, title), notes_keyboard(notes)


def note_actions(note_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏰ Напомнить", callback_data=f"note:rem:{note_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"note:del:{note_id}"),
    ]])


def back_to_notes():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ К заметкам", callback_data="note:list")
    ]])


async def edit_or_send(message, text, reply_markup=None):
    """Правит сообщение, а если Telegram не разрешает (старое) — шлёт новое."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await message.answer(text, reply_markup=reply_markup)


async def save_and_show(message, text):
    """Сохраняет заметку и показывает карточку с кнопками."""
    if len(text) > MAX_NOTE_LEN:
        await message.answer(
            f"✂️ Слишком длинная заметка — максимум {MAX_NOTE_LEN} символов."
        )
        return

    note = await storage.add_note(message.from_user.id, text)
    await message.answer(
        f"✅ Заметка #{note['id']} сохранена\n"
        f"🕒 {note_date(note)}\n\n"
        f"{note['text']}{note_tags_line(note)}",
        reply_markup=note_actions(note["id"]),
    )


@dp.message(Command("note"))
async def cmd_note(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""

    # Если ответить на сообщение и написать /note — сохраним это сообщение
    if not text and message.reply_to_message:
        source = message.reply_to_message
        text = (source.text or source.caption or "").strip()

    if not text:
        await message.answer(
            "📝 Как сохранить заметку:\n\n"
            "`/note Купить молоко`\n"
            "`/note #дом Купить молоко` — с тегом\n"
            "Ответь на сообщение и напиши `/note` — сохраню его\n\n"
            "Список: /notes\n"
            "Поиск: /find молоко\n"
            "Или отправь голосовое 🎤",
            parse_mode="Markdown",
        )
        return

    await save_and_show(message, text)


@dp.message(Command("notes"))
async def cmd_notes(message: Message):
    text, keyboard = await notes_payload(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@dp.message(Command("find"))
async def cmd_find(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    if not query:
        await message.answer(
            "🔎 Как искать заметки:\n\n"
            "`/find молоко`\n"
            "`/find #дом`\n\n"
            "Ищет по тексту и тегам без учёта регистра.",
            parse_mode="Markdown",
        )
        return

    notes = await storage.search_notes(message.from_user.id, query, limit=20)
    if not notes:
        await message.answer(f"🔎 По запросу «{query}» ничего не нашёл 🤷")
        return

    title = f"🔎 Нашёл {len(notes)} по запросу «{query}»:"
    await message.answer(
        notes_list_text(notes, title),
        reply_markup=notes_keyboard(notes),
    )


@dp.callback_query(F.data == "notes_menu")
async def btn_notes_menu(callback: CallbackQuery):
    text, keyboard = await notes_payload(callback.from_user.id)
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "note:list")
async def btn_note_list(callback: CallbackQuery):
    text, keyboard = await notes_payload(callback.from_user.id)
    await edit_or_send(callback.message, text, keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("note:view:"))
async def btn_note_view(callback: CallbackQuery):
    try:
        note_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Что-то не так с кнопкой 😅", show_alert=True)
        return

    note = await storage.get_note(note_id, callback.from_user.id)
    if not note:
        await callback.answer(
            "Заметка не найдена — возможно, уже удалена.",
            show_alert=True,
        )
        return

    await edit_or_send(
        callback.message,
        note_detail_text(note)[:4000],
        InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏰ Напомнить", callback_data=f"note:rem:{note_id}"
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить", callback_data=f"note:del:{note_id}"
                ),
            ],
            [InlineKeyboardButton(text="↩️ К заметкам", callback_data="note:list")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("note:del:"))
async def btn_note_delete(callback: CallbackQuery):
    try:
        note_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Что-то не так с кнопкой 😅", show_alert=True)
        return

    note = await storage.get_note(note_id, callback.from_user.id)
    if not note:
        await callback.answer("Уже удалено 🤷", show_alert=True)
        return

    await storage.delete_note(note_id, callback.from_user.id)
    await edit_or_send(
        callback.message,
        f"🗑 Заметка #{note_id} удалена:\n\n{note_preview(note['text'], 300)}",
        back_to_notes(),
    )
    await callback.answer("Удалено")


@dp.callback_query(F.data.startswith("note:rem:"))
async def btn_note_remind(callback: CallbackQuery):
    try:
        note_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Что-то не так с кнопкой 😅", show_alert=True)
        return

    note = await storage.get_note(note_id, callback.from_user.id)
    if not note:
        await callback.answer("Заметка не найдена 🤷", show_alert=True)
        return

    await edit_or_send(
        callback.message,
        f"⏰ Когда напомнить?\n\n📝 {note_preview(note['text'], 250)}",
        InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏰ Через час",
                    callback_data=f"note:rem_at:{note_id}:1h",
                ),
                InlineKeyboardButton(
                    text="🌅 Завтра в 9:00",
                    callback_data=f"note:rem_at:{note_id}:tomorrow9",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌙 Сегодня в 20:00",
                    callback_data=f"note:rem_at:{note_id}:today20",
                ),
                InlineKeyboardButton(
                    text="✍️ Своё время",
                    callback_data=f"note:rem_custom:{note_id}",
                ),
            ],
            [InlineKeyboardButton(text="↩️ Назад", callback_data=f"note:view:{note_id}")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("note:rem_at:"))
async def btn_note_remind_at(callback: CallbackQuery):
    try:
        _, _, raw_id, preset = callback.data.split(":", 3)
        note_id = int(raw_id)
    except (IndexError, ValueError):
        await callback.answer("Что-то не так с кнопкой 😅", show_alert=True)
        return

    due = reminder_due_preset(preset)
    note = await storage.get_note(note_id, callback.from_user.id)
    if not note or due is None:
        await callback.answer("Не получилось 🤷", show_alert=True)
        return

    add_reminder(
        callback.from_user.id,
        callback.message.chat.id,
        note["text"],
        due,
    )
    await edit_or_send(
        callback.message,
        f"✅ Напомню {due.strftime('%d.%m в %H:%M')}\n\n"
        f"📝 {note_preview(note['text'], 300)}",
        back_to_notes(),
    )
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("note:rem_custom:"))
async def btn_note_remind_custom(callback: CallbackQuery):
    try:
        note_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Что-то не так с кнопкой 😅", show_alert=True)
        return

    note = await storage.get_note(note_id, callback.from_user.id)
    if not note:
        await callback.answer("Заметка не найдена 🤷", show_alert=True)
        return

    pending_note_remind[callback.from_user.id] = note_id
    await callback.message.answer(
        f"✍️ Напиши время для заметки #{note_id}.\n\n"
        "Например:\n`10:00`\n`08.11 18:30`\n\n"
        "/cancel — отмена",
        parse_mode="Markdown",
    )
    await callback.answer()


# ---------- 🎤 Голосовые заметки ----------
@dp.message(F.voice)
async def on_voice_note(message: Message):
    voice = message.voice

    if voice.duration and voice.duration > 300:
        await message.answer("🎤 Слишком длинное голосовое — максимум 5 минут.")
        return
    if voice.file_size and voice.file_size > 20 * 1024 * 1024:
        await message.answer("🎤 Файл слишком большой (максимум 20 МБ).")
        return

    if not GROQ_API_KEY:
        await message.answer(
            "🎤 Для голосовых нужен бесплатный ключ Groq.\n\n"
            "1. Зайди на console.groq.com → API Keys\n"
            "2. Создай ключ и добавь в TelegramBot/.env:\n"
            "`GROQ_API_KEY=твой_ключ`\n"
            "3. Перезапусти бота\n\n"
            "Пока можно писать заметки текстом: /note ...",
            parse_mode="Markdown",
        )
        return

    status = await message.answer("🎤 Распознаю голосовое…")
    path = os.path.join(
        tempfile.gettempdir(), f"note_{message.message_id}.ogg"
    )

    try:
        await message.bot.download(voice, destination=path)
        with open(path, "rb") as f:
            audio = f.read()
        text = await transcribe(
            audio,
            filename="voice.ogg",
            mime_type=voice.mime_type or "audio/ogg",
            language=STT_LANGUAGE,
            api_key=GROQ_API_KEY,
        )
    except STTError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception as e:
        await status.edit_text(f"❌ Не смог скачать голосовое: {e}")
        return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if not text:
        await status.edit_text("🎤 Не разобрал речь. Попробуй ещё раз.")
        return

    cut = len(text) > MAX_NOTE_LEN
    text = text[:MAX_NOTE_LEN]

    note = await storage.add_note(message.from_user.id, text)
    extra = "\n\n✂️ Обрезал до 3000 символов." if cut else ""
    await status.edit_text(
        f"✅ Заметка #{note['id']} сохранена\n\n"
        f"🎤 {text}{extra}",
        reply_markup=note_actions(note["id"]),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if pending_note_remind.pop(message.from_user.id, None) is not None:
        await message.answer("✅ Отменил.")
    else:
        await message.answer("Нечего отменять 🙂")


# ---------- ШАГ 5: обычный текст (эхо) ----------
@dp.message()
async def echo(message: Message):
    if not message.text:
        await message.answer("Я понимаю только текст 📝")
        return

    if message.text.startswith("/"):
        await message.answer("Неизвестная команда 😅 Нажми /start")
        return

    user_id = message.from_user.id

    # Ждём своё время для напоминания по заметке?
    if user_id in pending_note_remind:
        note_id = pending_note_remind[user_id]
        due, body = parse_reminder(message.text)
        if due is None:
            await message.answer(
                body + "\n\n/cancel — отмена",
                parse_mode="Markdown",
            )
            return

        note = await storage.get_note(note_id, user_id)
        pending_note_remind.pop(user_id, None)

        if not note:
            await message.answer("🤔 Заметка уже удалена — напоминание не создал.")
            return

        add_reminder(user_id, message.chat.id, note["text"], due)
        await message.answer(
            f"✅ Напомню {due.strftime('%d.%m.%Y в %H:%M')}\n\n"
            f"📝 {note_preview(note['text'], 300)}"
        )
        return

    # Повторяем только если пользователь сам включил эхо-режим
    if user_echo_state.get(user_id, False):
        await message.answer(f"🦜 Ты написал: {message.text}")
    else:
        await message.answer(
            "🦜 Эхо-режим выключен.\n\n"
            "📝 Сохранить этот текст как заметку: /note <текст>\n"
            "/start — открыть меню"
        )


# ---------- Запуск ----------
async def check_public_url(url):
    """Проверяет, что мини-приложение реально открывается по HTTPS."""
    async with ClientSession() as session:
        for _ in range(5):
            try:
                async with session.get(url, timeout=15) as response:
                    text = await response.text()
                    return response.status, len(text)
            except Exception:
                await asyncio.sleep(2)
    return None, None


async def main():
    global WEBAPP_URL

    # ---------- 0. База заметок ----------
    await storage.init_db()
    print(f"🗄 База заметок: {storage.DB_PATH}")

    # ---------- 1. Локальный сервер мини-приложения ----------
    port = int(os.getenv("WEBAPP_PORT", "8080"))
    runner = web.AppRunner(create_app())
    await runner.setup()

    try:
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        print(f"🌐 Мини-приложение локально: http://localhost:{port}")
    except OSError:
        print(f"⚠️ Порт {port} занят — похоже, сервер уже запущен. Использую его.")

    # ---------- 2. HTTPS-туннель (Telegram не открывает http://) ----------
    tunnel_proc = None
    if WEBAPP_URL:
        # Если в .env задан свой адрес — используем его и туннель не поднимаем
        print(f"🔗 Mini App: {WEBAPP_URL} (адрес из .env)")
    else:
        print("🚇 Поднимаю туннель localhost.run… 10–20 секунд")
        WEBAPP_URL, tunnel_proc = await start_quick_tunnel(port)
        if WEBAPP_URL:
            print(f"🔗 Mini App: {WEBAPP_URL}")
        else:
            print("⚠️ Туннель не поднялся — кнопка Mini App покажет подсказку")

    # ---------- Проверка без запуска бота: python bot.py --check ----------
    if "--check" in sys.argv:
        if WEBAPP_URL:
            status, size = await check_public_url(WEBAPP_URL)
            print(f"🔎 Проверка публичной ссылки: HTTP {status}, {size} байт")
        else:
            print("🔎 Проверка: публичной ссылки нет")
        await stop_tunnel(tunnel_proc)
        await runner.cleanup()
        return

    # ---------- 3. Сам бот ----------
    print("🚀 Бот v2.2 запущен! Для остановки — Ctrl+C")
    asyncio.create_task(reminder_loop())

    try:
        await dp.start_polling(bot)
    finally:
        print("🛑 Останавливаюсь…")
        await stop_tunnel(tunnel_proc)
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
