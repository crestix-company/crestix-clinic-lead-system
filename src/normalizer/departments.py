"""帳票の略記を正規化する。原文はdepartmentsに別途保持する。"""
import re
import unicodedata

DEPARTMENTS = ["内科", "消化器内科", "循環器内科", "糖尿病内科", "眼科", "皮膚科", "泌尿器科", "美容整形外科", "整形外科", "歯科",
               "呼吸器内科", "腎臓内科", "血液内科", "心療内科", "精神科", "神経内科", "アレルギー科", "小児科",
               "脳神経外科", "心臓血管外科", "血管外科", "形成外科", "美容外科", "乳腺外科", "肛門外科",
               "耳鼻咽喉科", "産婦人科", "婦人科", "その他"]
ALIASES = {"内": "内科", "消": "消化器内科", "消内": "消化器内科", "胃": "消化器内科",
           "循": "循環器内科", "循内": "循環器内科", "糖": "糖尿病内科", "糖内": "糖尿病内科",
           "眼": "眼科", "皮": "皮膚科", "ひ": "泌尿器科", "泌": "泌尿器科", "整外": "整形外科", "歯": "歯科"}
# 実データ集計で略記の意味を確認できたものだけ追加（厚生局略記→科名。docs/DEPARTMENT_ABBREVIATIONS.md参照）。
ALIASES.update({"呼内": "呼吸器内科", "心内": "心療内科", "精": "精神科", "小": "小児科", "脳外": "脳神経外科",
                "形外": "形成外科", "美外": "美容外科", "アレ": "アレルギー科", "耳い": "耳鼻咽喉科",
                "矯歯": "歯科", "小歯": "歯科", "歯外": "歯科"})
# 「小児皮膚科」「美容皮膚科」など、正式な科名を含む表記。長い科名を先に判定する。
NAME_CONTAINS = ["美容整形外科", "産婦人科", "婦人科", "心臓血管外科", "血管外科", "脳神経外科", "耳鼻咽喉科", "耳鼻いんこう科", "皮膚科",
                 "眼科", "泌尿器科", "整形外科", "呼吸器内科", "腎臓内科", "血液内科", "神経内科", "精神科",
                 "アレルギー科", "小児科", "形成外科", "美容外科", "乳腺外科", "肛門外科"]
NAME_ALIAS = {"耳鼻いんこう科": "耳鼻咽喉科"}


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
        elif any(name in token for name in NAME_CONTAINS):
            name = next(name for name in NAME_CONTAINS if name in token)
            result.add(NAME_ALIAS.get(name, name))
        else:
            result.add("その他")
    return [x for x in DEPARTMENTS if x in result]
