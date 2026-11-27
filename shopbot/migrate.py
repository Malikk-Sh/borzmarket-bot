"""Перенос настроек и базы из предыдущей папки BorzMarket.

    .venv/bin/python migrate.py ~/borzmarket-v9-1/shopbot

Первый запуск копирует .env и borzmarket.sqlite3 (через SQLite Backup, старая база не меняется).
Повторный запуск ничего не перезаписывает. Если база здесь уже есть, а прежняя версия после
отката публиковала посты, их записи убираются из здешней очереди, чтобы не отправить второй раз.
"""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from queue_sync import backup, reconcile


def migrate(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"Папка с прошлой версией не найдена: {source}")
    if source.resolve() == target.resolve():
        raise SystemExit("Укажите папку прежней версии, а не текущую.")
    old_env, new_env = source / ".env", target / ".env"
    if old_env.is_file() and not new_env.exists():
        with new_env.open("xb") as stream:
            stream.write(old_env.read_bytes())
        new_env.chmod(0o600)
        print("Настройки перенесены.")
    elif new_env.exists():
        print("Настройки уже есть, оставляю как есть.")

    old_db, new_db = source / "borzmarket.sqlite3", target / "borzmarket.sqlite3"
    if old_db.is_file() and not new_db.exists():
        with TemporaryDirectory(dir=target) as work:
            copy = Path(work) / new_db.name
            with closing(sqlite3.connect(old_db.resolve().as_uri() + "?mode=ro", uri=True)) as origin:
                with closing(sqlite3.connect(copy)) as backup_db:
                    origin.backup(backup_db)
            copy.replace(new_db)
        print("Черновики и очередь перенесены.")
    elif old_db.is_file() and new_db.exists():
        plan = reconcile(new_db, old_db, unknown="keep", apply=False)
        if any(line.startswith(("🗑", "⚠️", "↩️", "🕓", "📚")) for line in plan):
            saved = backup(new_db)
            reconcile(new_db, old_db, unknown="keep", apply=True)
            print("База уже была. Сверил очередь с прежней версией:")
            print("\n".join(plan))
            print(f"Копия до сверки: {saved}")
        else:
            print("База уже есть, очередь совпадает с прежней версией.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", type=Path, help="папка shopbot предыдущей версии")
    migrate(parser.parse_args().source.expanduser(), Path(__file__).resolve().parent)
