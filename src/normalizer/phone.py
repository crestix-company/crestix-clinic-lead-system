import re
import unicodedata


def tel_match_key(value):
    """統合専用キー。数字だけにし、先頭0を一つだけ外す。元の値は変更しない。"""
    text = "" if value is None else str(value)
    digits = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", text))
    return digits[1:] if digits.startswith("0") else digits


def normalize_phone(value):
    """照合専用。元データは書き換えず、先頭0を推測で付け足さない。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    text = re.split(r"(?:内線|ext\.?)", text, flags=re.I)[0]
    return re.sub(r"[^0-9]", "", text)
