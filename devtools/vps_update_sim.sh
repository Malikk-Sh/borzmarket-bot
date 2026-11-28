#!/usr/bin/env bash
# Имитация рабочего VPS: v10.0 с прокси и данными в /root/borzmarket-proxy/shopbot, служба systemd —
# заглушка systemctl/journalctl. Блок обновления берётся из README как есть, меняются только корни путей.
# Использование: bash devtools/vps_update_sim.sh РЕЖИМ ПАПКА ПАПКА_SHOPBOT_V10_0
#   РЕЖИМ: ok | check_fails | start_fails. Нужен собранный borzmarket_bot_v10_1.zip в корне репозитория.
# Ничего вне указанной папки не меняет: systemctl и journalctl — заглушки, пути /root и /etc подменяются.
set -u
MODE=$1
SCR=$(realpath -m "${2:?папка для имитации}"); mkdir -p "$SCR"
REPO=$(cd "$(dirname "$0")/.." && pwd)
INSTALLED=$(realpath "${3:?папка shopbot установленной версии}")
BASE=$SCR/vpssim_$MODE; rm -rf "$BASE"; mkdir -p "$BASE/root" "$BASE/etc/systemd/system" "$BASE/bin"
OLD="$BASE/root/borzmarket-proxy/shopbot"
mkdir -p "$BASE/root/borzmarket-proxy"; cp -r "$INSTALLED" "$OLD"
python3 -m venv "$OLD/.venv" && "$OLD/.venv/bin/python" -m pip install -q -r "$OLD/requirements.txt" >/dev/null
"$OLD/.venv/bin/python" "$REPO/devtools/make_v91_install.py" "$OLD" >/dev/null
cp "$REPO/borzmarket_bot_v10_1.zip" "$BASE/root/"
LOG="$BASE/systemctl.log"
cat > "$BASE/bin/systemctl" <<SH
#!/usr/bin/env bash
echo "systemctl \$*" >> "$LOG"
DROP="$BASE/etc/systemd/system/borzmarket.service.d/v10-1.conf"
dir() { if [ -f "\$DROP" ]; then sed -n 's/^WorkingDirectory=//p' "\$DROP"; else echo "$OLD"; fi; }
case "\$1" in
  start) d=\$(dir); echo "start in \$d" >> "$LOG"
         if [ "$MODE" = start_fails ] && [ "\$d" != "$OLD" ]; then echo failed > "$BASE/state"; exit 0; fi
         (cd "\$d" && PRICE_MONITOR=0 .venv/bin/python "$REPO/devtools/smoke_start.py" . >> "$LOG" 2>&1) && echo active > "$BASE/state" || echo failed > "$BASE/state";;
  stop) echo inactive > "$BASE/state";;
  is-active) s=\$(cat "$BASE/state" 2>/dev/null || echo active); echo \$s; [ "\$s" = active ];;
  is-enabled) echo enabled;;
  show) echo "WorkingDirectory=\$(dir)";;
  daemon-reload) :;;
esac
SH
printf '#!/usr/bin/env bash\necho "-- journal (имитация): $*"\ntail -3 "%s"\n' "$LOG" > "$BASE/bin/journalctl"
chmod +x "$BASE/bin/systemctl" "$BASE/bin/journalctl"
echo active > "$BASE/state"
# Блок из README: между ```bash и ``` после «вставьте блок целиком»
BLOCK=$(awk '/^2\. \*\*VPS\*\*, после `ssh`/{f=1} f&&/^```bash$/{g=1;next} g&&/^```$/{exit} g{print}' "$REPO/shopbot/README.md")
BLOCK=$(printf '%s\n' "$BLOCK" | sed -e "s|/root/|$BASE/root/|g" -e "s|/etc/systemd/system|$BASE/etc/systemd/system|g")
if [ "$MODE" = check_fails ]; then
  BLOCK=$(printf '%s\n' "$BLOCK" | sed -e 's|\.venv/bin/python bot\.py --check-telegram|false|')
else
  BLOCK=$(printf '%s\n' "$BLOCK" | sed -e 's|\.venv/bin/python bot\.py --check-telegram|echo "(имитация getMe: настоящий вызов проверен тестами)"|')
fi
# Цена без Telegram в имитации идёт в сеть: оставляем, это один запрос страницы Ozon.
PATH="$BASE/bin:$PATH" bash -c "$BLOCK"
echo "== код выхода блока: $?"
echo "== вызовы systemctl:"; grep '^systemctl\|^start in' "$LOG"
echo "== drop-in:"; cat "$BASE/etc/systemd/system/borzmarket.service.d/v10-1.conf" 2>/dev/null || echo "(нет)"
NEW="$BASE/root/borzmarket-v10-1/shopbot"
echo "== .env права: $(stat -c %a "$NEW/.env" 2>/dev/null)  совпадает: $(cmp -s "$OLD/.env" "$NEW/.env" && echo да || echo нет)"
echo "== копия базы: $(ls "$BASE/root/" | grep backup || echo нет)"
ls "$NEW/borzmarket.sqlite3" 2>/dev/null && "$NEW/.venv/bin/python" - "$NEW" <<'PY'
import sys; sys.path.insert(0, sys.argv[1])
from drafts import DraftStore
s = DraftStore(sys.argv[1] + "/borzmarket.sqlite3")
print("новая база: состояния", [tuple(r) for r in s.conn.execute("SELECT state, count(*) FROM drafts GROUP BY state")],
      "история", s.conn.execute("SELECT count(*) FROM published").fetchone()[0])
PY
echo "== состояние службы: $(cat "$BASE/state")"
