"""帳票の略記を正規化する。原文はdepartmentsに別途保持する。"""
import re
import unicodedata

DEPARTMENTS = ["内科", "消化器内科", "循環器内科", "糖尿病内科", "眼科", "皮膚科", "泌尿器科", "整形外科", "歯科", "その他"]
ALIASES = {"内": "内科", "消": "消化器内科", "消内": "消化器内科", "胃": "消化器内科",
           "循": "循環器内科", "循内": "循環器内科", "糖": "糖尿病内科", "糖内": "糖尿病内科",
           "眼": "眼科", "皮": "皮膚科", "ひ": "泌尿器科", "泌": "泌尿器科", "整外": "整形外科", "歯": "歯科"}


def normalize_departments(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    result = set()
    for token in re.split(r"[\s,、/／;|・]+", text):
        if not token:
            continue
        if token in ALIASES:
            result.add(ALIASES[token])
        elif "糖尿病" in token:
            result.add("糖尿病内科")
        elif "消化器" in token or "胃腸" in token:
            result.add("消化器内科")
        elif "循環器" in token:
            result.add("循環器内科")
        elif token in DEPARTMENTS:
            result.add(token)
        else:
            result.add("その他")
    return [x for x in DEPARTMENTS if x in result]
