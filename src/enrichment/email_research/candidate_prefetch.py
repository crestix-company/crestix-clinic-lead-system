"""Candidate sourceのpre-fetch例外判定。HTTP/API通信は行わない。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from src.enrichment.email_research.clinic_identity import _core_name_present, _location_contradiction

STATUS_DEFAULT = "DEFAULT"
STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL = "TRUSTED_PUBLIC_MEDICAL_DETAIL"
STATUS_CLINIC_RECRUITMENT_DETAIL = "CLINIC_RECRUITMENT_DETAIL"

# 掲載元が第三者であることと、掲載メールの所有者が第三者であることは別問題。
# 以下は医院単独detailに限りfetchを許可し、最終ownershipは取得後のGate Cで判定する。
_TRUSTED_MEDICAL_DIRECTORY_HOSTS = {
    "byoinnavi.jp", "www.byoinnavi.jp", "navii.jp", "www.navii.jp",
}

_PREFECTURE_CODES = {
    "北海道": "01", "青森県": "02", "岩手県": "03", "宮城県": "04", "秋田県": "05", "山形県": "06",
    "福島県": "07", "茨城県": "08", "栃木県": "09", "群馬県": "10", "埼玉県": "11", "千葉県": "12",
    "東京都": "13", "神奈川県": "14", "新潟県": "15", "富山県": "16", "石川県": "17", "福井県": "18",
    "山梨県": "19", "長野県": "20", "岐阜県": "21", "静岡県": "22", "愛知県": "23", "三重県": "24",
    "滋賀県": "25", "京都府": "26", "大阪府": "27", "兵庫県": "28", "奈良県": "29", "和歌山県": "30",
    "鳥取県": "31", "島根県": "32", "岡山県": "33", "広島県": "34", "山口県": "35", "徳島県": "36",
    "香川県": "37", "愛媛県": "38", "高知県": "39", "福岡県": "40", "佐賀県": "41", "長崎県": "42",
    "熊本県": "43", "大分県": "44", "宮崎県": "45", "鹿児島県": "46", "沖縄県": "47",
}
_PUBLIC_MEDICAL_HOSTS = {"www.iryou.teikyouseido.mhlw.go.jp", "iryou.teikyouseido.mhlw.go.jp"}
_LEGACY_PUBLIC_MEDICAL_HOST_RE = re.compile(r"^(?:www\.)?qq\.pref\.([a-z0-9-]+)\.lg\.jp$", re.I)
_LEGACY_PUBLIC_DETAIL_RE = re.compile(r"/pb_md_info/([A-Za-z0-9_-]+)/?$", re.I)
_PREFECTURE_SLUGS = {
    "北海道": "hokkaido", "青森県": "aomori", "岩手県": "iwate", "宮城県": "miyagi",
    "秋田県": "akita", "山形県": "yamagata", "福島県": "fukushima", "茨城県": "ibaraki",
    "栃木県": "tochigi", "群馬県": "gunma", "埼玉県": "saitama", "千葉県": "chiba",
    "東京都": "tokyo", "神奈川県": "kanagawa", "新潟県": "niigata", "富山県": "toyama",
    "石川県": "ishikawa", "福井県": "fukui", "山梨県": "yamanashi", "長野県": "nagano",
    "岐阜県": "gifu", "静岡県": "shizuoka", "愛知県": "aichi", "三重県": "mie",
    "滋賀県": "shiga", "京都府": "kyoto", "大阪府": "osaka", "兵庫県": "hyogo",
    "奈良県": "nara", "和歌山県": "wakayama", "鳥取県": "tottori", "島根県": "shimane",
    "岡山県": "okayama", "広島県": "hiroshima", "山口県": "yamaguchi", "徳島県": "tokushima",
    "香川県": "kagawa", "愛媛県": "ehime", "高知県": "kochi", "福岡県": "fukuoka",
    "佐賀県": "saga", "長崎県": "nagasaki", "熊本県": "kumamoto", "大分県": "oita",
    "宮崎県": "miyazaki", "鹿児島県": "kagoshima", "沖縄県": "okinawa",
}
_RECRUIT_RE = re.compile(r"recruit|recruitment|entry-forms?|採用|求人|応募|エントリー", re.I)
_NON_DETAIL_FILE_RE = re.compile(r"\.(?:pdf|csv|xlsx?|zip)$", re.I)
_FACILITY_SUFFIX_RE = re.compile(r"(?:クリニック|診療所|医院|病院|医療センター)$")


@dataclass(frozen=True)
class PrefetchAssessment:
    status: str = STATUS_DEFAULT
    allow_fetch: bool = False
    reason: str = ""


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zぁ-ゟァ-ヶ一-鿿]", "", unicodedata.normalize("NFKC", value or "")).lower()


def _address_match(address: str, evidence: str) -> bool:
    expected, actual = _compact(address), _compact(evidence)
    if not expected or not actual:
        return False
    if expected in actual:
        return True
    # 都道府県+市区町村+番地主要数値の一致を「十分強い住所一致」とする。
    expected_digits = "".join(re.findall(r"\d+", expected))
    return len(expected) >= 8 and expected[: min(8, len(expected))] in actual and expected_digits in actual


def _clinic_name_match(evidence: str, clinic_name: str) -> bool:
    """正式名一致に加え、法人名や診療科が挿入された公的登録名だけを保守的に許容する。"""
    if _core_name_present(evidence, clinic_name):
        return True
    core = _compact(clinic_name)
    base = _FACILITY_SUFFIX_RE.sub("", core)
    return len(base) >= 4 and base in _compact(evidence)


def _phone_match(phone: str, evidence: str) -> bool:
    expected = "".join(re.findall(r"\d", unicodedata.normalize("NFKC", phone or "")))
    actual = "".join(re.findall(r"\d", unicodedata.normalize("NFKC", evidence or "")))
    return len(expected) >= 9 and expected in actual


def assess_candidate_source(
    *, clinic_name: str, prefecture: str = "", city: str = "", address: str = "", phone: str = "",
    candidate_email: str = "",
    source_url: str = "", source_title: str = "", snippet_text: str = "",
) -> PrefetchAssessment:
    """governmentの一律許可はせず、単独detail/recruitmentの強い証拠のみ通す。"""
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return PrefetchAssessment(reason="source URLが不正")
    host, path = (parsed.hostname or "").lower(), parsed.path or ""
    evidence = f"{source_title} {snippet_text}"

    if _NON_DETAIL_FILE_RE.search(path):
        return PrefetchAssessment(reason="PDF/CSV等は単独医療機関detailとして扱わない")

    if host in _PUBLIC_MEDICAL_HOSTS:
        query = parse_qs(parsed.query)
        is_detail = path.endswith("/S2430/initialize") and all(query.get(k) for k in ("kikanCd", "kikanKbn", "prefCd"))
        if not is_detail:
            return PrefetchAssessment(reason="公的ソースだが単独医療機関detailではない")
        if not _clinic_name_match(source_title, clinic_name):
            return PrefetchAssessment(reason="公的detailの医院名が対象と一致しない")
        expected_code = _PREFECTURE_CODES.get(prefecture)
        if not expected_code or query["prefCd"][0].zfill(2) != expected_code:
            return PrefetchAssessment(reason="公的detailの都道府県が対象と一致しない")
        address_ok = _address_match(address, evidence)
        facility_id = query["kikanCd"][0].strip()
        # snippetに住所が出ない場合も、正式医院名がtitleと完全一致し、
        # 都道府県code+固有施設IDを持つ単独detailならfetchのみ許可する。
        if not address_ok and not facility_id:
            return PrefetchAssessment(reason="公的detailの住所/施設IDの強い一致がない")
        reason = "医院名・都道府県・単独detail・施設IDが一致"
        reason += "・住所一致" if address_ok else "。住所はfetch後に再確認"
        return PrefetchAssessment(STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL, True, reason)

    legacy_match = _LEGACY_PUBLIC_MEDICAL_HOST_RE.match(host)
    if legacy_match:
        detail_match = _LEGACY_PUBLIC_DETAIL_RE.search(path)
        if not detail_match:
            return PrefetchAssessment(reason="旧都道府県医療情報ソースだが単独detailではない")
        if not _clinic_name_match(source_title, clinic_name):
            return PrefetchAssessment(reason="旧公的detailの医院名が対象と一致しない")
        expected_slug = _PREFECTURE_SLUGS.get(prefecture)
        if not expected_slug or legacy_match.group(1).lower() != expected_slug:
            return PrefetchAssessment(reason="旧公的detailの都道府県が対象と一致しない")
        facility_id = detail_match.group(1).strip()
        if not facility_id:
            return PrefetchAssessment(reason="旧公的detailに施設IDがない")
        address_ok = _address_match(address, evidence)
        reason = "医院名・都道府県host・単独detail・施設IDが一致"
        reason += "・住所一致" if address_ok else "。住所はfetch後に再確認"
        return PrefetchAssessment(STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL, True, reason)

    if host in _TRUSTED_MEDICAL_DIRECTORY_HOSTS:
        if path in ("", "/"):
            return PrefetchAssessment(reason="医療機関情報サイトだが単独detailではない")
        if not _clinic_name_match(evidence, clinic_name):
            return PrefetchAssessment(reason="医療機関detailの医院名が対象と一致しない")
        compact_evidence = _compact(evidence)
        if prefecture and _compact(prefecture) not in compact_evidence:
            return PrefetchAssessment(reason="医療機関detailの都道府県を確認できない")
        if city and _compact(city) not in compact_evidence:
            return PrefetchAssessment(reason="医療機関detailの市区町村を確認できない")
        return PrefetchAssessment(
            STATUS_TRUSTED_PUBLIC_MEDICAL_DETAIL, True,
            "第三者運営の医療機関単独detailで医院名・都道府県・市区町村が一致",
        )

    recruit_evidence = f"{path} {source_title} {snippet_text} {candidate_email.rsplit('@', 1)[0]}"
    if _RECRUIT_RE.search(recruit_evidence) and _core_name_present(evidence, clinic_name):
        if _location_contradiction(evidence, prefecture):
            return PrefetchAssessment(reason="採用detailの所在地が対象医院と矛盾")
        location_supplied = any((prefecture, city, address, phone))
        location_matches = (
            bool(prefecture and _compact(prefecture) in _compact(evidence)),
            bool(city and _compact(city) in _compact(evidence)),
            _address_match(address, evidence),
            _phone_match(phone, evidence),
        )
        if location_supplied and not any(location_matches):
            return PrefetchAssessment(reason="採用detailで対象医院の所在地を確認できない")
        return PrefetchAssessment(
            STATUS_CLINIC_RECRUITMENT_DETAIL, True,
            "対象医院名・所在地・採用語彙が一致する医院専用採用detail（Role=RECRUIT後のみ）",
        )
    return PrefetchAssessment()
