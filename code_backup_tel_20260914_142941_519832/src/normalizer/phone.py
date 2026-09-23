import re
import unicodedata


def normalize_phone(value):
    """照合専用。元データは書き換えず、先頭0を推測で付け足さない。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    text = re.split(r"(?:内線|ext\.?)", text, flags=re.I)[0]
    return re.sub(r"[^0-9]", "", text)
