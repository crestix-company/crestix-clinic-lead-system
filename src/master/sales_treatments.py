"""営業用の標榜診療科×治療pairを、保存済みHP Research根拠から判定する。"""
from dataclasses import dataclass
from functools import lru_cache

from src.enrichment.treatment_taxonomy import load_taxonomy


VALID_EVIDENCE_SOURCES = frozenset({"HOME_MENU", "INTRO_MENU", "DEDICATED_PAGE"})
VALID_SUPPORT = frozenset({"EXACT", "ALIAS", "MISSING", "AMBIGUOUS"})
VALID_MATCH_MODES = frozenset({"CATEGORY", "KEYWORD"})

# 営業UIでは内部事情の「詳細不明」を見せず、旧taxonomyの広義カテゴリを単純な「内視鏡」と表示する。
# 保存済みtaxonomy/evidence自体は変更せず、表示・選択pairのみ正規化する。
DISPLAY_TREATMENT_LABELS = {
    ("消化器内科", "内視鏡（詳細不明）"): "内視鏡",
}

# 営業用Treatment Mapping: legacy taxonomy由来のpair(department,treatment)を、
# Treatment Research sidecar(clinic_treatment_research_final.treatment_category_name)の
# 対応カテゴリへ結び付ける。両軸はclinic_idベースのOR(UNION DISTINCT、二重カウントなし)で評価し、
# Treatment ResearchのCONFIRMEDを優先根拠とする。ここに無いpairはlegacy単独で評価する
# (sidecar側に対応する治療カテゴリが存在しない、または未確認のため)。
# 旧taxonomyの広義カテゴリ「内視鏡」はこの対応表に含めない
# (胃カメラ/大腸カメラへ無条件に振り分けないため、legacyカテゴリのまま残す)。
SIDECAR_TREATMENT_CATEGORY_BY_PAIR = {
    ("消化器内科", "胃カメラ"): "胃カメラ検査",
    ("消化器内科", "大腸カメラ"): "大腸カメラ検査",
    ("消化器内科", "大腸内視鏡"): "大腸カメラ検査",
    ("消化器内科", "大腸ポリープ切除"): "大腸ポリープ切除",
    ("消化器内科", "鎮静剤 内視鏡"): "鎮静内視鏡",
}


def sidecar_treatment_category(department, treatment):
    """pairに対応するTreatment Research sidecarの治療カテゴリ名。対応が無ければNone。"""
    return SIDECAR_TREATMENT_CATEGORY_BY_PAIR.get((department, treatment))


@dataclass(frozen=True)
class SalesTreatment:
    department: str
    treatment: str
    match_mode: str
    current_categories: tuple[str, ...]
    evidence_keywords: tuple[str, ...]
    support_status: str


@dataclass(frozen=True)
class PairMatch:
    matched: bool
    reason: str
    department: str
    treatment: str
    matched_category: str = ""
    matched_keyword: str = ""
    evidence_source: str = ""


@lru_cache(maxsize=1)
def sales_treatment_master():
    departments = load_taxonomy().get("crestix_sales_categories")
    if not isinstance(departments, dict):
        raise ValueError("営業治療マスタのdepartmentsを確認してください。")
    result = {}
    seen = set()
    for department, items in departments.items():
        if not isinstance(items, list):
            raise ValueError(f"{department}の営業治療一覧を確認してください。")
        parsed = []
        for item in items:
            required = {"treatment", "match_mode", "current_categories", "evidence_keywords", "support_status"}
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError(f"{department}の営業治療定義を確認してください。")
            raw_treatment = str(item["treatment"])
            treatment = DISPLAY_TREATMENT_LABELS.get((department, raw_treatment), raw_treatment)
            key = (department, treatment)
            if key in seen:
                raise ValueError(f"営業治療が重複しています: {department} / {treatment}")
            seen.add(key)
            if item["match_mode"] not in VALID_MATCH_MODES or item["support_status"] not in VALID_SUPPORT:
                raise ValueError(f"営業治療の状態を確認してください: {department} / {treatment}")
            parsed.append(SalesTreatment(
                department=department,
                treatment=treatment,
                match_mode=item["match_mode"],
                current_categories=tuple(item["current_categories"] or ()),
                evidence_keywords=tuple(item["evidence_keywords"] or ()),
                support_status=item["support_status"],
            ))
        result[department] = tuple(parsed)
    return result


def treatment_definition(department, treatment):
    return next((item for item in sales_treatment_master().get(department, ())
                 if item.treatment == treatment), None)


def match_pair(clinic_departments, treatment_categories, treatment_evidence, department, treatment):
    """1 pairをfail-safeで判定する。source不明・医院名だけの根拠は採用しない。"""
    definition = treatment_definition(department, treatment)
    if definition is None:
        return PairMatch(False, "sales masterに存在しないpair", department, treatment)
    if department not in set(clinic_departments or ()):
        return PairMatch(False, "医院が標榜診療科を持たない", department, treatment)
    if definition.support_status == "MISSING":
        return PairMatch(False, "現行Researchで未対応", department, treatment)

    categories = set(treatment_categories or ())
    valid_evidence = [
        e for e in (treatment_evidence or ())
        if isinstance(e, dict) and e.get("source") in VALID_EVIDENCE_SOURCES
        and e.get("category") in categories
    ]
    if definition.match_mode == "CATEGORY":
        for evidence in valid_evidence:
            category = evidence.get("category", "")
            if category in definition.current_categories:
                return PairMatch(True, "HP由来category evidence一致", department, treatment,
                                 category, evidence.get("keyword", ""), evidence.get("source", ""))
        return PairMatch(False, "有効なHP由来category evidenceがない", department, treatment)

    for evidence in valid_evidence:
        if (evidence.get("category") in definition.current_categories
                and evidence.get("keyword") in definition.evidence_keywords):
            return PairMatch(True, "HP由来keyword evidence一致", department, treatment,
                             evidence.get("category", ""), evidence.get("keyword", ""), evidence.get("source", ""))
    return PairMatch(False, "有効なHP由来keyword evidenceがない", department, treatment)


def matching_pairs(clinic_departments, treatment_categories, treatment_evidence, selected_pairs):
    """選択pairをOR評価し、成立したpairをすべて返す。"""
    matches = []
    for department, treatment in selected_pairs or ():
        result = match_pair(clinic_departments, treatment_categories, treatment_evidence, department, treatment)
        if result.matched:
            matches.append(result)
    return matches


def prune_selected_pairs(selected_pairs, selected_departments):
    """UIの診療科解除時に、その科の選択治療を除くpure helper。"""
    allowed = set(selected_departments or ())
    return [(department, treatment) for department, treatment in (selected_pairs or ())
            if department in allowed]
