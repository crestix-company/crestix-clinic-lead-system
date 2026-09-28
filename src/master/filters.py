from dataclasses import dataclass, field, asdict
from src.utils.date_utils import today_japan
from src.scoring.research_scoring import AD_SIGNAL_NAMES

# 広告・集客施策数は既存の signal_count（HP制作会社等を含む）ではなく、抽出時に signals_json から数える。
AD_COUNT_SQL = "(SELECT count(DISTINCT value) FROM json_each(signals_json) WHERE value IN ("+",".join("'"+n.replace("'","''")+"'" for n in AD_SIGNAL_NAMES)+"))"


@dataclass
class Filters:
    active_only: bool = True
    hp_only: bool = True
    recent_only: bool = False
    age_min: float | None = None
    ranks: list[str] = field(default_factory=list)
    prefectures: list[str] = field(default_factory=list)
    medical_types: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    treatments: list[str] = field(default_factory=list)
    sales_pairs: list[tuple[str, str]] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    ad_min: int = 0
    production_companies: list[str] = field(default_factory=list)
    signal_min: int = 0
    hot: list[str] = field(default_factory=list)
    owner_equal: str = "指定なし"
    uuid_mode: str = "指定なし"
    new_only: bool = False
    recent_opening: bool = False
    maps_confirmed_only: bool = False
    maps_website_only: bool = False
    keyword: str = ""


def clauses(f, as_of=None):
    day = (as_of or today_japan()).isoformat()
    steps = []
    def add(label, sql, *args):
        steps.append((label, sql, list(args)))
    if f.active_only:
        add("現存クリニック（一覧基準日）", "active=1")
    if f.hp_only:
        add("HP確認済み", "hp_status='VERIFIED' AND hp_url<>''")
    if f.recent_only:
        add("指定年月日から10年以内", "designation_date<=? AND recent_until>=?", day, day)
    if f.age_min is not None:
        if not 0 <= f.age_min <= 1:
            raise ValueError("年齢確率は0〜100%で指定してください。")
        add(f"59歳以下確率{f.age_min:.0%}以上", "age_probability>=?", f.age_min)
    for values, col, label in [(f.ranks,"hp_rank","HPランク"), (f.prefectures,"prefecture","都道府県"),
                               (f.medical_types,"medical_type","医科・歯科"), (f.hot,"hot_status","アツさ")]:
        if values:
            add(label, f"{col} IN ({','.join('?' for _ in values)})", *values)
    # 診療科/治療の複数選択は同一項目内OR、項目間AND。特定シグナルは全選択AND。
    for values, col, label in [(f.departments,"departments_json","診療科"), (f.treatments,"treatments_json","治療カテゴリ")]:
        if values:
            add(label, f"EXISTS(SELECT 1 FROM json_each({col}) WHERE value IN ({','.join('?' for _ in values)}))", *values)
    if f.sales_pairs:
        from src.master.sales_treatments import treatment_definition, VALID_EVIDENCE_SOURCES
        pair_sql, pair_args = [], []
        sources = sorted(VALID_EVIDENCE_SOURCES)
        for department, treatment in f.sales_pairs:
            definition = treatment_definition(department, treatment)
            if definition is None or definition.support_status == "MISSING":
                continue
            evidence_match = (
                "json_extract(e.value,'$.category') IN (" +
                ",".join("?" for _ in definition.current_categories) + ") AND " +
                "EXISTS(SELECT 1 FROM json_each(treatments_json) tc "
                "WHERE tc.value=json_extract(e.value,'$.category')) AND " +
                "json_extract(e.value,'$.source') IN (" + ",".join("?" for _ in sources) + ")"
            )
            args = [department, *definition.current_categories, *sources]
            if definition.match_mode == "KEYWORD":
                evidence_match += " AND json_extract(e.value,'$.keyword') IN (" + ",".join(
                    "?" for _ in definition.evidence_keywords
                ) + ")"
                args.extend(definition.evidence_keywords)
            pair_sql.append(
                "(EXISTS(SELECT 1 FROM json_each(departments_json) d WHERE d.value=?) "
                "AND EXISTS(SELECT 1 FROM research_results r, "
                "json_each(json_extract(r.result_json,'$.treatment_evidence')) e "
                "WHERE r.clinic_id=clinics.id AND " + evidence_match + "))"
            )
            pair_args.extend(args)
        add("標榜診療科×治療", "(" + " OR ".join(pair_sql) + ")" if pair_sql else "0", *pair_args)
    if f.signal_min:
        add(f"集客投資シグナル{f.signal_min}個以上", "signal_count>=?", f.signal_min)
    # 広告・集客施策の複数選択は同一項目内OR。施策数は別項目（AND）。
    if f.signals:
        add("広告・集客施策", f"EXISTS(SELECT 1 FROM json_each(signals_json) WHERE value IN ({','.join('?' for _ in f.signals)}))", *f.signals)
    if f.ad_min:
        add(f"広告・集客施策{f.ad_min}個以上", AD_COUNT_SQL+">=?", f.ad_min)
    if f.production_companies:
        add("HP制作会社", f"EXISTS(SELECT 1 FROM json_each(json_extract(effective_json,'$.hp_production_companies')) WHERE value IN ({','.join('?' for _ in f.production_companies)}))", *f.production_companies)
    if f.owner_equal != "指定なし":
        add("開設者＝管理者", "owner_equal=?", int(f.owner_equal == "一致のみ"))
    if f.uuid_mode != "指定なし":
        add("既存UUID", "uuid<>''" if f.uuid_mode == "あり" else "uuid=''")
    if f.new_only:
        add("前回更新から追加", "is_new=1")
    if f.recent_opening:
        add("新規開業候補（1年以内・新規指定）", "designation_date BETWEEN date(?, '-1 year') AND ? AND registration_reason LIKE '%新規%'", day, day)
    if f.maps_confirmed_only:
        add("Google Maps掲載確認済み", "maps_presence_status IN ('MAPS_MATCHED_WEBSITE','MAPS_MATCHED_NO_WEBSITE')")
    if f.maps_website_only:
        add("Google Maps HP取得済み", "maps_presence_status='MAPS_MATCHED_WEBSITE' AND maps_website_url<>''")
    if f.keyword.strip():
        add("医院検索", "(clinic_name LIKE ? ESCAPE '\\' OR phone_norm LIKE ? ESCAPE '\\' OR uuid=?)", "%"+f.keyword.strip().replace("\\","\\\\").replace("%","\\%").replace("_","\\_")+"%", "%"+''.join(x for x in f.keyword if x.isdigit())+"%" if any(x.isdigit() for x in f.keyword) else "__NO_PHONE__", f.keyword.strip())
    return steps


def where(f, as_of=None):
    steps = clauses(f, as_of)
    return " AND ".join(["merged_into IS NULL", "merge_hold=0"]+[s for _,s,_ in steps]), [p for _,_,args in steps for p in args]
