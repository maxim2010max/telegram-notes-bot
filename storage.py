# 🗄 Хранилище заметок на SQLite
#
# Почему SQLite, а не JSON:
#   - заметок может быть много, искать по ним в JSON неудобно;
#   - SQLite уже встроен в Python, отдельный сервер не нужен;
#   - aiosqlite не блокирует бота, пока идёт запись.
#
# База лежит рядом с этим файлом: notes.db

import os
import re
from datetime import datetime

import aiosqlite

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "notes.db")

# Теги вида #дом, #работа, #идея
TAG_RE = re.compile(r"#([\w\-]+)", re.UNICODE)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def extract_tags(text):
    """Достаёт теги из текста: '#Дом купить хлеб' → 'дом'."""
    tags = {match.lower() for match in TAG_RE.findall(text or "")}
    return " ".join(sorted(tags))


def _now_str():
    return datetime.now().strftime(TIME_FORMAT)


def _to_note(row):
    """Превращает строку из базы в обычный словарь."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "text": row["text"],
        "tags": row["tags"],
        "created_at": row["created_at"],
    }


async def init_db():
    """Создаёт таблицу и индекс, если их ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                search_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_user "
            "ON notes(user_id, id)"
        )
        await db.commit()


async def add_note(user_id, text):
    """Добавляет заметку и возвращает её в виде словаря."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустая заметка")

    tags = extract_tags(text)
    search_text = f"{text} {tags}".lower()
    created_at = _now_str()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT INTO notes (user_id, text, tags, search_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, text, tags, search_text, created_at),
        )
        await db.commit()
        note_id = cursor.lastrowid
        cursor = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = await cursor.fetchone()

    return _to_note(row)


async def get_note(note_id, user_id):
    """Возвращает заметку только её владельцу."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM notes WHERE id = ? AND user_id = ?",
            (note_id, user_id),
        )
        row = await cursor.fetchone()
    return _to_note(row)


async def list_notes(user_id, limit=10):
    """Последние заметки пользователя (сначала новые)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM notes WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
    return [_to_note(row) for row in rows]


async def search_notes(user_id, query, limit=20):
    """
    Ищет по тексту и тегам без учёта регистра.
    lower() делаем в Python: SQLite не умеет lower() для кириллицы.
    """
    query = (query or "").strip().lower()
    if not query:
        return []

    # Чтобы % и _ в запросе не работали как шаблоны LIKE
    escaped = (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM notes WHERE user_id = ? "
            "AND search_text LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT ?",
            (user_id, f"%{escaped}%", limit),
        )
        rows = await cursor.fetchall()
    return [_to_note(row) for row in rows]


async def count_notes(user_id):
    """Сколько всего заметок у пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM notes WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
    return row[0] if row else 0


async def delete_note(note_id, user_id):
    """Удаляет заметку. Чужую удалить нельзя. Возвращает True/False."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM notes WHERE id = ? AND user_id = ?",
            (note_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0
