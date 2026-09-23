import re
import unicodedata

VARIANTS = str.maketrans({"髙": "高", "﨑": "崎", "神": "神", "邊": "辺", "邉": "辺",
                         "齋": "斎", "齊": "斉", "澤": "沢", "濱": "浜", "廣": "広",
                         "醫": "医", "國": "国", "內": "内"})


def normalize_person(value):
    text = unicodedata.normalize("NFKC", str(value or "")).translate(VARIANTS)
    text = re.sub(r"^(?:理事長|院長|管理者|開設者)[ :：]*", "", text)
    return re.sub(r"[\s・･.．]", "", text).casefold()


def person_from_owner(value):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if "理事長" in text:
        return text.split("理事長")[-1].strip(" :：")
    if re.search(r"法人|株式会社|有限会社|組合|大学|学校|区長|市長|知事", text):
        return ""
    return text


def surname(value, explicit=""):
    if explicit:
        return normalize_person(explicit)
    person = person_from_owner(value)
    parts = re.split(r"\s+", unicodedata.normalize("NFKC", person).strip())
    return normalize_person(parts[0]) if len(parts) >= 2 else ""


def normalize_clinic_name(value, loose=False):
    text = unicodedata.normalize("NFKC", str(value or "")).translate(VARIANTS).strip().casefold()
    # 法人名の終端が明確な場合だけ除去。任意の長さの個人名は推測しない。
    text = re.sub(r"^(?:(?:社会|特定)?医療法人(?:社団|財団)?)[\s　]*[^\s　]*?会[\s　]*", "", text)
    text = re.sub(r"^(?:(?:社会|特定)?医療法人(?:社団|財団)?)[\s　]+[^\s　]+[\s　]+", "", text)
    text = re.sub(r"^(?:(?:社会|特定)?医療法人(?:社団|財団)?)", "", text)
    text = re.sub(r"[\s()（）・･,，.．\-‐‑–—]", "", text)
    if loose:
        text = re.sub(r"クリニック|医院|診療所", "診療所", text)
    return text
