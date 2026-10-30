"""Публичный HTTPS-адрес для Mini App через бесплатный туннель Cloudflare (без регистрации).

Telegram открывает Mini App только по HTTPS. cloudflared пробрасывает локальный порт
и печатает адрес вида https://xxx.trycloudflare.com — его мы и ждём в выводе.
Адрес меняется при каждом запуске, поэтому бот обновляет кнопку приложения сам.
"""

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
# Куда ставит cloudflared winget, если его нет в PATH
WINGET_PATH = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages" / \
    "Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe/cloudflared.exe"


def find_cloudflared() -> str | None:
    return shutil.which("cloudflared") or (str(WINGET_PATH) if WINGET_PATH.exists() else None)


async def start_tunnel(port: int, timeout: float = 40) -> tuple[asyncio.subprocess.Process, str]:
    exe = find_cloudflared()
    if not exe:
        raise RuntimeError("cloudflared не найден. Установите: winget install Cloudflare.cloudflared")

    proc = await asyncio.create_subprocess_exec(
        exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )

    async def wait_url() -> str:
        while line := await proc.stderr.readline():
            if m := URL_RE.search(line):
                return m.group().decode()
        raise RuntimeError("cloudflared завершился, не выдав адрес")

    try:
        url = await asyncio.wait_for(wait_url(), timeout)
    except Exception:
        proc.kill()
        raise

    # Дочитываем вывод в фоне, иначе переполненный буфер остановит cloudflared
    async def drain() -> None:
        while await proc.stderr.readline():
            pass

    asyncio.create_task(drain())
    log.info("Tunnel ready: %s", url)
    return proc, url
