from collections import defaultdict
from dataclasses import dataclass
import math
import re
from src.utils.config import read_config, ROOT
from src.utils.date_utils import parse_year, today_japan


@dataclass
class AgeEstimate:
    registration_year: int | None
    median: int | None
    lower: int | None
    upper: int | None
    probability: float | None
    young: bool | None
    reason: str


class AgeEstimator:
    """離散分布を畳み込む。3要因の独立性・尾部分布は設定上の仮定。"""
    def __init__(self, config=None):
        self.config = config or read_config(ROOT / "config/age_model.yml")
        c = self.config
        self.limit = int(c["age_limit"])
        self.threshold = float(c["threshold"])
        if not 0 <= self.threshold <= 1:
            raise ValueError("年齢閾値は0〜1で指定してください。")
        self.quantiles = c["interval_quantiles"]
        if len(self.quantiles) != 2 or not 0 <= self.quantiles[0] < .5 < self.quantiles[1] <= 1:
            raise ValueError("年齢区間は中央値を挟む2つの分位点で指定してください。")
        self.pmf = defaultdict(float)
        entry = self._expand(c["medical_school_entry_age_distribution"], "age")
        school = self._expand(c["medical_school_delay_distribution"], "school_delay")
        exam = self._expand(c["national_exam_delay_distribution"], "exam_delay")
        years = int(c["school_years"])
        if years < 1 or min(entry) < 0:
            raise ValueError("入学年齢・在学年数を確認してください。")
        for age, pa in entry.items():
            for sd, ps in school.items():
                for nd, pn in exam.items():
                    self.pmf[age + years + sd + nd] += pa * ps * pn

    def _normalize(self, values):
        if not values or any(not math.isfinite(float(v)) or float(v) < 0 for v in values.values()):
            raise ValueError("確率には有限の0以上の数値を指定してください。")
        total = sum(float(v) for v in values.values())
        if total <= 0 or abs(total-1) > float(self.config["sum_tolerance"]):
            raise ValueError("確率分布の合計を1にしてください（丸め差のみ許容）。")
        return {k: float(v)/total for k, v in values.items()}

    def _expand(self, distribution, kind):
        expanded = defaultdict(float)
        for label, probability in self._normalize(distribution).items():
            match = re.fullmatch(r"(?:age|delay)_(\d+)(_or_more)?", label)
            if not match:
                raise ValueError(f"年齢モデルのカテゴリ形式が不正です: {label}")
            minimum = int(match[1])
            if not match[2]:
                expanded[minimum] += probability
                continue
            tail_key = label if kind == "age" else kind + label[len("delay"):]
            tail = self.config["tail_distributions"].get(tail_key)
            if tail is None:
                raise ValueError(f"末尾カテゴリの内訳がありません: {tail_key}")
            for value, fraction in self._normalize(tail).items():
                if int(value) < minimum:
                    raise ValueError("末尾カテゴリの下限より小さい値が含まれています。")
                expanded[int(value)] += probability * fraction
        return dict(expanded)

    def estimate(self, registration_year, current_year=None):
        current_year = current_year or today_japan().year
        year = parse_year(registration_year)
        if year is None or not int(self.config["minimum_registration_year"]) <= year <= current_year:
            return AgeEstimate(None, None, None, None, None, None, "医籍登録年が未取得または不正")
        offset = current_year - year
        ages = sorted((offset + age, p) for age, p in self.pmf.items())
        def quantile(q):
            acc = 0
            for age, p in ages:
                acc += p
                if acc + 1e-12 >= q:
                    return age
            return ages[-1][0]
        probability = min(1., max(0., sum(p for age, p in ages if age <= self.limit)))
        young = probability + 1e-12 >= self.threshold
        relation = "以上" if young else "未満"
        reason = (f"仮定モデル{self.config['model_version']}による年単位推定。"
                  f"{self.limit}歳以下={probability:.2%}、閾値{self.threshold:.0%}{relation}。"
                  f"下限/上限は{self.quantiles[0]:.0%}/{self.quantiles[1]:.0%}分位。"
                  "実年齢・誕生日・世代別差・要因間相関は未反映")
        return AgeEstimate(year, quantile(.5), quantile(self.quantiles[0]), quantile(self.quantiles[1]),
                           probability, young, reason)
