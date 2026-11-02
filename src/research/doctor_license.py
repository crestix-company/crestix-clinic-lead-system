from dataclasses import dataclass
from typing import Protocol
import pandas as pd
from bs4 import BeautifulSoup
from src.normalizer.clinic_name import normalize_person
from src.normalizer.phone import normalize_phone
from src.utils.date_utils import parse_year, parse_date, today_japan
from src.utils.cache import read_cache, merge_cache

LICENSE_COLUMNS = ["doctor_name", "registration_year", "source", "checked_at", "confidence", "note",
                   "gender", "profession", "candidate_count", "candidate_id", "clinic_id", "phone", "verified"]


def is_true(value):
    return str(value).strip().lower() in {"true", "1", "yes", "はい", "確認済み"}


@dataclass
class LicenseResult:
    year: int | None
    status: str
    reason: str
    source: str = ""
    checked_at: str = ""
    confidence: float = 0.


class LicenseAdapter(Protocol):
    def search(self, doctor_name: str) -> pd.DataFrame:
        """アクセス制限を回避しない、利用可能なデータ提供元の実装用。"""
        ...


def parse_license_html(html, source="手動保存した公式検索結果HTML"):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select('[class*="captcha"], [id*="captcha"]'):
        raise ValueError("検索結果を含まない認証画面です。結果を確認してCSVに入力してください。")
    aliases = {"氏名": "doctor_name", "性別": "gender", "職種": "profession",
               "登録年": "registration_year", "医籍登録年": "registration_year"}
    records = []
    for table in soup.find_all("table"):
        columns = None
        for tr in table.find_all("tr"):
            cells = ["".join(c.stripped_strings).replace(" ", "").replace("　", "") for c in tr.find_all(["td", "th"], recursive=False)]
            if "氏名" in cells and any(k in cells for k in ["登録年", "医籍登録年"]):
                columns = [aliases.get(c, "") for c in cells]
                continue
            if columns and len(cells) == len(columns):
                record = {k: v for k, v in zip(columns, cells) if k}
                if record.get("doctor_name") and parse_year(record.get("registration_year")):
                    record.update(source=source, checked_at=today_japan().isoformat(), confidence="0.9", note="保存HTMLから抽出")
                    records.append(record)
    if not records:
        raise ValueError("氏名・登録年の表を認識できません。手動CSVに入力してください。")
    result = pd.DataFrame(records).drop_duplicates()
    names = result["doctor_name"].map(normalize_person)
    result["candidate_count"] = names.map(names.value_counts()).astype(str)
    return result.reindex(columns=LICENSE_COLUMNS, fill_value="")


class DoctorLicenseCache:
    def __init__(self, path=None, frame=None, minimum_confidence=.8, adapter=None):
        self.path = path
        self.minimum_confidence = minimum_confidence
        self.adapter = adapter
        base = read_cache(path, LICENSE_COLUMNS) if path else pd.DataFrame(columns=LICENSE_COLUMNS)
        self.frame = pd.concat([base, frame], ignore_index=True).fillna("").astype(str).drop_duplicates() if frame is not None else base
        self.frame = self.frame.reindex(columns=LICENSE_COLUMNS, fill_value="")
        self.searched = set()

    def import_records(self, frame, persist=True):
        required = {"doctor_name", "registration_year", "source", "checked_at", "confidence"}
        if not required.issubset(frame.columns):
            raise ValueError("医籍CSVにはdoctor_name, registration_year, source, checked_at, confidenceが必要です。")
        self.frame = pd.concat([self.frame, frame], ignore_index=True).fillna("").astype(str).drop_duplicates()
        if persist and self.path:
            self.frame = merge_cache(self.path, self.frame, LICENSE_COLUMNS)

    def lookup(self, doctor_name, clinic_id="", phone=""):
        name = normalize_person(doctor_name)
        if not name:
            return LicenseResult(None, "未取得", "管理者氏名が空白")
        hits = self.frame[self.frame["doctor_name"].map(normalize_person) == name]
        if hits.empty and self.adapter and name not in self.searched:
            self.searched.add(name)
            found = self.adapter.search(doctor_name)
            if not found.empty:
                self.import_records(found)
            hits = self.frame[self.frame["doctor_name"].map(normalize_person) == name]
        if hits.empty:
            return LicenseResult(None, "未取得", "公式検索結果を手動CSVまたは保存HTMLで補完")
        # 同姓同名は、医院ID/電話と本人確認済みフラグの組でのみ解消。
        scope = hits[
            ((hits["clinic_id"] == clinic_id) & bool(clinic_id) |
             ((hits["phone"].map(normalize_phone) == normalize_phone(phone)) & bool(phone))) &
            hits["verified"].map(is_true)
        ]
        if not scope.empty:
            hits = scope
        else:
            hits = hits[(hits["clinic_id"] == "") & (hits["phone"] == "")]
        if hits.empty:
            return LicenseResult(None, "要確認", "同名医師のキャッシュは別医院に紐付いている")
        counts = pd.to_numeric(hits["candidate_count"], errors="coerce").fillna(1)
        if counts.gt(1).any() and scope.empty:
            return LicenseResult(None, "要確認", "同姓同名の候補が複数。本人特定後に医院IDとverifiedを入力")
        years = hits["registration_year"].map(parse_year)
        # 同一人物の再確認だけを統合。別候補は同年でも自動統合しない。
        candidate_ids = set(hits["candidate_id"]) - {""}
        if len(set(years)) != 1 or len(candidate_ids) > 1:
            return LicenseResult(None, "要確認", "同姓同名または医籍登録年の競合")
        professions = set(hits["profession"]) - {""}
        if professions - {"医師", "doctor", "physician"}:
            return LicenseResult(None, "要確認", "職種が医師であることを確認できない")
        hit = hits.sort_values("checked_at").iloc[-1]
        try:
            confidence = float(hit["confidence"])
        except ValueError:
            confidence = 0.
        if (not 0 <= confidence <= 1 or confidence < self.minimum_confidence or not hit["source"]
                or parse_date(hit["checked_at"][:10]) is None):
            return LicenseResult(None, "要確認", "医籍データの出典・確認日・信頼度が不足")
        year = parse_year(hit["registration_year"])
        return LicenseResult(year, "取得済み" if year else "未取得", hit["note"] or "医籍キャッシュと氏名を照合",
                             hit["source"], hit["checked_at"], confidence)
