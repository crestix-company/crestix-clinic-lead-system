from copy import deepcopy
import re
import unicodedata
from bs4 import BeautifulSoup
from src.scoring.age_estimator import AgeEstimator
from src.normalizer.clinic_name import normalize_person
from src.utils.date_utils import parse_year, parse_date, today_japan

YEAR_PATTERN = r"(?:19\d{2}|20\d{2}|(?:平成|昭和|令和)\s*(?:元|\d{1,2}))年?"


def _clean(text):
    return unicodedata.normalize("NFKC",str(text or ""))


ROLE_NAME_PATTERN = re.compile(
    # v25.2: 次の人物見出しは「姓 名」だけでなく「新谷弘実」のような
    # 空白なし氏名も境界として扱う。医師免許/国家試験などの一般語は除外する。
    r"(?:院長|理事長|副院長|名誉院長|特別顧問|名誉顧問|顧問|管理者|担当医)"
    r"\s*[：:]?\s*[一-龯々﨑髙隆ヶヵ]{2,12}"
    r"|(?:^|[\s　])医師\s*[：:]?\s*(?!免許|国家試験|紹介|法|会|として|の)"
    r"[一-龯々﨑髙隆ヶヵ]{2,12}"
)


def _manager_pattern(manager_name):
    cleaned=_clean(manager_name).strip()
    parts=[re.escape(x) for x in re.split(r"[\s　]+",cleaned) if x]
    if len(parts)>=2:
        return re.compile(r"\s*".join(parts))
    compact=re.escape(cleaned.replace(" ","").replace("　",""))
    return re.compile(compact)


def _trim_to_manager_block(text, manager_name, max_len=1800):
    """複数医師ページから現在の管理者本人の経歴ブロックだけを切り出す。"""
    text=_clean(text)
    pat=_manager_pattern(manager_name)
    m=pat.search(text)
    if not m:
        return ""

    # v25.2: 本人名より前の別医師の経歴年を混ぜない。
    start=max(0,m.start()-40)
    end=min(len(text),m.end()+max_len)

    # 次の役職 + 別人名（空白なし氏名も含む）が出たら、そこで切る。
    for nxt in ROLE_NAME_PATTERN.finditer(text,m.end()):
        block=nxt.group(0)
        if not pat.search(block):
            end=min(end,nxt.start())
            break

    return text[start:end].strip()



def _manager_heading_chunks(soup, manager_name, max_len=1800):
    """院長名が見出し(h1-h6)にある場合、その見出しから次の別人物見出しまでだけを使う。

    v25.2:
    半蔵門胃腸クリニックのように
      h3 院長 掛谷和俊
      1982年 ... 医学部卒業
      h3 特別顧問 新谷弘実
      1960年 ... / 1963年 ...
    と続くページで、特別顧問側の年を院長経歴へ混ぜない。
    """
    name=normalize_person(manager_name)
    if not name:
        return []

    chunks=[]
    seen=set()
    heading_names={"h1","h2","h3","h4","h5","h6"}

    for heading in soup.find_all(list(heading_names)):
        heading_text=_clean(heading.get_text(" ",strip=True))
        if name not in normalize_person(heading_text):
            continue

        parts=[heading_text]
        for elem in heading.next_elements:
            if elem is heading:
                continue

            elem_name=getattr(elem,"name",None)
            if elem_name in heading_names:
                candidate=_clean(elem.get_text(" ",strip=True))
                # 次の「役職 + 別人名」見出しで終了。
                if ROLE_NAME_PATTERN.search(candidate) and name not in normalize_person(candidate):
                    break
                continue

            # NavigableString 等だけを積む。タグ本体は子孫テキストと重複するため追加しない。
            if elem_name is None:
                piece=_clean(str(elem)).strip()
                if piece:
                    parts.append(piece)

            joined=re.sub(r"\s+"," "," ".join(parts)).strip()
            if len(joined)>=max_len:
                break

        chunk=re.sub(r"\s+"," "," ".join(parts)).strip()[:max_len]
        if chunk and re.search(r"卒業|医師免許|医籍|国家試験",chunk):
            key=chunk[:1200]
            if key not in seen:
                seen.add(key)
                chunks.append(chunk)

    return chunks


def _director_chunks(page, manager_name):
    """複数医師ページでも、現在の管理者本人の近傍だけを経歴候補にする。"""
    name=normalize_person(manager_name)
    if not name:
        return []
    soup=BeautifulSoup(page.html,"html.parser")
    for x in soup.select("script,style,noscript,template"):
        x.decompose()

    # v25.3: 古い医院HPでは役職・医師名が画像の alt/title にしか入っていないことがある。
    # その場合も DOM 見出しとして扱えるよう、画像の代替テキストを可視文字列へ展開する。
    for img in soup.find_all("img"):
        alt=_clean(img.get("alt") or img.get("title") or "").strip()
        if alt:
            img.replace_with(" " + alt + " ")

    # v25.2: 見出しで院長本人が明示されているページは、まず見出し境界で人物分離する。
    heading_chunks=_manager_heading_chunks(soup,manager_name)
    if heading_chunks:
        return heading_chunks

    chunks=[]
    seen=set()
    for node in soup.find_all(string=True):
        raw=_clean(node)
        if name not in normalize_person(raw):
            continue
        cur=node.parent
        selected=None
        # v25: 5000文字級の親要素まで広げず、本人の近傍を優先する。
        for _ in range(7):
            if cur is None:
                break
            text=_clean(cur.get_text(" ",strip=True))
            if name in normalize_person(text) and re.search(r"院長|管理者|経歴|略歴|プロフィール|卒業|医師免許|医籍|国家試験",text):
                trimmed=_trim_to_manager_block(text,manager_name)
                if trimmed:
                    selected=trimmed
                if selected and len(selected)<=1800 and re.search(r"卒業|医師免許|医籍|国家試験",selected):
                    break
            cur=cur.parent
        if selected:
            key=selected[:1200]
            if key not in seen:
                seen.add(key)
                chunks.append(selected)

    # DOM上で名前が分割される場合も、ページ全体ではなく本人名周辺だけを使う。
    full=_clean(soup.get_text(" ",strip=True))
    if not chunks and name in normalize_person(full):
        trimmed=_trim_to_manager_block(full,manager_name)
        if trimmed and re.search(r"院長|管理者|経歴|略歴|プロフィール|卒業|医師免許|医籍|国家試験",trimmed):
            chunks=[trimmed]
    return chunks


def _year_matches(text):
    return [(m.start(),m.end(),parse_year(m.group(0)),m.group(0)) for m in re.finditer(YEAR_PATTERN,text)]


def _nearest_year(text, keyword_match, before=55, after=14):
    """卒業/医籍キーワードに対応する年を採用する。

    v25.1: 「1960年 医学部卒業。1963年に渡米」のような文では、
    卒業後に現れる勤務・留学年ではなく、キーワード直前の年を必ず優先する。
    直前に年が無い表記（「医学部卒業 1994年」等）のみ後方年へフォールバックする。
    """
    years=_year_matches(text)
    before_candidates=[]
    after_candidates=[]
    for ys,ye,year,raw in years:
        if not year or not (1900<=year<=today_japan().year):
            continue
        if ye<=keyword_match.start():
            distance=keyword_match.start()-ye
            if distance<=before:
                before_candidates.append((distance,year,raw,ys,ye))
        elif ys>=keyword_match.end():
            distance=ys-keyword_match.end()
            if distance<=after:
                after_candidates.append((distance,year,raw,ys,ye))
    if before_candidates:
        return min(before_candidates,key=lambda x:x[0])
    if after_candidates:
        return min(after_candidates,key=lambda x:x[0])
    return None


def _years_from_chunks(chunks, kind):
    results=[]
    seen=set()
    if kind=="graduation":
        keyword=re.compile(
            r"(?:医学部|医学科|医科大学)[^。、；;\n]{0,18}?(?:卒業|卒)",re.I
        )
    else:
        keyword=re.compile(r"医師免許(?:取得)?|医籍(?:登録)?|医師国家試験(?:合格)?",re.I)

    for text in chunks:
        for km in keyword.finditer(text):
            nearest=_nearest_year(text,km,before=60 if kind=="graduation" else 45,after=14)
            if not nearest:
                continue
            _,year,_,ys,ye=nearest
            left=max(0,min(ys,km.start())-35)
            right=min(len(text),max(ye,km.end())+55)
            evidence=re.sub(r"\s+"," ",text[left:right]).strip()
            key=(year,evidence)
            if key in seen:
                continue
            seen.add(key)
            results.append((year,evidence))
    return results

def graduation_evidence(record,pages):
    """現在の院長/管理者本人の近傍から大学医学部の卒業年を抽出。複数医師ページにも対応。"""
    manager=record.get("manager_name","")
    result=[]
    seen=set()
    for page in pages:
        for year,evidence in _years_from_chunks(_director_chunks(page,manager),"graduation"):
            key=(year,page.url)
            if key in seen:
                continue
            seen.add(key)
            result.append({"year":year,"url":page.url,"evidence":evidence[:300],"doctor_name":manager})
    return result


def license_evidence(record,pages):
    manager=record.get("manager_name","")
    result=[]
    seen=set()
    for page in pages:
        for year,evidence in _years_from_chunks(_director_chunks(page,manager),"license"):
            key=(year,page.url)
            if key in seen:
                continue
            seen.add(key)
            result.append({"year":year,"url":page.url,"evidence":evidence[:300],"doctor_name":manager})
    return result


def _int_age(value):
    try:
        age=int(float(str(value).strip()))
        return age if 20<=age<=100 else None
    except (TypeError,ValueError):
        return None


def estimate_profile_age(record,pages=(),current_year=None):
    """年齢根拠の優先順: 公的/取込済み年齢・生年・医籍年 → HPの医籍年 → HP卒業年。

    指定年月日は開業時期の公的情報として先に確認するが、年齢そのものは算出できないため
    年齢値の根拠には使わず、経歴へ自動フォールバックする。
    """
    current_year=current_year or today_japan().year
    designation=parse_date(record.get("designation_date",""))
    master_context={
        "designation_date":designation.isoformat() if designation else str(record.get("designation_date","") or ""),
        "registration_reason":str(record.get("registration_reason","") or ""),
        "owner_manager_equal":record.get("owner_manager_equal"),
    }

    result={
        "doctor_name":record.get("manager_name",""),"license_registration_year":None,"graduation_year":None,
        "graduation_evidence":[],"license_evidence":[],"age_probability_under_59":None,
        "age_estimation_source":"UNKNOWN","age_estimation_confidence":"UNKNOWN","age_estimation_year":current_year,
        "age_master_context":master_context,
    }

    direct_age=next((_int_age(record.get(k)) for k in ["manager_age","doctor_age"] if _int_age(record.get(k)) is not None),None)
    if direct_age is None and record.get("owner_manager_equal") is True:
        direct_age=_int_age(record.get("owner_age"))
    if direct_age is not None:
        return {**result,"age_probability_under_59":1.0 if direct_age<=59 else 0.0,
                "age_estimation_source":"厚生局/補足マスタの管理者年齢","age_estimation_confidence":"OFFICIAL_VALUE",
                "age_estimation_reason":f"公的・取込済み管理者年齢 {direct_age}歳を使用。指定年月日も確認済み。"}

    birth=parse_year(record.get("manager_birth_year") or record.get("doctor_birth_year"))
    if birth and 1900<=birth<=current_year:
        approx=current_year-birth
        probability=1.0 if approx<=58 else .5 if approx==59 else 0.0
        return {**result,"age_probability_under_59":probability,"age_estimation_source":"厚生局/補足マスタの管理者生年",
                "age_estimation_confidence":"OFFICIAL_YEAR","age_estimation_reason":"生年のみのため誕生日未反映。指定年月日も確認済み。"}

    grad_evidence=graduation_evidence(record,pages) if pages else record.get("graduation_evidence",[]) or []
    lic_evidence=license_evidence(record,pages) if pages else record.get("license_evidence",[]) or []
    grad_years={e["year"] for e in grad_evidence if e.get("year")}
    lic_years={e["year"] for e in lic_evidence if e.get("year")}

    # v25.4: 強制再調査では、過去の自動調査結果より今回のHP実測根拠を優先する。
    # store.get() は前回の research_results も record に含めるため、古い誤判定年をそのまま
    # record["graduation_year"] から再利用すると、今回の正しい evidence を上書きしてしまう。
    # ただし手動修正値は最優先で維持する。
    manual_fields=set(record.get("manual_fields") or [])

    record_lic=parse_year(record.get("license_registration_year"))
    if "license_registration_year" in manual_fields and record_lic:
        lic=record_lic
    elif len(lic_years)==1:
        lic=next(iter(lic_years))
    elif not lic_years:
        lic=record_lic
    else:
        lic=None
    if lic and not 1900<=lic<=current_year:
        lic=None

    record_grad=parse_year(record.get("graduation_year"))
    if "graduation_year" in manual_fields and record_grad:
        grad=record_grad
    elif len(grad_years)==1:
        grad=next(iter(grad_years))
    elif not grad_years:
        grad=record_grad
    else:
        grad=None
    if grad and not 1900<=grad<=current_year:
        grad=None

    result.update(license_registration_year=lic,graduation_year=grad,
                  graduation_evidence=grad_evidence,license_evidence=lic_evidence)

    if len(grad_years)>1 or len(lic_years)>1 or (lic and grad and (lic<grad or lic-grad>4)):
        return {**result,"age_estimation_confidence":"REVIEW",
                "age_estimation_reason":"院長本人の卒業年/医籍年候補が複数または矛盾しています。指定年月日では年齢を確定できないため要確認。"}

    if not lic and not grad:
        source=(f"厚生局指定年月日 {designation.isoformat()} を確認（年齢は直接算出不可）" if designation else "年齢根拠未取得")
        return {**result,"age_estimation_source":source,
                "age_estimation_reason":"指定年月日は開業時期の根拠であり院長年齢ではないため、HPの院長経歴まで確認したが卒業年/医籍年を取得できませんでした。"}

    if lic:
        estimate=AgeEstimator().estimate(lic,current_year)
        if record.get("license_registration_year"):
            source=record.get("license_source") or "厚生局/取込済み医籍登録年"
        else:
            source=lic_evidence[0]["url"] if lic_evidence else "HP院長経歴の医師免許年"
    else:
        config=deepcopy(AgeEstimator().config)
        config["national_exam_delay_distribution"]={"delay_0":1.0}
        estimate=AgeEstimator(config).estimate(grad,current_year)
        source=grad_evidence[0]["url"] if grad_evidence else "HP院長経歴の大学卒業年"

    return {**result,"age_probability_under_59":estimate.probability,"age_estimation_source":source,
            "age_estimation_confidence":"MODEL_ESTIMATE",
            "age_estimation_reason":"指定年月日を先に確認し、年齢を直接算出できないため院長本人の医籍年/大学卒業年から仮定分布で59歳以下確率を推定。"}
