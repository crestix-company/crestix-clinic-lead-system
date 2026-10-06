"""Translates src.master.filters.Filters into Postgres SQL for the Supabase adapter.

Behavior-preserving rules applied here:
  - SQLite's default LIKE case-folds ASCII A-Z/a-z ONLY -- it does NOT fold fullwidth Latin
    (e.g. "Ｈ" vs "ｈ") or any other Unicode case pair. Postgres's ILIKE folds all of Unicode
    (confirmed empirically: 'ＨＩＫＡ' ILIKE '%ｈｉｋａ%' is true in Postgres, false as a plain
    LIKE in SQLite), which is a wider match than SQLite ever produces -- using ILIKE here would
    be a real behavior change, not just a dialect difference, so it is never used. Instead
    `clinic_name`/`phone_norm` and the search pattern are both passed through `_ascii_fold()`
    (translate() over just A-Z/a-z) and then compared with a plain, case-sensitive LIKE.
  - clinics.active / is_new / merge_hold / owner_equal are `boolean` in Postgres (they were
    `INTEGER` 0/1 in SQLite; Stage3's migration converted them -- see full_shadow_import.py
    bool_cols) -> compared against true/false literals, never 0/1.
  - departments_json / treatments_json / signals_json are native `jsonb` in Postgres (they were
    JSON-encoded TEXT in SQLite, read via json_each) -> queried with jsonb_array_elements_text.
  - effective_json is still `text` in Postgres, not `jsonb` (stored losslessly, byte-identical to
    SQLite -- and deliberately so: some rows embed a NUL-character JSON escape inside a string
    value, which is valid JSON/valid SQLite TEXT but which jsonb refuses outright with
    "unsupported Unicode escape sequence"). It is never cast to jsonb here; the one key this
    module reads from it (facility_type, for the always-applied hospital/center exclusion) is
    pulled out with a plain quote-free substring regex instead.

Fields whose SSOT never reached Postgres in the Stage3 migration (the MHLW sidecar, the Sales
Classification CSV) or whose translation is deliberately deferred past Stage4-A (sales_pairs,
hp_treatment_categories, research_status) raise BackendNotSupportedError instead of silently
producing a wrong answer. See docs/supabase_migration/ for the Stage3 mapping that backs this.

effective_ranks / site_types / municipalities / production_companies are NOT translated to SQL
at all: their source logic (effective_hp_rank(), classify_site(), extract_municipality(), and
reading the hp_production_companies array out of effective_json) stays the single pure-Python
implementation already used by the SQLite path -- the last one specifically so it never needs a
jsonb cast either -- applied here as a post-filter in Python so there is never a second,
divergent implementation of that logic.
"""
from src.master.filters import AD_COUNT_SQL as _SQLITE_AD_COUNT_SQL  # noqa: F401 (kept for cross-reference)
from src.master.scope import SCOPE_LEGACY_PRE_NATIONAL, SCOPE_VALUES, LEGACY_PRE_NATIONAL_CUTOFF
from src.scoring.research_scoring import AD_SIGNAL_NAMES
from src.repository.errors import BackendNotSupportedError

_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ASCII_FOLD_TABLE = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)
_ASCII_FOLD_SQL = f"translate({{col}}, '{_ASCII_UPPER}', '{_ASCII_LOWER}')"


def _ascii_fold(text):
    """Matches SQLite's actual LIKE case-folding scope exactly: ASCII A-Z/a-z only."""
    return text.translate(_ASCII_FOLD_TABLE)


UNSUPPORTED_FIELDS = (
    "mhlw_official_departments", "crestix_sales_departments", "mhlw_departments",
    "hp_treatment_categories", "research_status", "sales_pairs",
    "sales_tiers", "sales_confidence", "exclude_human_review",
)

POST_FILTER_FIELDS = ("effective_ranks", "site_types", "municipalities", "production_companies")

_AD_NAMES_SQL = ",".join("'" + n.replace("'", "''") + "'" for n in AD_SIGNAL_NAMES)


def raise_if_unsupported(filters):
    hit = [name for name in UNSUPPORTED_FIELDS if getattr(filters, name)]
    if hit:
        raise BackendNotSupportedError(
            "Supabase adapterは次のfilterに未対応です(Stage4-Aの範囲外: SSOTが未移行、"
            "またはfilter DSL翻訳を未実装。SQLite backendを使用してください): " + ", ".join(hit)
        )


def needs_post_filter(filters):
    return bool(
        filters.effective_ranks or filters.site_types or filters.municipalities
        or filters.production_companies
    )


def _minus_one_year(iso_date):
    from datetime import date
    d = date.fromisoformat(iso_date)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:
        return d.replace(year=d.year - 1, day=28).isoformat()


def clauses(filters, as_of=None):
    """Mirrors src.master.filters.clauses() field-for-field, Postgres dialect only.
    Returns [(label, sql_with_%s_placeholders, [args])]. Raises for UNSUPPORTED_FIELDS;
    silently skips POST_FILTER_FIELDS (callers must apply those separately in Python).
    """
    from src.utils.date_utils import today_japan
    raise_if_unsupported(filters)
    day = (as_of or today_japan()).isoformat()
    steps = []

    def add(label, sql, *args):
        steps.append((label, sql, list(args)))

    if filters.scope not in SCOPE_VALUES:
        raise ValueError("対象データ（scope）の指定を確認してください。")
    if filters.scope == SCOPE_LEGACY_PRE_NATIONAL:
        add("既存営業リスト（全国append前）", "first_seen_at<%s", LEGACY_PRE_NATIONAL_CUTOFF)
    # effective_json is PostgreSQL `text`, never `jsonb` (Stage3 kept it that way deliberately:
    # some rows contain a NUL-character JSON escape inside a string value -- valid JSON, valid SQLite
    # TEXT, but jsonb rejects it outright with "unsupported Unicode escape sequence", which would
    # make this *unconditional* base clause fail on every count()/query() call). A plain substring
    # regex reads the one short, quote-free enum value we need without ever parsing the document,
    # so it is immune to that. (production_companies, which also reads effective_json but needs a
    # whole array, is handled as a Python post-filter instead -- see needs_post_filter().)
    add(
        "病院・センター除外（営業対象外）",
        "NOT (exclude_reason IN ('hospital','center') "
        "OR COALESCE(substring(effective_json from '\"facility_type\":\"([^\"]*)\"'),'')='病院' "
        "OR clinic_name LIKE '%%病院%%' OR clinic_name LIKE '%%センター%%')",
    )
    if filters.active_only:
        add("現存クリニック（一覧基準日）", "active=true")
    if filters.hp_only:
        add("HP確認済み", "hp_status='VERIFIED' AND hp_url<>''")
    if filters.recent_only:
        add("指定年月日から10年以内", "designation_date<=%s AND recent_until>=%s", day, day)
    if filters.age_min is not None:
        if not 0 <= filters.age_min <= 1:
            raise ValueError("年齢確率は0〜100%で指定してください。")
        add(f"59歳以下確率{filters.age_min:.0%}以上", "age_probability>=%s", filters.age_min)
    for values, col, label in [(filters.ranks, "hp_rank", "旧HPランク"), (filters.prefectures, "prefecture", "都道府県"),
                                (filters.medical_types, "medical_type", "医科・歯科"), (filters.hot, "hot_status", "アツさ")]:
        if values:
            add(label, f"{col} IN ({','.join('%s' for _ in values)})", *values)
    for values, col, label in [(filters.departments, "departments_json", "診療科"), (filters.treatments, "treatments_json", "治療カテゴリ")]:
        if values:
            add(label, f"EXISTS(SELECT 1 FROM jsonb_array_elements_text({col}) v WHERE v IN ({','.join('%s' for _ in values)}))", *values)
    if filters.signals:
        add("広告・集客施策", f"EXISTS(SELECT 1 FROM jsonb_array_elements_text(signals_json) v WHERE v IN ({','.join('%s' for _ in filters.signals)}))", *filters.signals)
    if filters.ad_min:
        add(f"広告・集客施策{filters.ad_min}個以上",
            f"(SELECT count(DISTINCT v) FROM jsonb_array_elements_text(signals_json) v WHERE v IN ({_AD_NAMES_SQL}))>=%s",
            filters.ad_min)
    # production_companies: applied as a Python post-filter (see module docstring) -- never
    # translated to SQL, so intentionally no `add(...)` here.
    if filters.signal_min:
        add(f"集客投資シグナル{filters.signal_min}個以上", "signal_count>=%s", filters.signal_min)
    if filters.owner_equal != "指定なし":
        add("開設者＝管理者", "owner_equal=%s", filters.owner_equal == "一致のみ")
    if filters.uuid_mode != "指定なし":
        add("既存UUID", "uuid<>''" if filters.uuid_mode == "あり" else "uuid=''")
    if filters.new_only:
        add("前回更新から追加", "is_new=true")
    if filters.recent_opening:
        add("新規開業候補（1年以内・新規指定）", "designation_date BETWEEN %s AND %s AND registration_reason LIKE '%%新規%%'",
            _minus_one_year(day), day)
    if filters.maps_confirmed_only:
        add("Google Maps掲載確認済み", "maps_presence_status IN ('MAPS_MATCHED_WEBSITE','MAPS_MATCHED_NO_WEBSITE')")
    if filters.maps_website_only:
        add("Google Maps HP取得済み", "maps_presence_status='MAPS_MATCHED_WEBSITE' AND maps_website_url<>''")
    if filters.keyword.strip():
        escaped = filters.keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        digits = "".join(x for x in filters.keyword if x.isdigit())
        # ASCII-only fold on both sides (see module docstring) -- NOT ILIKE, which over-matches
        # relative to SQLite's actual LIKE behavior for non-ASCII "case-like" characters.
        add("医院検索",
            f"({_ASCII_FOLD_SQL.format(col='clinic_name')} LIKE %s ESCAPE '\\' OR "
            f"{_ASCII_FOLD_SQL.format(col='phone_norm')} LIKE %s ESCAPE '\\' OR uuid=%s)",
            "%" + _ascii_fold(escaped) + "%",
            ("%" + digits + "%") if digits else "__NO_PHONE__",
            filters.keyword.strip())
    return steps


def where(filters, as_of=None):
    steps = clauses(filters, as_of)
    sql = " AND ".join(["merged_into IS NULL", "merge_hold=false"] + [s for _, s, _ in steps])
    args = [a for _, _, args in steps for a in args]
    return sql, args
