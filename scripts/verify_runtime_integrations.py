#!/usr/bin/env python3
"""Read-only PH1 runtime integration smoke without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path


def read_settings(path: Path) -> dict[str, str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            str(key): str(value or "")
            for key, value in connection.execute("SELECT key, value FROM settings")
        }
    finally:
        connection.close()


async def verify_telegram(settings: dict[str, str]) -> str:
    token = settings.get("bot:token", "").strip()
    if not token:
        return "SKIP_NOT_CONFIGURED"

    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession

    from src.bot_runner import build_proxy_url

    proxy_enabled = settings.get("bot:proxy_enabled", "0") == "1"
    proxy_url = None
    if proxy_enabled:
        proxy_url = build_proxy_url(
            settings.get("bot:proxy_host", ""),
            settings.get("bot:proxy_port", "1080"),
            settings.get("bot:proxy_user", ""),
            settings.get("bot:proxy_pass", ""),
        )
        if proxy_url is None:
            raise RuntimeError("configured Telegram proxy is incomplete")

    # Match the production run_all path exactly: aiohttp-socks accepts
    # socks5:// while build_proxy_url deliberately uses socks5h:// to express
    # remote DNS semantics at the configuration boundary.
    aiogram_proxy = proxy_url.replace("socks5h://", "socks5://") if proxy_url else None
    session = AiohttpSession(proxy=aiogram_proxy) if aiogram_proxy else AiohttpSession()
    bot = Bot(token=token, session=session)
    try:
        identity = await bot.get_me()
        if not identity.id:
            raise RuntimeError("Telegram getMe returned no bot id")
    except Exception as exc:
        raise RuntimeError(
            f"Telegram integration failed safely: {type(exc).__name__}"
        ) from None
    finally:
        await bot.session.close()
    return "PASS_PROXY" if proxy_enabled else "PASS_DIRECT"


async def verify_openrouter(
    settings: dict[str, str], *, expected_http_status: int | None = None
) -> str:
    key = settings.get("bot:openrouter_api_key", "").strip()
    model = settings.get("bot:openrouter_model", "").strip()
    if not key or not model:
        return "SKIP_NOT_CONFIGURED"

    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://sub.beykus.fun",
                    "X-Title": "MGBoost PH1 Runtime Smoke",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 1,
                    "tools": [],
                },
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    if expected_http_status is not None and response.status == expected_http_status:
                        return f"BASELINE_HTTP_{response.status}"
                    raise RuntimeError(f"unexpected HTTP status {response.status}")
                payload = await response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
                    raise RuntimeError("unexpected completion response schema")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"OpenRouter integration failed safely: {type(exc).__name__}"
        ) from None
    return "PASS"


async def run(path: Path, *, expected_openrouter_status: int | None = None) -> None:
    settings = read_settings(path)
    telegram = await verify_telegram(settings)
    openrouter = await verify_openrouter(
        settings, expected_http_status=expected_openrouter_status
    )
    print(f"telegram_runtime_integration={telegram}")
    print(f"openrouter_runtime_integration={openrouter}")
    print("credentials_printed=0")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--expected-openrouter-status", type=int)
    args = parser.parse_args()
    asyncio.run(
        run(args.db, expected_openrouter_status=args.expected_openrouter_status)
    )


if __name__ == "__main__":
    main()
