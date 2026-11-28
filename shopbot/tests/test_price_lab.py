"""price_lab.py на фикстурах: режимы на одном ответе, проба WBCON и сравнение с эталоном."""
import asyncio
import csv
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import price_lab
import state
import wbcon
from fixtures import OZ_SKU, ozon_page, jpeg
from isolation import CleanState


class PriceLabTests(CleanState, unittest.IsolatedAsyncioTestCase):
    async def test_modes_share_one_answer_and_reference_gives_deviation(self):
        calls = []

        async def send(client, request, *args, **kwargs):
            calls.append(str(request.url))
            if request.url.host == "www.ozon.ru":
                return httpx.Response(200, text=ozon_page(), request=request)
            if request.url.host.endswith("ozone.ru"):
                return httpx.Response(200, content=jpeg(), request=request)
            if request.url.host == "oz.wbcon.su":
                return httpx.Response(200, json={"article": OZ_SKU, "name": "Рюкзак", "price": 3083,
                                                 "imageUrl": "https://ir.ozone.ru/a.jpg"}, request=request)
            return httpx.Response(404, request=request)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(httpx.AsyncClient, "send", send), \
                patch.dict(os.environ, {"HTTP_IMPERSONATE": "0", "OZON_PRICE_FALLBACK": "1"}):
            reference = Path(folder) / "ref.csv"
            with reference.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=price_lab.REFERENCE_FIELDS)
                writer.writeheader()
                writer.writerow(dict(market="ozon", sku=OZ_SKU, region="Москва", payment="regular",
                                     reference_rub="2972", reference_at=""))
            out = Path(folder) / "lab.csv"
            args = SimpleNamespace(products=[f"ozon:{OZ_SKU}"], items=None, modes="own,combo,legacy", wbcon=True,
                                   reference=reference, self_reference=False, env="fixture", out=out, record=None,
                                   replay=None, pause=0)
            original = httpx.AsyncClient.send
            try:
                self.assertEqual(await price_lab.main_async(args), 0)
            finally:
                httpx.AsyncClient.send = original
            rows = list(csv.DictReader(out.open(encoding="utf-8")))
        pages = [c for c in calls if "www.ozon.ru/product" in c]
        self.assertEqual(len(pages), 1)  # combo и legacy воспроизвели ответ own
        own = [r for r in rows if r["mode"] == "own" and r["payment"] == "regular"][0]
        self.assertEqual((own["amount_rub"], own["seller"] == "", own["comparable"]), ("2969", True, "да"))
        self.assertEqual(own["deviation_rub"], "-3")
        combo = [r for r in rows if r["mode"] == "combo" and r["payment"] == "regular"][0]
        self.assertEqual(combo["replayed"], "1")
        probe = [r for r in rows if r["mode"] == "wbcon"][0]
        self.assertEqual((probe["amount_rub"], probe["source"], probe["origin"]), ("3083", "wbcon", "aggregator"))
        self.assertEqual(probe["deviation_pct"], "3.73")
        self.assertTrue(any(r["payment"] == "С банками" for r in rows))

    async def test_replay_of_compressed_answer_keeps_text(self):
        """Ответ пришёл сжатым: запись хранит распакованное тело без заголовка сжатия."""
        import gzip
        body = gzip.compress("<html>цена 2972</html>".encode())

        def reply(request):
            return httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Length": str(len(body))},
                                  content=body)
        cassette = price_lab.Cassette()
        original = httpx.AsyncClient.send
        try:
            price_lab.install_cassette(cassette)
            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                live = await client.get("https://www.ozon.ru/product/1/")
                again = await client.get("https://www.ozon.ru/product/1/")
        finally:
            httpx.AsyncClient.send = original
        self.assertEqual((live.text, again.text), ("<html>цена 2972</html>",) * 2)
        self.assertEqual((cassette.live, cassette.replayed), (1, 1))
        self.assertNotIn("content-encoding", {k.lower() for k in next(iter(cassette.items.values()))["headers"]})

    def test_status_comes_from_price_source_not_short_link(self):
        from diagnostics import Event
        events = [Event("ozon.link", "error", http=403), Event("ozon.page", "ok", http=200)]
        self.assertEqual(price_lab.main_status(events, "ozon_page"), "200")
        events.append(Event("wbcon.ozon", "limit", http=429))
        self.assertEqual(price_lab.main_status(events, "wbcon"), "429")

    def test_template_has_separate_columns_for_regular_and_card_price(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t.csv"
            with patch("sys.argv", ["price_lab.py", "--template", str(path)]):
                self.assertEqual(price_lab.main(), 0)
            rows = list(csv.DictReader(path.open(encoding="utf-8")))
        self.assertEqual({r["payment"] for r in rows}, {"regular", "с картой банка"})


if __name__ == "__main__":
    unittest.main()
