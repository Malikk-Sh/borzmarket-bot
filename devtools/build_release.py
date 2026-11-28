"""Сборка релизного архива BorzMarket из папки shopbot/.

    python3 devtools/build_release.py --version 10.1

Создаёт borzmarket_bot_v10_1.zip в корне репозитория. Внутри — каталог shopbot/ без
секретов и рабочих данных: .env, базы SQLite и их WAL/SHM, сессии Telegram, окружения,
кэши Python и логи в архив не попадают. Отказывается собирать, если находит что-то похожее
на токен в файлах архива.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "shopbot"
EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", "examples", "build", ".git"}
EXCLUDE_NAMES = {".env"}
EXCLUDE_SUFFIXES = (".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".session", ".session-journal", ".log", ".pyc")
EXCLUDE_PATTERNS = (re.compile(r"\.before-sync-"), re.compile(r"\.sqlite3\."))
SECRET = re.compile(rb"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")  # формат токена BotFather


def included(path: Path) -> bool:
    relative = path.relative_to(SOURCE)
    if any(part in EXCLUDE_DIRS for part in relative.parts):
        return False
    if path.name in EXCLUDE_NAMES or path.name.endswith(EXCLUDE_SUFFIXES):
        return False
    return not any(pattern.search(path.name) for pattern in EXCLUDE_PATTERNS)


def build(version: str) -> Path:
    tag = version.replace(".", "_")
    target = ROOT / f"borzmarket_bot_v{tag}.zip"
    files = sorted(p for p in SOURCE.rglob("*") if p.is_file() and included(p))
    for path in files:
        if path.suffix in (".py", ".md", ".txt", ".example", ".json") and SECRET.search(path.read_bytes()):
            raise SystemExit(f"Похоже на токен в {path}. Сборка остановлена.")
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo.from_file(path, arcname=str(Path("shopbot") / path.relative_to(SOURCE)))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.date_time = (2026, 11, 28, 12, 0, 0)  # одинаковый архив при одинаковых файлах
            info.external_attr = (0o644 if not path.name.endswith(".sh") else 0o755) << 16
            archive.writestr(info, path.read_bytes())
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--version", required=True)
    target = build(parser.parse_args().version)
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    print(f"{target.name}: {len(names)} файлов, {target.stat().st_size // 1024} КБ")
    bad = [n for n in names if n.endswith(EXCLUDE_SUFFIXES) or "/.venv/" in n or n.endswith("/.env")]
    if bad:
        raise SystemExit(f"В архив попало лишнее: {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
