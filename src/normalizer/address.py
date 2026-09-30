import re
import unicodedata
from .clinic_name import VARIANTS

KANJI = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 市区町村抽出（郡付き町村を含む）。市/区/町/村より前を非貪欲に最大7文字まで許容する。
_MUNICIPALITY_RE = re.compile(r"^((?:.{1,6}郡)?.{1,7}?(?:市|区|町|村))")


def extract_municipality(address):
    """住所文字列（都道府県プレフィックス付き/なしどちらでも可）から市区町村だけを抜き出す。

    clinics.address列は常に都道府県プレフィックス付きで格納される（src/master/comdesk.py
    の record_from_row 参照）。ここでは comdesk.PREFECTURE を遅延importして先に剥がし、
    残りの先頭から市区町村を1つだけ抜き出す。マッチしなければ空文字。
    DBスキーマは変更しない（query-time専用のpure関数、SQLiteのユーザー定義関数として登録する）。
    """
    from src.master.comdesk import PREFECTURE  # 遅延import: normalizer -> master の逆依存を避ける

    text = str(address or "")
    prefix = PREFECTURE.match(text)
    rest = text[len(prefix[1]):] if prefix else text
    match = _MUNICIPALITY_RE.match(rest)
    return match[1] if match else ""


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
