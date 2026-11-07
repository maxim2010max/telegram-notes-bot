# 🚇 Туннель для мини-приложения
# Telegram требует https://, поэтому локальный сервер (http://localhost:8080)
# мы "выставляем" наружу.
#
# Основной вариант — localhost.run: бесплатно, без регистрации, без промежуточных
# страниц. Работает через обычный SSH (он уже есть в Windows).
# Запасной — быстрый туннель Cloudflare (если SSH недоступен).
#
# Ничего настраивать не нужно: bot.py вызывает start_quick_tunnel() сам.

import asyncio
import os
import re
import shutil

# Ссылки, которые печатают туннели в своём выводе
LOCALHOST_RUN_URL = re.compile(r"https://[a-z0-9-]+\.lhr\.life")
CLOUDFLARE_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_ssh():
    """Ищет ssh.exe в PATH и в стандартном месте Windows."""
    exe = shutil.which("ssh")
    if exe:
        return exe

    candidates = [
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "OpenSSH", "ssh.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def find_cloudflared():
    """Ищет cloudflared.exe (запасной вариант туннеля)."""
    exe = shutil.which("cloudflared")
    if exe:
        return exe

    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "cloudflared", "cloudflared.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "cloudflared", "cloudflared.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


async def _drain_output(proc):
    """Читает вывод процесса, чтобы он не завис из-за заполненной трубы."""
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
    except Exception:
        pass


async def _wait_for_url(proc, pattern, timeout):
    """Ждёт в выводе процесса ссылку, подходящую под pattern."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None

        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            return None

        if not line:
            return None

        text = line.decode("utf-8", "ignore")
        match = pattern.search(text)
        if match:
            return match.group(0)


async def _start_localhost_run(port, timeout):
    """Основной туннель: ssh -R 80:localhost:<port> nokey@localhost.run"""
    ssh = find_ssh()
    if not ssh:
        print("⚠️ SSH-клиент не найден — пропускаю localhost.run")
        return None, None

    try:
        proc = await asyncio.create_subprocess_exec(
            ssh,
            "-T",
            "-o", "StrictHostKeyChecking=no",   # не спрашиваем про ключ хоста
            "-o", "UserKnownHostsFile=NUL",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30",     # держим соединение живым
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-R", f"80:127.0.0.1:{port}",       # порт 80 на сервере → наш порт
            "nokey@localhost.run",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        print(f"⚠️ Не смог запустить SSH: {e}")
        return None, None

    url = await _wait_for_url(proc, LOCALHOST_RUN_URL, timeout)
    if url:
        asyncio.create_task(_drain_output(proc))
        return url, proc

    await stop_tunnel(proc)
    return None, None


async def _start_cloudflared(port, timeout):
    """Запасной туннель: cloudflared tunnel --url ..."""
    exe = find_cloudflared()
    if not exe:
        return None, None

    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "tunnel",
            "--url",
            f"http://127.0.0.1:{port}",
            "--no-autoupdate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        print(f"⚠️ Не смог запустить cloudflared: {e}")
        return None, None

    url = await _wait_for_url(proc, CLOUDFLARE_URL, timeout)
    if url:
        asyncio.create_task(_drain_output(proc))
        return url, proc

    await stop_tunnel(proc)
    return None, None


async def start_quick_tunnel(port, timeout=35):
    """
    Поднимает HTTPS-туннель на http://127.0.0.1:<port>.

    Возвращает (url, process) или (None, None), если не получилось.
    Ошибки не бросаем: бот должен запуститься в любом случае.
    """
    url, proc = await _start_localhost_run(port, timeout)
    if url:
        return url, proc

    print("⚠️ localhost.run не ответил, пробую Cloudflare…")
    return await _start_cloudflared(port, timeout=40)


async def stop_tunnel(proc):
    """Аккуратно останавливает процесс туннеля."""
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except ProcessLookupError:
        pass
