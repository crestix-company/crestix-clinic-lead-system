"""Rule C HP本人確認の純粋判定ルール（ネットワーク・DB非依存）。"""
import re


STALE_RE = re.compile(
    r"ドメイン.*(?:販売|オークション)|このドメインは(?:販売|取得)|for sale|parked domain|domain is for sale|"
    r"閉院(?:いたしました|しました|することになりました|のお知らせ)|"
    r"このサイトは表示できません|page not found|404 not found|"
    r"このウェブサイトは(?:現在)?(?:利用|表示)できません|サイト利用不可",
    re.I,
)
MOVE_RE = re.compile(r"移転(?:しました|いたしました|のお知らせ)", re.I)


def stale_or_move(text):
    if MOVE_RE.search(text or ""):
        return "MOVE"
    if STALE_RE.search(text or ""):
        return "STALE"
    return ""


def classify_identity(*, same_medical_type, stale_kind, cm_name, mhlw_name,
                      cm_address, mhlw_address, cm_phone, mhlw_phone):
    """保守的に判定する。住所だけの証拠ではRule Cを昇格させない。"""
    if not same_medical_type:
        return "HP_CONFLICT_REVIEW", "MEDICAL_TYPE_MISMATCH", False
    if stale_kind == "MOVE":
        return "HP_MOVE_REVIEW", "POSSIBLE_MOVE", False
    if stale_kind == "STALE":
        return "HP_STALE_REVIEW", "STALE_OR_INVALID_SITE", False
    name_any = cm_name or mhlw_name
    address_any = cm_address or mhlw_address
    phone_any = cm_phone or mhlw_phone
    if cm_name and mhlw_name and (address_any or phone_any):
        return "HP_IDENTITY_CONFIRMED", "BOTH_NAMES_AND_ADDRESS_OR_PHONE", False
    if name_any and phone_any and address_any:
        if cm_name != mhlw_name:
            return "HP_RENAME_CONFIRMED", "NAME_VARIANT_WITH_ADDRESS_AND_PHONE", True
        return "HP_IDENTITY_CONFIRMED", "NAME_WITH_ADDRESS_AND_PHONE", False
    if cm_name and not cm_address and mhlw_address:
        return "HP_MOVE_REVIEW", "POSSIBLE_MOVE", False
    if mhlw_name and not mhlw_address and cm_address:
        return "HP_MOVE_REVIEW", "POSSIBLE_MOVE", False
    return "HP_CONFLICT_REVIEW", "INSUFFICIENT_OR_CONFLICTING_IDENTITY_EVIDENCE", False
