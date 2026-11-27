"""Черновое название из адреса карточки Ozon.

Короткая ссылка ozon.ru/t/… перенаправляет на /product/<транслит-названия>-<SKU>/. Даже когда
сама карточка закрыта (HTTP 403), этот адрес уже получен ботом от Ozon. Обратная
транслитерация неточна: мягкий знак теряется, латиница становится кириллицей, длинные
названия обрезаны. Поэтому результат помечается как черновой и требует подтверждения.
"""
from __future__ import annotations

import re

_MULTI = (
    ("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ch", "ч"), ("sh", "ш"),
    ("yu", "ю"), ("ya", "я"),
)
_SINGLE = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з", "i": "и", "k": "к", "l": "л",
    "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х",
    "c": "ц", "w": "в", "x": "кс", "q": "к", "j": "дж",
}
_VOWELS = set("аеёиоуыэюя")
# Единицы после числа и короткие слова, у которых есть устойчивое написание.
_UNITS = {"vt": "Вт", "kvt": "кВт", "l": "л", "ml": "мл", "kg": "кг", "g": "г", "sm": "см", "mm": "мм",
          "m": "м", "gb": "ГБ", "tb": "ТБ", "mp": "Мп", "v": "В", "a": "А", "ma": "мА", "mach": "мАч",
          "gts": "Гц", "dyuym": "дюйм", "dyuymov": "дюймов", "sht": "шт", "bar": "бар"}
_TRAILING = {"для", "с", "в", "на", "и", "из", "по", "от", "до", "к", "о", "без", "под", "над", "со", "за"}
_LATIN_HINT = re.compile(r"[wxqj]|c(?!h)|ph|th")  # таких сочетаний нет в схеме транслитерации Ozon
# Ozon теряет мягкий знак. Самые частые окончания восстанавливаем, остальное проверит человек.
_SOFT = ((re.compile(r"([аеи])лн"), r"\1льн"), (re.compile(r"тел$"), "тель"), (re.compile(r"(.{3,})ост$"), r"\1ость"),
         (re.compile(r"тел(я|ю|ем)$"), r"тел\1"))


def _word(token: str) -> str:
    if not token.isalpha():
        return token
    if _LATIN_HINT.search(token):
        return token.upper() if len(token) <= 3 else token[:1].upper() + token[1:]  # бренд или модель
    out = []
    i = 0
    while i < len(token):
        for latin, cyr in _MULTI:
            if token.startswith(latin, i):
                # «ts» перед согласной обычно «тс» (детский), иначе «ц» (цвет)
                out.append(cyr)
                i += len(latin)
                break
        else:
            ch = token[i]
            if ch == "t" and token.startswith("ts", i):
                out.append("тс" if token[i + 2:i + 3] == "k" else "ц")  # детский, но цвет
                i += 2
                continue
            if ch == "y":
                prev = out[-1] if out else ""
                at_end = i == len(token) - 1
                if prev in _VOWELS and (at_end or token[i + 1:i + 2] not in "aeiou"):
                    out.append("й")  # «ой», «ий», «ай», «ей», «ый» в конце
                elif not prev:
                    out.append("й")
                else:
                    out.append("ы")
                i += 1
                continue
            if ch == "e" and not out:
                out.append("э")  # экран, электрический
                i += 1
                continue
            out.append(_SINGLE.get(ch, ch))
            i += 1
    word = "".join(out)
    for pattern, repl in _SOFT:
        word = pattern.sub(repl, word)
    return word


def title_from_slug(slug: str) -> str:
    """'paroochistitel-dlya-uborki-15000-vt' → 'Пароочиститель для уборки 15000 Вт'."""
    tokens = [t for t in re.split(r"[-_]+", slug.lower()) if t]
    if tokens and tokens[-1].isdigit() and len(tokens[-1]) >= 6:
        tokens = tokens[:-1]  # хвост — сам SKU
    words = []
    for index, token in enumerate(tokens):
        previous = tokens[index - 1] if index else ""
        if previous.isdigit() and token in _UNITS:
            words.append(_UNITS[token])
        else:
            words.append(_word(token))
    while words and words[-1] in _TRAILING:
        words.pop()  # Ozon обрезает длинный адрес на середине фразы
    text = " ".join(words).strip()
    if len(text) < 6 or not re.search("[а-яё]", text):
        return ""
    return text[:1].upper() + text[1:]


def slug_of(path: str) -> str:
    """'/product/termos-stalnoy-1526882900/' → 'termos-stalnoy'."""
    match = re.match(r"^/product/([a-z0-9-]+)-[0-9]{5,16}/?$", path or "")
    return match.group(1) if match else ""
