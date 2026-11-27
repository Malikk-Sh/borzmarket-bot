"""Перенос настроек и базы из предыдущей папки BorzMarket."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


def migrate(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"Папка с прошлой версией не найдена: {source}")
    old_env, new_env = source / ".env", target / ".env"
    if old_env.is_file() and not new_env.exists():
        with new_env.open("xb") as stream:
            stream.write(old_env.read_bytes())
        new_env.chmod(0o600)
        print("Настройки перенесены.")

    old_db, new_db = source / "borzmarket.sqlite3", target / "borzmarket.sqlite3"
    if old_db.is_file() and not new_db.exists():
        with TemporaryDirectory(dir=target) as work:
            copy = Path(work) / new_db.name
            with closing(sqlite3.connect(old_db.resolve().as_uri() + "?mode=ro", uri=True)) as origin:
                with closing(sqlite3.connect(copy)) as backup:
                    origin.backup(backup)
            copy.replace(new_db)
        print("Черновики и очередь перенесены.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="папка shopbot предыдущей версии")
    migrate(parser.parse_args().source.expanduser(), Path(__file__).resolve().parent)
