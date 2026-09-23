from dataclasses import dataclass
from collections import defaultdict
from rapidfuzz import fuzz, process
from src.normalizer.phone import normalize_phone
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.address import normalize_address


@dataclass
class MatchResult:
    record: dict | None
    method: str
    score: float
    reason: str
    candidates: list[str]


class KouseikyokuMatcher:
    def __init__(self, master, config):
        self.config = config
        self.records = master.fillna("").astype(str).to_dict("records")
        self.phones, self.names, self.addresses = defaultdict(list), defaultdict(list), defaultdict(list)
        self.loose_names = []
        self.norm = []
        for i, r in enumerate(self.records):
            p, n, a = normalize_phone(r.get("phone")), normalize_clinic_name(r.get("clinic_name")), normalize_address(r.get("address"))
            self.norm.append((p, n, a))
            self.loose_names.append(normalize_clinic_name(r.get("clinic_name"), loose=True))
            for value, index in ((p, self.phones), (n, self.names), (a, self.addresses)):
                if value:
                    index[value].append(i)

    def match(self, phone="", clinic_name="", address=""):
        p, n, a = normalize_phone(phone), normalize_clinic_name(clinic_name), normalize_address(address)
        def ids(indices):
            return [self.records[i].get("clinic_id") or f"マスタ行{i+2}" for i in indices][:10]
        def accepted(i, method, score=100):
            return MatchResult(self.records[i].copy(), method, score, "", ids([i]))
        def ambiguous(indices, method, message):
            return MatchResult(None, method, 0, message, ids(indices))
        if p and p in self.phones:
            pool = self.phones[p]
            if len(pool) == 1:
                i = pool[0]
                np, nn, na = self.norm[i]
                if n and fuzz.ratio(n, nn) < 60 and (not a or fuzz.ratio(a, na) < 90):
                    return ambiguous(pool, "電話番号一致・矛盾", "電話番号は一致するが医院名・住所の裏付けが不足")
                return accepted(i, "電話番号一致")
            exact = [i for i in pool if n and self.norm[i][1] == n and a and self.norm[i][2] == a]
            if len(exact) == 1:
                return accepted(exact[0], "電話番号＋医院名＋住所一致")
            return ambiguous(pool, "電話番号重複", "同じ電話番号に複数施設。照合先を特定できない")
        if n and n in self.names:
            pool = self.names[n]
            exact = [i for i in pool if a and self.norm[i][2] == a]
            if len(exact) == 1:
                return accepted(exact[0], "医院名＋住所一致")
            if len(pool) == 1:
                i = pool[0]
                if a and self.norm[i][2] and fuzz.ratio(a, self.norm[i][2]) < 80:
                    return ambiguous(pool, "医院名一致・住所矛盾", "同名医院または移転の可能性。住所を確認")
                if p and self.norm[i][0] and p != self.norm[i][0]:
                    return ambiguous(pool, "医院名一致・電話矛盾", "医院名のみ一致。電話番号の相違を確認")
                return accepted(i, "医院名正規化一致")
            return ambiguous(pool, "医院名重複", "同名医院が複数あり住所で特定できない")
        if a and a in self.addresses:
            pool = self.addresses[a]
            exact = [i for i in pool if n and normalize_clinic_name(clinic_name, True) == self.loose_names[i]]
            if len(exact) == 1:
                return accepted(exact[0], "住所＋医院名表記揺れ一致", 98)
            # 同じビルだけを根拠に別の医院を結合しない。
            if not n:
                return ambiguous(pool, "住所一致", "住所のみ一致。医院名または電話番号の裏付けが必要")
        if n and self.records and (a or p):
            hits = process.extract(normalize_clinic_name(clinic_name, True), self.loose_names,
                                   scorer=fuzz.ratio, limit=self.config["max_candidates"],
                                   score_cutoff=self.config["minimum_name_similarity"])
            ranked = []
            for _, name_score, i in hits:
                mp, _, ma = self.norm[i]
                address_score = fuzz.ratio(a, ma) if a and ma else 0
                phone_score = fuzz.ratio(p, mp) if len(p) >= 9 and len(mp) >= 9 else 0
                support = max(address_score, phone_score)
                if support < self.config["minimum_address_similarity"]:
                    continue
                weight = self.config["fuzzy_name_weight"]
                ranked.append((weight * name_score + (1-weight) * support, i, address_score))
            ranked.sort(reverse=True)
            if ranked:
                score, i, address_score = ranked[0]
                unique = len(ranked) == 1 or score-ranked[1][0] >= self.config["ambiguity_margin"]
                # 電話の1桁差だけでは自動結合しない。氏名＋電話類似は候補まで。
                if score >= self.config["fuzzy_threshold"] and unique and address_score >= self.config["minimum_address_similarity"]:
                    return accepted(i, "医院名＋住所類似一致", round(score, 2))
                return ambiguous([i for _, i, _ in ranked], "類似候補", "類似度または候補差が不足。照合候補を確認")
        return MatchResult(None, "未一致", 0, "厚生局データとの一致なし。欠落・表記・更新時点を確認", [])
