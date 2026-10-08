"""Deterministic dental sales tags for prioritising dental lead lists.

This module is intentionally separate from the medical Treatment taxonomy.
It classifies only records whose medical_type is 歯科, keeps all matched tags,
and exposes a stable priority group used by sales list ordering.

Human review is stored separately; auto classification never overwrites a human label.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Iterable


CLASSIFIER_VERSION = "dental-sales-tags-v1.0.0"
AUTO_CONFIRMED = "CONFIRMED"
AUTO_REVIEW = "REVIEW"

OFFER_CUES = re.compile(
    r"当院|当クリニック|当医院|診療|治療|施術|料金|費用|メニュー|対応|"
    r"行って(?:います|おります)?|実施|提供|取り扱|予約|相談|専門"
)


@dataclass(frozen=True)
class DentalTagDefinition:
    code: str
    label: str
    priority_group: int
    sort_order: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class DentalTagEvidence:
    tag_code: str
    tag_label: str
    priority_group: int
    sort_order: int
    auto_status: str
    confidence: float
    source: str
    matched_alias: str
    evidence_text: str
    rule_id: str
    classifier_version: str = CLASSIFIER_VERSION

    def as_dict(self):
        return asdict(self)


TAG_DEFINITIONS = (
    DentalTagDefinition(
        "ALIGNER_INVISALIGN", "マウスピース矯正 / インビザライン", 1, 10,
        ("インビザライン", "invisalign", "マウスピース矯正", "クリアアライナー", "アライナー矯正"),
    ),
    DentalTagDefinition(
        "ORTHODONTICS_GENERAL", "矯正歯科全般", 2, 20,
        ("矯正歯科", "歯列矯正", "矯正治療", "ワイヤー矯正", "成人矯正", "小児矯正"),
    ),
    DentalTagDefinition(
        "ALL_ON_4", "All-on-4", 3, 30,
        ("all-on-4", "all on 4", "allon4", "オールオン4", "オールオンフォー"),
    ),
    DentalTagDefinition(
        "IMPLANT_GENERAL", "インプラント全般", 4, 40,
        ("インプラント治療", "口腔インプラント", "インプラント"),
    ),
    DentalTagDefinition(
        "CERAMIC", "セラミック治療", 5, 50,
        ("セラミック治療", "オールセラミック", "セラミッククラウン", "セラミックインレー", "セラミック"),
    ),
    DentalTagDefinition(
        "AESTHETIC_GENERAL", "審美歯科全般", 6, 60,
        ("審美歯科", "審美治療", "審美補綴"),
    ),
    DentalTagDefinition(
        "ORAL_SURGERY", "口腔外科", 7, 70,
        ("歯科口腔外科", "口腔外科"),
    ),
    DentalTagDefinition(
        "WHITENING", "ホワイトニング", 7, 71,
        ("オフィスホワイトニング", "ホームホワイトニング", "デュアルホワイトニング", "ホワイトニング"),
    ),
    DentalTagDefinition(
        "ZIRCONIA", "ジルコニア", 7, 72,
        ("ジルコニアクラウン", "ジルコニアセラミック", "ジルコニア"),
    ),
    DentalTagDefinition(
        "LAMINATE_VENEER", "ラミネートベニア", 7, 73,
        ("ラミネートベニア", "ラミネートべニア"),
    ),
    DentalTagDefinition(
        "PRIVATE_DENTURE", "入れ歯・義歯（自費）", 8, 80,
        ("自費入れ歯", "自費の入れ歯", "自費義歯", "ノンクラスプデンチャー", "金属床義歯"),
    ),
    DentalTagDefinition(
        "WISDOM_TOOTH", "親知らず", 8, 81,
        ("親知らず", "親不知", "智歯抜歯", "智歯"),
    ),
    DentalTagDefinition(
        "ROOT_CANAL", "根管治療", 8, 82,
        ("精密根管治療", "マイクロスコープ根管", "歯内療法", "根管治療"),
    ),
    DentalTagDefinition(
        "GENERAL_DENTISTRY", "一般歯科", 9, 90,
        ("一般歯科",),
    ),
    DentalTagDefinition(
        "CLEANING", "クリーニング", 9, 91,
        ("pmtc", "歯面清掃", "クリーニング"),
    ),
    DentalTagDefinition(
        "CHECKUP", "定期検診", 9, 92,
        ("定期検診", "定期健診", "歯科検診", "歯科健診", "メンテナンス"),
    ),
    DentalTagDefinition(
        "CARIES", "虫歯治療", 9, 93,
        ("虫歯治療", "むし歯治療", "う蝕治療", "齲蝕治療"),
    ),
)

DEFINITION_BY_CODE = {item.code: item for item in TAG_DEFINITIONS}

# A specific treatment implies its broader sales category as well.
PARENT_TAG = {
    "ALIGNER_INVISALIGN": "ORTHODONTICS_GENERAL",
    "ALL_ON_4": "IMPLANT_GENERAL",
    "CERAMIC": "AESTHETIC_GENERAL",
    "WHITENING": "AESTHETIC_GENERAL",
    "ZIRCONIA": "AESTHETIC_GENERAL",
    "LAMINATE_VENEER": "AESTHETIC_GENERAL",
}


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower().replace("–", "-").replace("—", "-")


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    if isinstance(value, tuple):
        return [str(x) for x in value if str(x).strip()]
    if not value:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return [str(x) for x in parsed if str(x).strip()]
        return [stripped]
    return [str(value)]


def _page_payload(page) -> dict:
    if isinstance(page, dict):
        raw = page.get("page_json", page)
    else:
        raw = page
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {"text": raw}
    return raw if isinstance(raw, dict) else {}


def _alias_match(text: str, aliases: Iterable[str]):
    normalized = _normalize(text)
    for alias in aliases:
        if _normalize(alias) in normalized:
            return alias
    return ""


def _context_excerpt(text: str, alias: str, radius: int = 100) -> str:
    if not text or not alias:
        return ""
    lower, needle = text.lower(), alias.lower()
    index = lower.find(needle)
    if index < 0:
        return text[: 2 * radius]
    start = max(0, index - radius)
    end = min(len(text), index + len(alias) + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _candidate(
    definition: DentalTagDefinition,
    *,
    status: str,
    confidence: float,
    source: str,
    alias: str,
    evidence: str,
    rule_id: str,
) -> DentalTagEvidence:
    return DentalTagEvidence(
        definition.code,
        definition.label,
        definition.priority_group,
        definition.sort_order,
        status,
        float(confidence),
        source,
        alias,
        re.sub(r"\s+", " ", str(evidence or "")).strip()[:500],
        rule_id,
    )


def _best(existing: DentalTagEvidence | None, new: DentalTagEvidence) -> DentalTagEvidence:
    if existing is None:
        return new
    status_rank = {AUTO_REVIEW: 0, AUTO_CONFIRMED: 1}
    old_key = (status_rank.get(existing.auto_status, -1), existing.confidence)
    new_key = (status_rank.get(new.auto_status, -1), new.confidence)
    return new if new_key > old_key else existing


def classify_dental_sales_tags(record: dict, hp_pages=()) -> list[DentalTagEvidence]:
    """Return deterministic auto tags for one clinic.

    Existing clinics can be classified from medical_type/name/departments alone.
    Verified HP pages add specific treatment tags later without replacing human review labels.
    """
    if str(record.get("medical_type") or "") != "歯科":
        return []

    found: dict[str, DentalTagEvidence] = {}

    def add(item: DentalTagEvidence):
        found[item.tag_code] = _best(found.get(item.tag_code), item)

    # Every active dental clinic remains exportable at the lowest priority even before HP research.
    general = DEFINITION_BY_CODE["GENERAL_DENTISTRY"]
    add(_candidate(
        general, status=AUTO_CONFIRMED, confidence=.70, source="MEDICAL_TYPE",
        alias="歯科", evidence="medical_type=歯科", rule_id="DENTAL_MEDICAL_TYPE_FALLBACK",
    ))

    name = str(record.get("clinic_name") or "")
    departments = _as_list(record.get("departments_json") or record.get("departments"))

    # Strong structural metadata signals. Clinic-name matching is intentionally limited to
    # distinctive dental service labels rather than generic treatment words.
    metadata_texts = [("CLINIC_NAME", name, .94)]
    metadata_texts.extend(("DEPARTMENT", value, .97) for value in departments)
    metadata_codes = {
        "ALIGNER_INVISALIGN", "ORTHODONTICS_GENERAL", "ALL_ON_4",
        "IMPLANT_GENERAL", "AESTHETIC_GENERAL", "ORAL_SURGERY",
    }
    for source, text, confidence in metadata_texts:
        for definition in TAG_DEFINITIONS:
            if definition.code not in metadata_codes:
                continue
            alias = _alias_match(text, definition.aliases)
            if alias:
                add(_candidate(
                    definition, status=AUTO_CONFIRMED, confidence=confidence,
                    source=source, alias=alias, evidence=text,
                    rule_id=f"{source}_{definition.code}",
                ))

    # Verified official HP pages: title/headings are structural evidence; body text needs an
    # offer-context cue to auto-confirm. Plain mentions stay REVIEW so training labels can teach
    # the next rule version instead of silently inflating precision.
    for raw_page in hp_pages or ():
        page = _page_payload(raw_page)
        title = str(page.get("title") or "")
        headings = str(page.get("headings") or "")
        body = str(page.get("text") or "")
        url = str(page.get("url") or (raw_page.get("url") if isinstance(raw_page, dict) else "") or "")
        structural = " ".join(x for x in (title, headings) if x)
        for definition in TAG_DEFINITIONS:
            alias = _alias_match(structural, definition.aliases)
            if alias:
                add(_candidate(
                    definition, status=AUTO_CONFIRMED, confidence=.99,
                    source="HP_HEADING", alias=alias,
                    evidence=f"{title} {headings} {url}",
                    rule_id=f"HP_HEADING_{definition.code}",
                ))
                continue
            alias = _alias_match(body, definition.aliases)
            if not alias:
                continue
            excerpt = _context_excerpt(body, alias)
            offered = bool(OFFER_CUES.search(excerpt))
            add(_candidate(
                definition,
                status=AUTO_CONFIRMED if offered else AUTO_REVIEW,
                confidence=.95 if offered else .75,
                source="HP_BODY",
                alias=alias,
                evidence=f"{excerpt} {url}",
                rule_id=(
                    f"HP_BODY_OFFER_{definition.code}"
                    if offered else f"HP_BODY_MENTION_{definition.code}"
                ),
            ))

    # Add broader parent tags while preserving the exact high-priority tag.
    for child_code, parent_code in PARENT_TAG.items():
        child = found.get(child_code)
        if not child:
            continue
        parent = DEFINITION_BY_CODE[parent_code]
        add(_candidate(
            parent,
            status=child.auto_status,
            confidence=max(.0, child.confidence - .01),
            source="DERIVED",
            alias=child.tag_label,
            evidence=f"{child.tag_label} から派生",
            rule_id=f"DERIVED_{child_code}_TO_{parent_code}",
        ))

    return sorted(found.values(), key=lambda item: (item.priority_group, item.sort_order, item.tag_code))


def primary_dental_sales_tag(tags: Iterable[DentalTagEvidence]):
    confirmed = [item for item in tags if item.auto_status == AUTO_CONFIRMED]
    if not confirmed:
        return None
    return min(confirmed, key=lambda item: (item.priority_group, item.sort_order, item.tag_code))


def tag_definition_rows():
    return [asdict(item) for item in TAG_DEFINITIONS]
