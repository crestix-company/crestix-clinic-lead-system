"""Phase4.1 v2 JOIN用の追加正規化ヘルパー(READ ONLY dry-run専用・pure functions・no file I/O)。

既存の src.normalizer.clinic_name.normalize_clinic_name / src.normalizer.address.normalize_address
は変更せず再利用する。ここで追加するのは:
  - light_normalize_name(): 法人種別を除去しない軽量な名称正規化(normalized_clinic_name)
  - base_address(): ビル名・階数を除いた住所本体(normalized_base_address)
の2つのみ。raw値はどちらの関数も破壊しない(呼び出し側でraw列を別途保持すること)。
"""
import re
import unicodedata

from src.normalizer.clinic_name import VARIANTS, normalize_clinic_name

# 既存 normalize_clinic_name() は 医療法人/社会医療法人/特定医療法人/医療法人社団/医療法人財団 のみ対応
# (「...会」までを実データ確認済みの安全な範囲でまとめて除去する)。
# ユーザー指定の残り4種別(一般社団法人/公益社団法人/一般財団法人/公益財団法人)は、
# 同じ「...会」境界推測をせず、種別トークンだけを保守的に除去する
# (医院本体名称の一部である可能性がある団体名(例:「研医会」)を誤って削除しないため)。
EXTRA_CORP_PREFIX_RE = re.compile(r"^(?:一般社団法人|公益社団法人|一般財団法人|公益財団法人)")


def light_normalize_name(value):
    """NFKC+異体字統一+空白/記号除去+casefold のみ。法人種別プレフィックスは残す。"""
    text = unicodedata.normalize("NFKC", str(value or "")).translate(VARIANTS).strip().casefold()
    return re.sub(r"[\s()（）・･,，.．\-‐‑–—]", "", text)


def comparison_name(value):
    """比較専用の医院名。法人種別を保守的に除去する(raw値は変更しない・呼び出し側で別保持)。"""
    text = normalize_clinic_name(value)
    return EXTRA_CORP_PREFIX_RE.sub("", text)


def base_address(full_normalized_address, raw_address=""):
    """normalized_full_address(既存normalize_address済み)からビル名・階数を除いた住所本体を取り出す。

    優先順位:
      1. raw_addressに(全角/半角)空白があれば、最初の数字より後ろの最初の空白で分割し、
         その手前をbaseの入力とする(公的データでよく見られる「住所　ビル名」区切りを利用)。
      2. 空白が無ければ、full_normalized_addressの先頭(非数字)+最初の数字以降の
         連続する数字/ハイフン runを住所本体とみなし、それ以降(カナ/漢字が再開する箇所)を切り捨てる。

    既知の制限: 「３０－２階」のように番地とフロア番号がハイフンで地続きに書かれている場合、
    フロア番号がbaseに混入することがある(例: "...30-2"に2階の"2"が入る)。安全側の制限であり
    (baseがより限定的になるだけで誤結合のリスクは増えない)、README/reportに明記する。
    """
    if raw_address:
        from src.normalizer.address import normalize_address
        m = re.search(r"\d", raw_address)
        if m:
            rest = raw_address[m.start():]
            sp = re.search(r"[ 　]", rest)
            if sp:
                candidate = raw_address[:m.start() + sp.start()]
                return normalize_address(candidate)
    m = re.match(r"^([^\d]*\d[\d\-]*)", full_normalized_address or "")
    base = m.group(1) if m else (full_normalized_address or "")
    return base.rstrip("-")
