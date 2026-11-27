"""Откат на прежнюю папку BorzMarket без повторной отправки постов.

Запуск из папки ЭТОЙ (новой) версии, когда оба бота остановлены (Ctrl+C):

    .venv/bin/python rollback.py ~/borzmarket-v9-1/shopbot            # только показать план
    .venv/bin/python rollback.py ~/borzmarket-v9-1/shopbot --apply    # выполнить

Новая база считается главной: всё, что новая версия уже отправила из очереди, убирается из
очереди старой папки и попадает в её историю. Записи, которых в новой версии больше нет,
возвращаются в черновики. Черновики, созданные в новой версии, остаются в её папке.
Перед изменением старая база копируется рядом: borzmarket.sqlite3.before-sync-ДАТА.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from queue_sync import backup, reconcile

HERE = Path(__file__).resolve().parent


def running_bots(folders: list[Path]) -> list[str]:
    """Процессы bot.py, запущенные из указанных папок (насколько это видно через /proc)."""
    found = []
    targets = {folder.resolve() for folder in folders}
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            if "bot.py" not in command:
                continue
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if cwd in targets:
            found.append(f"PID {entry.name} в {cwd}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("old", type=Path, help="папка shopbot прежней версии, куда откатываемся")
    parser.add_argument("--apply", action="store_true", help="выполнить план, а не только показать")
    parser.add_argument("--force", action="store_true", help="не проверять запущенные процессы")
    args = parser.parse_args()
    old = args.old.expanduser().resolve()
    target, source = old / "borzmarket.sqlite3", HERE / "borzmarket.sqlite3"
    if not (old / "bot.py").is_file():
        print(f"В папке {old} нет bot.py. Укажите папку shopbot прежней версии.")
        return 2
    if old == HERE:
        print("Укажите другую папку: это папка текущей версии.")
        return 2
    if not target.is_file() or not source.is_file():
        print("Сверять нечего: в одной из папок нет borzmarket.sqlite3. Можно запускать прежнюю версию.")
        return 0
    if not args.force and (busy := running_bots([old, HERE])):
        print("Сначала остановите бота (Ctrl+C): " + "; ".join(busy))
        return 3
    print(f"Главная база: {source}\nСтарая база: {target}\n")
    plan = reconcile(target, source, unknown="draft", apply=False)
    print("\n".join(plan))
    if not args.apply:
        print("\nЭто план. Чтобы выполнить, добавьте --apply.")
        return 0
    copy = backup(target)
    reconcile(target, source, unknown="draft", apply=True)
    print(f"\nГотово. Копия старой базы до сверки: {copy}")
    print(f"Запуск прежней версии: cd {old} && env -u ADMIN_IDS .venv/bin/python bot.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
