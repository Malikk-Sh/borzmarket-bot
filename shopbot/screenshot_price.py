"""Локальный OCR для ручной сверки обычной цены на скриншоте."""
import asyncio
import os
import re
import shutil

from card import ParseError, parse_price

MONEY = re.compile(r"(?<![\d.,])([0-9]{1,3}(?:[^\S\n][0-9]{3})+|[0-9]{1,9})(?:([,.][0-9]{2}))?[ \t]*(?:₽|руб(?:лей|ля|ль)?\.?|[РP]\.?)(?!\w)", re.I)


def amounts(text):
    found = []
    for match in MONEY.finditer(text):
        price = parse_price(match[1] + (match[2] or ""))
        if price and price not in found:
            found.append(price)
    return tuple(found[:8])


async def recognize(data):
    executable = shutil.which("tesseract")
    if not executable:
        raise ParseError("Для чтения скриншотов выполните в Debian: apt install tesseract-ocr tesseract-ocr-rus. Или нажмите «Цена» в черновике.")
    process = await asyncio.create_subprocess_exec(
        executable, "stdin", "stdout", "-l", "rus+eng", "--psm", "11",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), 18)
    except asyncio.TimeoutError as error:
        raise ParseError("Распознавание заняло слишком много времени. Обрежьте скриншот до блока цены.") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise ParseError("Не удалось прочитать изображение. Проверьте установку tesseract-ocr и tesseract-ocr-rus в Debian.")
    return amounts(output.decode("utf-8", "replace"))
