# 🎤 Распознавание голосовых через Groq Whisper
#
# Нужен бесплатный ключ Groq:
#   1. Зарегистрируйся на https://console.groq.com
#   2. Создай API Key
#   3. Добавь в TelegramBot/.env строку: GROQ_API_KEY=твой_ключ
#
# Модель whisper-large-v3-turbo быстрая и хорошо понимает русский.

import json
import os

import aiohttp

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "whisper-large-v3-turbo"


class STTError(Exception):
    """Ошибка распознавания речи."""


async def transcribe(
    audio_bytes,
    filename="voice.ogg",
    mime_type="audio/ogg",
    language="ru",
    api_key=None,
    model=None,
):
    """Отправляет аудио в Groq Whisper и возвращает распознанный текст."""
    key = api_key or os.getenv("GROQ_API_KEY", "")
    if not key:
        raise STTError(
            "Нет ключа Groq для распознавания речи.\n"
            "Добавь в TelegramBot/.env строку:\n"
            "GROQ_API_KEY=твой_ключ\n"
            "Ключ бесплатно выдаётся на console.groq.com"
        )

    model = model or os.getenv("STT_MODEL", DEFAULT_MODEL)

    data = aiohttp.FormData()
    data.add_field("file", audio_bytes, filename=filename, content_type=mime_type)
    data.add_field("model", model)
    data.add_field("language", language)
    data.add_field("response_format", "json")
    data.add_field("temperature", "0")

    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                GROQ_URL,
                data=data,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise STTError(f"Groq HTTP {response.status}: {body[:200]}")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as e:
                    raise STTError("Groq вернул непонятный ответ 😕") from e
                return (payload.get("text") or "").strip()
    except aiohttp.ClientError as e:
        raise STTError(f"Нет связи с Groq: {e}") from e
