"""UI件数とComdesk出力件数の差分の根本原因（artifacts/hp_rank_recalibration/ui_export_gap_analysis.csv）を
固定するテスト。src/master/filters.py の既存Filters/clauses()のみを対象とし、app_v2.py（Streamlit UI）
自体は変更していない・テストしていない（今回はdry run分析のみ、実装はまだ行っていない）。
"""
from src.master.filters import Filters, clauses


def _clause_sql(f):
    return " AND ".join(sql for _label, sql, _args in clauses(f))


def test_default_uuid_mode_does_not_restrict_by_uuid():
    """UIの通常カウント（uuid_mode未指定='指定なし'）はUUIDあり/なし両方を含む。"""
    sql = _clause_sql(Filters())
    assert "uuid" not in sql


def test_uuid_mode_ari_excludes_uuid_less_clinics():
    """app_v2.py:781が出力時だけ強制しているuuid_mode='あり'の実際の挙動。
    これがUI件数とCSV件数が食い違う唯一の原因（他の条件はreplace()でUIと同一のまま引き継がれる）。"""
    sql = _clause_sql(Filters(uuid_mode="あり"))
    assert "uuid<>''" in sql


def test_uuid_mode_nashi_would_restrict_to_uuid_less_only():
    sql = _clause_sql(Filters(uuid_mode="なし"))
    assert "uuid=''" in sql


def test_research_status_filter_only_applied_when_explicitly_selected():
    """Treatment研究状態は、ユーザーが明示的に選択しない限りfilterに現れない
    （=「未取得だからという理由だけで自動除外」されない）。"""
    unrestricted_sql = _clause_sql(Filters())
    assert "clinic_research_status" not in unrestricted_sql

    restricted_sql = _clause_sql(Filters(research_status=["NOT_RESEARCHED"]))
    assert "clinic_research_status" in restricted_sql


def test_hp_treatment_category_filter_only_applied_when_explicitly_selected():
    unrestricted_sql = _clause_sql(Filters())
    assert "treatment_category_name" not in unrestricted_sql

    restricted_sql = _clause_sql(Filters(hp_treatment_categories=["内視鏡"]))
    assert "treatment_category_name" in restricted_sql
    assert "CONFIRMED" in restricted_sql


def test_ui_count_filters_and_naive_export_filters_differ_only_by_uuid_mode():
    """app_v2.py:730-781の構成を再現: exportはfilters全体をreplace()で引き継ぎ、
    uuid_mode='あり'とsales_tiersからD除外だけを上書きする。この2つの差分のうち
    実質的な差（612件→408件のようなgap）を生むのはuuid_modeのみであることを固定する。"""
    ui_filters = Filters(sales_tiers=["A", "B", "C"], prefectures=["東京都"])
    # app_v2.py:781相当: dataclasses.replace(filters, sales_tiers=export_tiers, uuid_mode="あり")
    from dataclasses import replace
    export_filters = replace(ui_filters, sales_tiers=[t for t in ui_filters.sales_tiers if t != "D"], uuid_mode="あり")

    ui_sql = _clause_sql(ui_filters)
    export_sql = _clause_sql(export_filters)

    assert "uuid" not in ui_sql
    assert "uuid<>''" in export_sql
    # sales_tiers側はD抜きの時点でexport_tiers==ui_filters.sales_tiersなので無変化（今回のケースでは差が出ない）。
    assert export_filters.sales_tiers == ui_filters.sales_tiers
