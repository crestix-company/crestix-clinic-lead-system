import re
import unicodedata
from .clinic_name import VARIANTS

KANJI = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _number(match):
    value = match[1]
    if "十" in value:
        a, b = value.split("十", 1)
        n = (KANJI.get(a, 1) if a else 1) * 10 + (KANJI.get(b, 0) if b else 0)
    else:
        n = int("".join(str(KANJI[c]) for c in value))
    return str(n)


def normalize_address(value):
    text = unicodedata.normalize("NFKC", str(value or "")).translate(VARIANTS)
    text = re.sub(r"〒?\s*\d{3}-\d{4}\s*", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"([〇一二三四五六七八九十]+)(?=丁目|番地?|号)", _number, text)
    text = re.sub(r"[‐‑–—−ー]", "-", text)
    text = re.sub(r"(?<=\d)(?:丁目|番地|番|号)(?:の)?", "-", text)
    text = re.sub(r"(?<=\d)の(?=\d)", "-", text)
    return re.sub(r"-+", "-", text).strip("-").casefold()
