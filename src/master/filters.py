from dataclasses import dataclass, field, asdict
from src.utils.date_utils import today_japan
from src.scoring.research_scoring import AD_SIGNAL_NAMES
from src.master.scope import SCOPE_ALL, SCOPE_LEGACY_PRE_NATIONAL, SCOPE_VALUES, LEGACY_PRE_NATIONAL_CUTOFF
from src.master.research_sidecar import RESEARCH_SIDECAR_QUALIFIED_TABLE, CLINIC_RESEARCH_STATUS_QUALIFIED_TABLE

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
    municipalities: list[str] = field(default_factory=list)
    medical_types: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    mhlw_official_departments: list[str] = field(default_factory=list)
    crestix_sales_departments: list[str] = field(default_factory=list)
    # 後方互換: 旧mhlw_departmentsはCrestix営業カテゴリを意味する。
    mhlw_departments: list[str] = field(default_factory=list)
    treatments: list[str] = field(default_factory=list)
    # Phase7 Treatment Research（CONFIRMED根拠）ベースの新filter。旧treatments(treatments_json)とは別物。
    hp_treatment_categories: list[str] = field(default_factory=list)
    research_status: list[str] = field(default_factory=list)
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
    scope: str = SCOPE_ALL


def clauses(f, as_of=None):
    day = (as_of or today_japan()).isoformat()
    steps = []
    def add(label, sql, *args):
        steps.append((label, sql, list(args)))
    if f.scope not in SCOPE_VALUES:
        raise ValueError("対象データ（scope）の指定を確認してください。")
    if f.scope == SCOPE_LEGACY_PRE_NATIONAL:
        add("既存営業リスト（全国append前）", "first_seen_at<?", LEGACY_PRE_NATIONAL_CUTOFF)
    # 病院・センターは厚生局マスターから削除しないが(README.md「病院・センター」節)、
    # 通常の営業用Comdesk出力には含めない。UI count = CSV rowsを常に一致させるため、
    # fixed_export.pyだけでなくFilter/一覧側にも同じ判定を適用する(選択で外せない必須条件)。
    add("病院・センター除外（営業対象外）",
        "NOT (exclude_reason IN ('hospital','center') "
        "OR COALESCE(json_extract(effective_json,'$.facility_type'),'')='病院' "
        "OR clinic_name LIKE '%病院%' OR clinic_name LIKE '%センター%')")
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
    if f.municipalities:
        add("市区町村", f"municipality_of(address) IN ({','.join('?' for _ in f.municipalities)})", *f.municipalities)
    # 診療科/治療の複数選択は同一項目内OR、項目間AND。特定シグナルは全選択AND。
    for values, col, label in [(f.departments,"departments_json","診療科"), (f.treatments,"treatments_json","治療カテゴリ")]:
        if values:
            add(label, f"EXISTS(SELECT 1 FROM json_each({col}) WHERE value IN ({','.join('?' for _ in values)}))", *values)
    if f.mhlw_official_departments:
        # ナビイ正式名称は完全一致のみ。substring分類やCrestix名称への置換は行わない。
        add("ナビイ正式診療科", "EXISTS(SELECT 1 FROM mhlwdb.clinic_mhlw_departments_final m WHERE m.clinic_id=clinics.id "
            f"AND m.mhlw_department_name IN ({','.join('?' for _ in f.mhlw_official_departments)}))",
            *f.mhlw_official_departments)
    crestix_departments = list(dict.fromkeys([*f.crestix_sales_departments, *f.mhlw_departments]))
    if crestix_departments:
        # mhlw_departmentsは旧UI/APIとの後方互換。意味はCrestix営業カテゴリのまま維持する。
        add("Crestix営業カテゴリ", "EXISTS(SELECT 1 FROM mhlwdb.clinic_mhlw_departments_final m WHERE m.clinic_id=clinics.id "
            f"AND m.mapping_status IN ('EXACT','ALIAS') AND m.crestix_department IN ({','.join('?' for _ in crestix_departments)}))",
            *crestix_departments)
    if f.hp_treatment_categories:
        # Phase7 CONFIRMED根拠のみ営業対象。REVIEW/NOT_CONFIRMED/FETCH_FAILED/未調査は含めない。
        # Navi正式診療科・Crestix営業カテゴリとは独立したclinic_id JOIN（Crestix9カテゴリへの限定なし）。
        add("HP治療カテゴリ（CONFIRMED）",
            f"EXISTS(SELECT 1 FROM {RESEARCH_SIDECAR_QUALIFIED_TABLE} r WHERE r.clinic_id=clinics.id "
            f"AND r.treatment_category_name IN ({','.join('?' for _ in f.hp_treatment_categories)}) "
            "AND r.research_status='CONFIRMED')",
            *f.hp_treatment_categories)
    if f.research_status:
        # 医院単位の調査状態（SSOT: clinic_research_status）。clinic_treatment_research_final
        # （Treatment単位・HP治療カテゴリ用）とは独立したテーブルで、一方の値から他方を推測しない。
        statuses = [s for s in f.research_status if s != "NOT_RESEARCHED"]
        parts, status_args = [], []
        if statuses:
            parts.append(
                f"EXISTS(SELECT 1 FROM {CLINIC_RESEARCH_STATUS_QUALIFIED_TABLE} rs WHERE rs.clinic_id=clinics.id "
                f"AND rs.research_status IN ({','.join('?' for _ in statuses)}))"
            )
            status_args.extend(statuses)
        if "NOT_RESEARCHED" in f.research_status:
            parts.append(f"NOT EXISTS(SELECT 1 FROM {CLINIC_RESEARCH_STATUS_QUALIFIED_TABLE} rs WHERE rs.clinic_id=clinics.id)")
        add("Research Status", "(" + " OR ".join(parts) + ")", *status_args)
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
