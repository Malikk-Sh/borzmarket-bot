"""Эксперимент с источниками цен BorzMarket. Без Telegram и без рабочей базы.

    .venv/bin/python price_lab.py ozon:1526882900 wb:866768707 --env vps --wbcon --out lab.csv
    .venv/bin/python price_lab.py --items items.txt --reference reference.csv --env vps --out lab.csv
    .venv/bin/python price_lab.py --template reference.csv

Режимы на одинаковых входах (--modes, по умолчанию own,combo):
  own    — собственный путь: только площадка, WBCON и BHAPI выключены, память пустая;
  combo  — площадка, затем резерв с пометкой (как PRICE_ENGINE=combo в боте);
  legacy — порядок v10.0 внутри новой версии.
--wbcon добавляет прямую пробу WBCON по каждому артикулу, чтобы увидеть отклонение именно сервиса.
Ответ, уже полученный в этом запуске, следующим режимам воспроизводится из записи (колонка replayed):
так режимы сравниваются на одном ответе и не тратят квоту. Пауза WBCON соблюдается (2 запроса в минуту).
Эталон (--reference) — значения из приложения покупателя; шаблон даёт --template.
Платные вызовы выключены: BHAPI в эксперименте не используется.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import json
import os
import platform
import statistics
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

MOSCOW = timezone(timedelta(hours=3))
FIELDS = ["run_id", "environment", "mode", "market", "sku", "variant", "seller", "region", "payment", "source",
          "origin", "observed_at", "fetched_at", "amount_rub", "reference_rub", "reference_at", "comparable",
          "deviation_rub", "deviation_pct", "reason", "http_status", "latency_ms", "cache_hit", "replayed",
          "title", "photo_url", "photo_ok", "wbcon_calls", "wbcon_requests"]
REFERENCE_FIELDS = ["market", "sku", "region", "seller", "variant", "payment", "reference_rub", "reference_at",
                    "observed_by", "comment"]
COMPARE_WINDOW = 300  # эталон и наблюдение не дальше пяти минут друг от друга


# --- запись и воспроизведение ответов -----------------------------------------------------
class Cassette:
    """Первый ответ на запрос берётся из сети, повторный в этом запуске — из записи."""

    def __init__(self, path: Path | None = None, replay_only: bool = False):
        self.path = path
        self.replay_only = replay_only
        self.items: dict[str, dict] = {}
        self.live = self.replayed = 0
        self.last_replayed = False
        if path and path.exists():
            self.items = json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def key(method: str, url: str, body: bytes = b"") -> str:
        return f"{method} {url} {hashlib.sha1(body or b'').hexdigest()[:12]}"

    def get(self, key, request):
        stored = self.items.get(key)
        if stored is None:
            return None
        self.replayed += 1
        self.last_replayed = True
        return httpx.Response(stored["status"], headers=stored["headers"],
                              content=base64.b64decode(stored["body"]), request=request)

    def put(self, key, response: httpx.Response) -> None:
        self.live += 1
        # Тело уже распаковано: заголовки сжатия и длины при воспроизведении сломали бы разбор.
        skipped = {"content-encoding", "content-length", "transfer-encoding"}
        headers = {k: v for k, v in response.headers.items() if k.lower() not in skipped}
        self.items[key] = {"status": response.status_code, "headers": headers,
                           "body": base64.b64encode(response.content).decode()}

    def save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.items), encoding="utf-8")


def install_cassette(cassette: Cassette):
    """Перехватывает httpx и клиента с отпечатком Chrome, ничего не меняя в коде бота."""
    import net
    original_send = httpx.AsyncClient.send

    async def send(client, request, *args, **kwargs):
        key = Cassette.key(request.method, str(request.url), request.content)
        if (hit := cassette.get(key, request)) is not None:
            return hit
        if cassette.replay_only:
            return httpx.Response(599, text="нет записи ответа", request=request)
        response = await original_send(client, request, *args, **kwargs)
        await response.aread()
        cassette.put(key, response)
        return response
    httpx.AsyncClient.send = send

    original_curl = net.CurlClient._send

    async def curl_send(client, method, url, *, headers=None, **options):
        params = options.get("params")
        full = str(httpx.URL(str(url), params=params)) if params else str(url)
        body = json.dumps(options.get("json"), sort_keys=True).encode() if options.get("json") is not None else b""
        key = Cassette.key(method, full, body)
        request = httpx.Request(method, full)
        if (hit := cassette.get(key, request)) is not None:
            return hit
        if cassette.replay_only:
            return httpx.Response(599, text="нет записи ответа", request=request)
        response = await original_curl(client, method, url, headers=headers, **options)
        cassette.put(key, response)
        return response
    net.CurlClient._send = curl_send


# --- один товар в одном режиме ---------------------------------------------------------------
def rub(value) -> str:
    """Сумма без хвостовых нулей: 2969, 2969.9."""
    text = format(Decimal(value).quantize(Decimal("0.01")), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def stamp(value) -> str:
    return datetime.fromtimestamp(value, MOSCOW).strftime("%Y-%m-%d %H:%M:%S") if value else ""


async def run_mode(value: str, mode: str, cassette: Cassette) -> tuple[object, list, bool]:
    import prices
    import state
    from diagnostics import capture_events
    os.environ["PRICE_ENGINE"] = "own" if mode == "own" else mode
    os.environ["OZON_PRICE_ENGINE"] = os.environ["WB_PRICE_ENGINE"] = os.environ["PRICE_ENGINE"]
    state.reset()  # без памяти предыдущего прогона
    prices._RESULT_CACHE.clear()
    replayed_before = cassette.replayed
    with capture_events() as events:
        product = await prices.fetch_product(value, direct_only=(mode == "own"), allow_paid=False, fresh=True)
        photo = None
        if product.image_urls:
            from photos import download_jpeg
            try:
                photo, _ = await asyncio.wait_for(download_jpeg(product), 18)
            except asyncio.TimeoutError:
                photo = None
        product = prices.finish(product, photo is not None)
        product.photo_source = "market" if photo is not None else ""
    return product, list(product.trace or events), cassette.replayed > replayed_before


async def wbcon_probe(value: str, cassette: Cassette, waits: list, resolved: str = "") -> tuple[object | None, str, bool]:
    """Прямой запрос к WBCON. При лимите ждём разрешённое окно, без циклов повторов."""
    import prices
    import wbcon
    from card import ParseError, Product
    from diagnostics import capture_events
    from links import LinkError
    try:
        market, url = prices.market_url(resolved or value)  # короткая ссылка → адрес с артикулом из режима own
    except LinkError as error:
        return None, str(error), False
    product = Product(market, "", url)
    if not wbcon.available(product):
        return None, "WBCON не принимает размер или нестандартный регион", False
    replayed_before = cassette.replayed
    for attempt in range(2):
        try:
            with capture_events() as events:
                quote = await wbcon.lookup(product)
            quote.trace = list(events)
            return quote, "", cassette.replayed > replayed_before
        except ParseError as error:
            pause = wbcon.paused(market)
            window = wbcon._recent[market]
            if "Лимит WBCON. Попробуйте через" in str(error) and attempt == 0:
                wait = max(1.0, 61 - (time.monotonic() - window[0])) if window else 30
                waits.append(wait)
                await asyncio.sleep(wait)
                continue
            if pause:
                return None, f"WBCON на паузе до {stamp(pause[0])}", False
            return None, str(error), False
    return None, "лимит WBCON", False


def main_status(events, source: str) -> str:
    """HTTP-код источника цены, а не первого шага (короткой ссылки)."""
    order = ("ozon.page", "wb.detail", "wbcon.", "bhapi.")
    if source.startswith("wbcon"):
        order = ("wbcon.",)
    for prefix in order:
        for event in reversed(events):
            if event.source.startswith(prefix) and event.http:
                return str(event.http)
    if source.startswith("wbcon"):
        return ""  # ответ WBCON из кэша процесса: своего HTTP-кода у этой строки нет
    return next((str(e.http) for e in reversed(events) if e.http), "")


def rows_for(run_id, env, mode, product, events, replayed, value, wbcon_calls=0, reason_override=""):
    import report
    from storefront import sku_in_url
    base = dict(run_id=run_id, environment=env, mode=mode, market=product.marketplace if product else "",
                sku=sku_in_url(product.url) if product else value, variant=getattr(product, "variant", ""),
                seller=getattr(product, "seller", ""), region=getattr(product, "region", "") or getattr(product, "wb_dest", ""),
                source=getattr(product, "price_source", ""), origin=getattr(product, "price_origin", "") or "network",
                observed_at=stamp(getattr(product, "price_at", None)),
                fetched_at=stamp(getattr(product, "price_fetched_at", None) or time.time()),
                http_status=main_status(events, getattr(product, "price_source", "") if product else ""),
                latency_ms=sum(e.ms or 0 for e in events), cache_hit=int(any(e.outcome == "cached" for e in events)),
                replayed=int(replayed), title=(getattr(product, "title", "") or "")[:80],
                photo_url=int(bool(getattr(product, "image_urls", None))),
                photo_ok=int(getattr(product, "photo_source", "") == "market"), wbcon_calls=wbcon_calls,
                # обращения к WBCON по событиям этого товара (из сети или из записи), без ответов из кэша
                wbcon_requests=sum(1 for e in events if e.source.startswith("wbcon.") and e.http))
    if product is None:
        return [dict(base, payment="regular", amount_rub="", reason=reason_override or "нет ответа")]
    if product.price is None:
        reason = reason_override or (product.gaps or {}).get("price") or product.import_note or "цены нет"
        if product.price_unavailable:
            reason = "нет в наличии"
        return [dict(base, payment="regular", amount_rub="", reason=reason[:200])]
    out = []
    conditional_only = product.conditional_price is not None and product.conditional_price == product.price \
        and bool(product.price_note)
    out.append(dict(base, payment=product.price_note if conditional_only else "regular", amount_rub=rub(product.price),
                    reason=""))
    if product.conditional_price is not None and not conditional_only:
        out.append(dict(base, payment=product.conditional_note or "условная",
                        amount_rub=rub(product.conditional_price), reason=""))
    return out


# --- сравнение с эталоном ------------------------------------------------------------------------
def payment_kind(text: str) -> str:
    folded = (text or "").casefold()
    if folded in ("", "regular", "обычная", "без карты", "с другими банками"):
        return "regular"
    if any(word in folded for word in ("банк", "карт")):
        return "card"
    if "кошел" in folded:
        return "wallet"
    return folded


def parse_moment(text: str) -> float | None:
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text.strip(), pattern).replace(tzinfo=MOSCOW).timestamp()
        except (ValueError, AttributeError):
            continue
    return None


def compare(rows: list[dict], references: list[dict], *, self_reference: bool = False) -> None:
    for row in rows:
        if not row["amount_rub"]:
            row["comparable"] = "нет цены"
            continue
        if self_reference and row["origin"] != "aggregator":
            # Цена витрины против той же витрины совпадает по построению: в точность не идёт.
            row["comparable"] = "эталон из того же ответа"
            continue
        matches = [r for r in references if r["market"] == row["market"] and r["sku"] == row["sku"]
                   and payment_kind(r["payment"]) == payment_kind(row["payment"])]
        if not matches:
            row["comparable"] = "нет эталона"
            continue
        ref = matches[0]
        row["reference_rub"], row["reference_at"] = ref["reference_rub"], ref["reference_at"]
        problems = []
        for name in ("region", "seller", "variant"):
            theirs, ours = (ref.get(name) or "").casefold(), (row.get(name) or "").casefold()
            if theirs and ours and theirs not in ours and ours not in theirs:
                problems.append(f"другой {name}")
        ours_at, ref_at = parse_moment(row["fetched_at"]), parse_moment(ref["reference_at"] or "")
        if ours_at and ref_at and abs(ours_at - ref_at) > COMPARE_WINDOW:
            problems.append(f"разница во времени {int(abs(ours_at - ref_at) // 60)} мин")
        try:
            reference = Decimal(ref["reference_rub"])
            amount = Decimal(row["amount_rub"])
        except Exception:
            row["comparable"] = "эталон без суммы"
            continue
        if reference <= 0:
            row["comparable"] = "эталон без суммы"
            continue
        row["deviation_rub"] = rub(amount - reference)
        row["deviation_pct"] = f"{(amount - reference) * 100 / reference:.2f}"
        row["comparable"] = "да" if not problems else "нет: " + ", ".join(problems)


def summarize(rows: list[dict], items: int) -> list[str]:
    lines = []
    for market in ("ozon", "wb"):
        for mode in sorted({r["mode"] for r in rows if r["market"] == market}):
            group = [r for r in rows if r["market"] == market and r["mode"] == mode]
            skus = {r["sku"] for r in group}
            priced = {r["sku"] for r in group if r["amount_rub"] and r["payment"] == "regular"}
            any_price = {r["sku"] for r in group if r["amount_rub"]}
            titled = {r["sku"] for r in group if r["title"]}
            photos = {r["sku"] for r in group if str(r.get("photo_ok")) == "1"}
            fresh_own = {r["sku"] for r in group if r["amount_rub"] and r["origin"] == "network"}
            reserve = {r["sku"] for r in group if r["amount_rub"] and r["origin"] == "aggregator"}
            unique = {(r["sku"], payment_kind(r["payment"])): r for r in group if r.get("comparable") == "да"}
            pairs = list(unique.values())  # один товар по двум ссылкам считается одной парой
            devs = [abs(float(r["deviation_pct"])) for r in pairs]
            text = (f"{market} · {mode}: товаров {len(skus)}, обычная цена {len(priced)}, любая цена {len(any_price)}, "
                    f"название {len(titled)}, фото скачано {len(photos)}, свежие собственные {len(fresh_own)}, "
                    f"резерв {len(reserve)}, "
                    f"сопоставимых пар {len(pairs)}")
            if devs:
                exact = sum(1 for r in pairs if Decimal(r["deviation_rub"]) == 0)
                text += (f"; точно {exact}, ≤1% {sum(d <= 1 for d in devs)}, ≤5% {sum(d <= 5 for d in devs)}, "
                         f"≤20% {sum(d <= 20 for d in devs)}, медиана {statistics.median(devs):.2f}%, "
                         f"максимум {max(devs):.2f}%, выше эталона {sum(float(r['deviation_pct']) > 0 for r in pairs)}, "
                         f"ниже {sum(float(r['deviation_pct']) < 0 for r in pairs)}")
            lines.append(text)
    return lines


async def main_async(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
    os.environ["BHAPI_OZON_TOKEN"] = os.environ["BHAPI_WB_TOKEN"] = ""  # платные вызовы выключены
    import state
    state.reset()
    import wbcon
    values = list(args.products)
    if args.items:
        values += [line.strip() for line in args.items.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.startswith("#")]
    if not values:
        print("Укажите товары: ozon:АРТИКУЛ, wb:АРТИКУЛ, ссылки или файл --items.")
        return 2
    cassette = Cassette(args.replay or args.record, replay_only=bool(args.replay))
    install_cassette(cassette)
    run_id = uuid.uuid4().hex[:8]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    rows, waits = [], []
    wbcon_live_before = 0
    started = time.perf_counter()
    for number, value in enumerate(values, 1):
        print(f"[{number}/{len(values)}] {value}", flush=True)
        resolved = ""
        for mode in modes:
            calls_before = sum(1 for k in cassette.items if "wbcon.su" in k)
            try:
                product, events, replayed = await run_mode(value, mode, cassette)
            except Exception as error:  # неверная ссылка, сбой разбора
                rows += rows_for(run_id, args.env, mode, None, [], False, value, reason_override=str(error)[:160])
                continue
            resolved = resolved or product.url
            calls = sum(1 for k in cassette.items if "wbcon.su" in k) - calls_before
            rows += rows_for(run_id, args.env, mode, product, events, replayed, value, wbcon_calls=calls)
        if args.wbcon:
            before = sum(1 for k in cassette.items if "wbcon.su" in k)
            quote, reason, replayed = await wbcon_probe(value, cassette, waits, resolved)
            calls = sum(1 for k in cassette.items if "wbcon.su" in k) - before
            if quote is not None:
                quote = replace(quote, price_origin="aggregator")
            rows += rows_for(run_id, args.env, "wbcon", quote, list(getattr(quote, "trace", None) or []), replayed,
                             value, wbcon_calls=calls, reason_override=reason)
        if number < len(values):
            await asyncio.sleep(args.pause)
    references = []
    if args.reference:
        with args.reference.open(encoding="utf-8-sig") as stream:
            references = [r for r in csv.DictReader(stream) if (r.get("reference_rub") or "").strip()]
    elif args.self_reference:
        # Эталон «витрина площадки в этом же запуске»: пригоден только для оценки резерва (WBCON).
        for r in rows:
            if r["mode"] == "own" and r["amount_rub"]:
                references.append(dict(market=r["market"], sku=r["sku"], region=r["region"], seller=r["seller"],
                                       variant=r["variant"], payment=r["payment"], reference_rub=r["amount_rub"],
                                       reference_at=r["fetched_at"], observed_by="витрина в этом запуске"))
    compare(rows, references, self_reference=bool(args.self_reference and not args.reference))
    cassette.save()
    out = args.out or Path(f"price_lab_{run_id}.csv")
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    took = time.perf_counter() - started
    print(f"\nСреда {args.env} · {platform.machine()} · Python {platform.python_version()} · запуск {run_id} · {took:.1f} с")
    print(f"Сетевых ответов из сети {cassette.live}, из записи {cassette.replayed}, "
          f"ожиданий окна WBCON {len(waits)}")
    for line in summarize(rows, len(values)):
        print(line)
    print(f"Файл: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("products", nargs="*", help="ozon:АРТИКУЛ, wb:АРТИКУЛ или ссылки")
    parser.add_argument("--items", type=Path, help="файл со списком товаров, по одному в строке")
    parser.add_argument("--modes", default="own,combo", help="own,combo,legacy через запятую")
    parser.add_argument("--wbcon", action="store_true", help="отдельная проба WBCON по каждому товару")
    parser.add_argument("--reference", type=Path, help="CSV эталона из приложения (шаблон: --template)")
    parser.add_argument("--self-reference", action="store_true",
                        help="эталон = цена витрины в режиме own этого же запуска (оценка WBCON)")
    parser.add_argument("--env", default="vps", help="метка среды: vps, cloud, fixture")
    parser.add_argument("--out", type=Path, help="куда записать CSV")
    parser.add_argument("--record", type=Path, help="сохранить ответы сети в файл")
    parser.add_argument("--replay", type=Path, help="воспроизвести ответы из файла вместо сети")
    parser.add_argument("--pause", type=float, default=3.0, help="пауза между товарами, секунды")
    parser.add_argument("--template", type=Path, help="записать пустой шаблон эталона и выйти")
    args = parser.parse_args()
    if args.template:
        with args.template.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=REFERENCE_FIELDS)
            writer.writeheader()
            writer.writerow(dict(market="ozon", sku="1526882900", region="Москва", seller="", variant="",
                                 payment="regular", reference_rub="", reference_at="2026-11-28 14:05",
                                 observed_by="приложение Ozon, без входа", comment="обычная цена"))
            writer.writerow(dict(market="ozon", sku="1526882900", region="Москва", seller="", variant="",
                                 payment="с картой банка", reference_rub="", reference_at="2026-11-28 14:05",
                                 observed_by="приложение Ozon", comment="зелёная цена, если видна"))
            writer.writerow(dict(market="wb", sku="866768707", region="Москва", seller="", variant="",
                                 payment="regular", reference_rub="", reference_at="2026-11-28 14:06",
                                 observed_by="приложение WB", comment="цена без WB Кошелька"))
        print(f"Шаблон эталона: {args.template}")
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
