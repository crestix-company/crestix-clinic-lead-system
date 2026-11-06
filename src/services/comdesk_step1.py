"""One Step1 operation: match two Comdesk exports and prepare HP-rank updates.

CSV contents determine authority. A missing HP rank is not a deletion request.
"""
from __future__ import annotations

from types import SimpleNamespace
import pandas as pd


def rank_only_table(clinic_table):
    required = {"UUID", "HPランク", "URL", "メールアドレス"}
    if not required.issubset(clinic_table.headers):
        raise ValueError("医院情報CSVにUUID・HPランク・URL・メールアドレスが必要です。")
    df = clinic_table.data
    if list(df.columns) != list(clinic_table.headers):
        df = df.copy()
        df.columns = clinic_table.headers
    result = pd.DataFrame({
        "UUID": df["UUID"].fillna("").astype(str).str.strip(),
        "HPランク": df["HPランク"].fillna("").astype(str).str.strip(),
        "URL": [""] * len(df),
        "メールアドレス": [""] * len(df),
    })
    return SimpleNamespace(headers=list(result.columns), data=result)
