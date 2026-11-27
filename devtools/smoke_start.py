"""Запуск bot.main() без Telegram: проверяет чтение .env, открытие базы, фоновые задачи и выход.

Запускается интерпретатором из папки бота:
    .venv/bin/python ../../devtools/smoke_start.py .

Сеть к Telegram не нужна: Bot.me и long polling подменяются, отправок в канал нет.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

folder = Path(sys.argv[1]).resolve()
os.chdir(folder)
sys.path.insert(0, str(folder))

import bot  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402


async def main():
    started = {}

    async def polling(self, *bots, **kwargs):
        store = kwargs["store"]
        started["drafts"] = store.counts(1001, 1001)
        started["state_tables"] = [r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('source_state','quotes')")]
        await asyncio.sleep(0.2)  # фоновые задачи успевают стартовать

    with patch.object(Bot, "me", AsyncMock(return_value=SimpleNamespace(username="test_bot"))), \
         patch.object(Dispatcher, "start_polling", polling), \
         patch.object(Bot, "send_message", AsyncMock()), patch.object(Bot, "send_photo", AsyncMock()):
        await bot.main()
    print("main() завершился штатно:", started)


asyncio.run(main())
