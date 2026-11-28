#!/usr/bin/env bash
# Проверка релиза «как на телефоне» в чистой папке: старая установка v9.1 с данными,
# установка новой версии блоком из README, тесты, CLI, рендер, запуск без Telegram, откат и повторная миграция.
#   bash devtools/verify_release.sh borzmarket_bot_v9_1.zip borzmarket_bot_v10_1.zip /путь/к/пустой/папке
# Имена папок берутся из имени нового архива: borzmarket_bot_v10_1.zip → ~/borzmarket-v10-1.
# Указанная папка не очищается и не удаляется: проверка идёт в новом подкаталоге verify-XXXXXX внутри неё.
set -euo pipefail
[ $# -eq 3 ] || { echo "Использование: verify_release.sh старый.zip новый.zip папка_для_проверок"; exit 2; }
OLD_ZIP=$(realpath "$1"); NEW_ZIP=$(realpath "$2")
for archive in "$OLD_ZIP" "$NEW_ZIP"; do
  unzip -tq "$archive" >/dev/null 2>&1 || { echo "Не ZIP-архив: $archive"; exit 2; }
done
BASE=$(realpath -m "$3"); mkdir -p "$BASE"
TAG=$(basename "$NEW_ZIP" .zip); TAG=${TAG#borzmarket_bot_v}   # 10_1
DIR="borzmarket-v${TAG//_/-}"                                    # borzmarket-v10-1
WORK=$(mktemp -d "$BASE/verify-XXXXXX")
echo "== Рабочий каталог проверки: $WORK"
DEV=$(cd "$(dirname "$0")" && pwd)
export HOME="$WORK/home"; mkdir -p "$HOME"
echo "== Платформа: $(uname -m), $(python3 --version), $(date -u +%FT%TZ)"

echo "== 1. Прежняя установка v9.1 с данными"
unzip -q "$OLD_ZIP" -d "$HOME/borzmarket-v9-1"
echo "v9.1 распакована в $HOME/borzmarket-v9-1/shopbot"
( cd "$HOME/borzmarket-v9-1/shopbot" && python3 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt \
  && .venv/bin/python "$DEV/make_v91_install.py" . )
cp "$NEW_ZIP" "$HOME/borzmarket_bot_v${TAG}.zip"

echo "== 2. Установка новой версии блоком из README (из папки прежней версии)"
cd "$HOME/borzmarket-v9-1/shopbot"
(
  set -e
  old_bot="$PWD"
  test -f "$old_bot/bot.py" && test -f "$old_bot/.env" || { echo "Перейдите в папку прежнего бота с bot.py и .env"; exit 1; }
  test -f "$old_bot/borzmarket.sqlite3" && python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$old_bot/borzmarket.sqlite3" "$HOME/borzmarket-backup-before-v${TAG//_/-}.sqlite3"
  unzip -q -n ~/borzmarket_bot_v${TAG}.zip -d ~/$DIR
  cd ~/$DIR/shopbot
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -r requirements.txt
  .venv/bin/python -m pip install -q -r requirements-ozon.txt || echo "curl_cffi не установился, бот работает без него"
  .venv/bin/python migrate.py "$old_bot"
  echo "$old_bot" > ~/borzmarket-previous-folder.txt
  echo "Готово. Резервная копия базы: $HOME/borzmarket-backup-before-v${TAG//_/-}.sqlite3"
)
NEW="$HOME/$DIR/shopbot"
cd "$NEW"
.venv/bin/python -m pip list 2>/dev/null | grep -iE "^(aiogram|httpx|pillow|curl.cffi|python-dotenv) " || true

echo "== 3. Тесты из распакованного архива"
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3

echo "== 4. CLI"
.venv/bin/python prices.py --help | head -3
.venv/bin/python -c "import bot, prices, collage, rollback, migrate, tg_preview; print('импорт модулей: ok')"

echo "== 5. Перенос: настройки, черновик с фото, очередь, история"
grep -E "^(CHANNEL_ID|AUTO_POST|WB_DEST|QUEUE_SLOTS)=" .env
.venv/bin/python - <<'EOF'
from drafts import DraftStore
store = DraftStore("borzmarket.sqlite3")
print("черновики/очередь/ближайший:", store.counts(1001, 1001)[:2])
rows = store.conn.execute("SELECT id, state FROM drafts ORDER BY state").fetchall()
print("записи:", [(r[1], bool(store.get(r[0]).items[0].photo)) for r in rows])
print("история:", [tuple(r) for r in store.conn.execute("SELECT status, post_url FROM published")])
print("наблюдения за ценой:", tuple(store.conn.execute("SELECT count(*) FROM price_watch").fetchone()))
store.close()
EOF

echo "== 6. Запуск bot.main() без Telegram"
PRICE_MONITOR=0 .venv/bin/python "$DEV/smoke_start.py" .

echo "== 7. Рендер примеров на этой машине"
.venv/bin/python tools/render_examples.py --out examples --repeat 1 > /dev/null && ls examples | grep -c jpg

echo "== 8. Новая версия публикует одну запись из очереди (имитация), затем откат"
.venv/bin/python - <<'EOF'
import time
from drafts import DraftStore
from history import Receipt
store = DraftStore("borzmarket.sqlite3")
queued = [store.get(r[0]) for r in store.conn.execute("SELECT id FROM drafts WHERE state='queued' ORDER BY due_at")]
first = queued[0]
assert store.claim(first, "@borzmarket_channel", int(time.time()))
store.finish(first, "sent", receipt=Receipt(-1001234567890, "@borzmarket_channel", 78,
                                            "https://t.me/borzmarket_channel/78", int(time.time())))
print("новая версия отправила:", first.id)
store.close()
EOF
.venv/bin/python rollback.py "$(cat ~/borzmarket-previous-folder.txt)"
.venv/bin/python rollback.py "$(cat ~/borzmarket-previous-folder.txt)" --apply | tail -2
OLD="$(cat ~/borzmarket-previous-folder.txt)"
"$OLD/.venv/bin/python" - "$OLD" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
from drafts import DraftStore
store = DraftStore(sys.argv[1] + "/borzmarket.sqlite3")
print("старая очередь после сверки:", [tuple(r) for r in store.conn.execute("SELECT state, count(*) FROM drafts GROUP BY state")])
print("старая история:", store.conn.execute("SELECT count(*) FROM published WHERE status='sent'").fetchone()[0], "постов")
store.close()
EOF

echo "== 9. Повторная миграция ничего не ломает"
.venv/bin/python migrate.py "$OLD"

echo "== 10. В архиве нет секретов и рабочих данных"
unzip -l "$NEW_ZIP" | grep -E "\.env$|sqlite3|\.session|/\.venv/|\.log$" && { echo "ЛИШНЕЕ В АРХИВЕ"; exit 1; } || echo "лишнего нет"
echo "== Проверка завершена"
