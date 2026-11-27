"""Сетевой слой BorzMarket.

Ozon проверяет отпечаток TLS. Обычный httpx получает страницу «Antibot Challenge»
(HTTP 403) или цикл перенаправлений 307, хотя тот же адрес открывается в браузере.
Поэтому для площадок бот использует curl_cffi: он повторяет рукопожатие Chrome.
Если библиотека не установлена или не запускается на устройстве, всё продолжает
работать на httpx, как в прежних версиях.

Ответ curl_cffi упаковывается в httpx.Response. Остальной код бота этого не замечает.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

import httpx

LOG = logging.getLogger("shopbot.net")

try:  # необязательная зависимость
    from curl_cffi import __version__ as CURL_VERSION
    from curl_cffi import requests as curl_requests
except Exception:  # нет колёс под платформу, нет библиотеки и т. п.
    curl_requests, CURL_VERSION = None, ""

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_curl_broken = False


def impersonation_enabled() -> bool:
    """True, если запросы к площадкам пойдут с отпечатком Chrome."""
    return curl_requests is not None and not _curl_broken and os.getenv("HTTP_IMPERSONATE", "1") != "0"


def describe() -> str:
    """Строка для /diag: чем бот ходит на площадки."""
    if curl_requests is None:
        return "Отпечаток Chrome · не установлен (pip install curl_cffi). Ozon может отвечать HTTP 403."
    if _curl_broken:
        return "Отпечаток Chrome · библиотека не запустилась на этом устройстве, работает httpx."
    if os.getenv("HTTP_IMPERSONATE", "1") == "0":
        return "Отпечаток Chrome · выключен в .env (HTTP_IMPERSONATE=0)."
    return f"Отпечаток Chrome · curl_cffi {CURL_VERSION} · {os.getenv('HTTP_IMPERSONATE_TARGET', 'chrome')}"


def _is_timeout(error: BaseException) -> bool:
    return getattr(error, "code", None) == 28 or "timed out" in str(error).lower() or isinstance(error, asyncio.TimeoutError)


def _library_failed(error: BaseException) -> bool:
    """Сбой самой библиотеки (не нашлась libcurl и т. п.), а не ошибка сети или ответа."""
    if isinstance(error, ImportError):
        return True
    curl_error = getattr(curl_requests, "exceptions", None)
    network = tuple(filter(None, (getattr(curl_error, "RequestException", None),)))
    try:
        from curl_cffi.curl import CurlError
        network += (CurlError,)
    except Exception:
        pass
    return isinstance(error, OSError) and not isinstance(error, network + (ConnectionError, TimeoutError))


def _to_httpx(method: str, url: str, reply) -> httpx.Response:
    skipped = {"content-encoding", "content-length", "transfer-encoding"}
    headers = [(key, value) for key, value in reply.headers.items() if key.lower() not in skipped]
    return httpx.Response(
        reply.status_code, headers=headers, content=reply.content, request=httpx.Request(method, url),
    )


class CurlClient:
    """Подмножество httpx.AsyncClient, которое использует бот: get, post, async with."""

    def __init__(self, *, headers: dict[str, str] | None = None, timeout: float = 10.0, proxy: str | None = None):
        options = {
            "impersonate": os.getenv("HTTP_IMPERSONATE_TARGET", "chrome"),
            "timeout": timeout,
            "allow_redirects": False,
        }
        if proxy:
            options["proxy"] = proxy
        try:
            self._session = curl_requests.AsyncSession(**options)
        except TypeError:  # старые версии принимали proxies=
            proxy_value = options.pop("proxy", None)
            if proxy_value:
                options["proxies"] = {"http": proxy_value, "https": proxy_value}
            self._session = curl_requests.AsyncSession(**options)
        # User-Agent задаёт сам отпечаток. Чужой UA с рукопожатием Chrome выдаёт бота.
        self._headers = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
        self._timeout = timeout
        self._proxy = proxy
        self._fallback: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "CurlClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        try:
            await self._session.close()
        except Exception:  # закрытие не должно ронять разбор
            pass
        if self._fallback is not None:
            await self._fallback.aclose()

    async def get(self, url, *, params=None, headers=None, follow_redirects: bool = False) -> httpx.Response:
        return await self._send("GET", url, params=params, headers=headers)

    async def post(self, url, *, json=None, headers=None) -> httpx.Response:
        return await self._send("POST", url, json=json, headers=headers)

    async def _send(self, method: str, url, *, headers=None, **options) -> httpx.Response:
        global _curl_broken
        merged = {**self._headers, **{k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}}
        text_url = str(url)
        if self._fallback is None:
            try:
                reply = await self._session.request(method, text_url, headers=merged, **options)
                return _to_httpx(method, text_url, reply)
            except Exception as error:
                if not _library_failed(error):
                    if _is_timeout(error):
                        raise httpx.ReadTimeout(str(error)) from error
                    raise httpx.TransportError(f"{type(error).__name__}: {error}") from error
                _curl_broken = True
                LOG.warning("curl_cffi не запустился (%s). Дальше работает httpx.", type(error).__name__)
                self._fallback = httpx.AsyncClient(
                    headers={"User-Agent": BROWSER_UA}, timeout=self._timeout, proxy=self._proxy or None,
                )
        return await self._fallback.request(method, text_url, headers=merged, follow_redirects=False, **options)


def make_client(*, headers: dict[str, str] | None = None, timeout: float = 6.0,
                proxy: str | None = None, impersonate: bool = False):
    """Клиент для площадки. impersonate=True включает отпечаток Chrome, если он доступен."""
    if impersonate and impersonation_enabled():
        try:
            return CurlClient(headers=headers, timeout=timeout, proxy=proxy)
        except Exception as error:
            LOG.warning("Не удалось создать клиент curl_cffi (%s). Использую httpx.", type(error).__name__)
    plain = {"User-Agent": BROWSER_UA, **(headers or {})}
    return httpx.AsyncClient(headers=plain, timeout=timeout, proxy=proxy or None)


class TTLCache:
    """Небольшой кэш с временем жизни. Хранит только то, что положили явно."""

    def __init__(self, ttl: float, limit: int = 256):
        self.ttl, self.limit = ttl, limit
        self._data: OrderedDict = OrderedDict()

    def get(self, key):
        row = self._data.get(key)
        if row is None:
            return None
        stamp, value = row
        if time.monotonic() - stamp > self.ttl:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key, value) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.limit:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


_inflight: dict = {}


async def once(key, factory: Callable[[], Awaitable]):
    """Одинаковые параллельные запросы ждут один общий результат."""
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(factory())
        _inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _inflight.pop(k, None))
    return await asyncio.shield(task)
