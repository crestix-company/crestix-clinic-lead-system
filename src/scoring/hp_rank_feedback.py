"""HPランクの「機械判定 -> 人間確認 -> 修正 -> Ground Truth蓄積」を支える
append-onlyフィードバックログの設計・実装。

重要な設計方針:
- machine_rank/machine_scoreの再計算はいつでも新しいイベント行を追加するだけで、
  既存の manual_rank を上書き・削除しない（履歴は常に残る）。
- final_rank は「その医院について確定している最新の manual_rank があればそれ、
  なければ最新の machine_rank」という読み取り時の解決ルールで決める
  （resolve_final_rank）。ストレージ側に final_rank を冗長に保存しない。
- 安定IDは medical_key（厚生局の医療機関番号ベース、Phase0で確認済みの
  重複ゼロの安定キー）を主キーとして扱う。clinic_id（現行SQLiteのclinics.id）
  は今のDBでの参照用の付随情報として持つが、将来Supabase等（例:
  clinic_ops.hp_rank_feedback）へ移すときはmedical_key基準で移行できるように、
  SQLite固有の機能（AUTOINCREMENT依存、外部key制約でのclinics直結等）には
  依存しないテーブル設計にしている。
- このモジュールは本番DB（data/clinics.sqlite3 だった旧ファイル、または
  ~/CrestixData/clinic-lead/clinics.sqlite3）のスキーマを一切変更しない。
  ここで定義するテーブルは呼び出し側が明示的に渡したconnection上にだけ作られる
  （テスト・temp DB専用。本番へは今回接続していない）。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3

FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS hp_rank_feedback_events(
 id INTEGER PRIMARY KEY,
 medical_key TEXT NOT NULL,
 clinic_id INTEGER,
 website_url TEXT NOT NULL DEFAULT '',
 machine_rank TEXT NOT NULL,
 machine_score REAL,
 model_version TEXT NOT NULL,
 features_json TEXT NOT NULL DEFAULT '{}',
 manual_rank TEXT,
 reviewer TEXT,
 reason TEXT,
 reviewed_at TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hp_feedback_medical_key ON hp_rank_feedback_events(medical_key, id);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init_feedback_schema(conn):
    """呼び出し側が渡したconnection上にだけテーブルを作る（本番DBには接続しない前提）。"""
    conn.executescript(FEEDBACK_SCHEMA)


@dataclass
class MachineRankEvent:
    medical_key: str
    machine_rank: str
    model_version: str
    clinic_id: int | None = None
    website_url: str = ""
    machine_score: float | None = None
    features: dict = field(default_factory=dict)


def record_machine_rank(conn, event: MachineRankEvent):
    """機械の再計算結果を新しいイベントとして追加するだけ。manual_rankは含めない
    （＝既存の人間修正を絶対に上書きしない）。"""
    conn.execute(
        "INSERT INTO hp_rank_feedback_events"
        "(medical_key,clinic_id,website_url,machine_rank,machine_score,model_version,features_json,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (event.medical_key, event.clinic_id, event.website_url, event.machine_rank,
         event.machine_score, event.model_version, json.dumps(event.features, ensure_ascii=False), now()),
    )


def record_manual_override(conn, medical_key, manual_rank, reviewer, reason=""):
    """人間の修正を新しいイベントとして追加する。直前の機械判定を引き継ぎ、
    manual_rankだけを追記する（machine側の情報は変えない）。"""
    prev = latest_feedback(conn, medical_key)
    if prev is None:
        raise ValueError(f"medical_key={medical_key} の機械判定イベントが先に必要です。")
    conn.execute(
        "INSERT INTO hp_rank_feedback_events"
        "(medical_key,clinic_id,website_url,machine_rank,machine_score,model_version,features_json,"
        "manual_rank,reviewer,reason,reviewed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (medical_key, prev["clinic_id"], prev["website_url"], prev["machine_rank"], prev["machine_score"],
         prev["model_version"], json.dumps(prev["features"], ensure_ascii=False),
         manual_rank, reviewer, reason, now(), now()),
    )


def latest_feedback(conn, medical_key):
    """medical_keyについて最新のイベント行（machine側の最新情報＋そこまでの
    最新manual_rankの解決結果）を返す。無ければNone。"""
    row = conn.execute(
        "SELECT * FROM hp_rank_feedback_events WHERE medical_key=? ORDER BY id DESC LIMIT 1",
        (medical_key,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["features"] = json.loads(result.pop("features_json"))
    # manual_rankは「これまでのどのイベントかで最後に設定された値」を採用する
    # （直近の再計算イベントがmanual_rank=NULLでも、以前の人間確認は消えない）。
    manual_row = conn.execute(
        "SELECT manual_rank, reviewer, reason, reviewed_at FROM hp_rank_feedback_events"
        " WHERE medical_key=? AND manual_rank IS NOT NULL ORDER BY id DESC LIMIT 1",
        (medical_key,),
    ).fetchone()
    if manual_row:
        result["manual_rank"] = manual_row["manual_rank"]
        result["reviewer"] = manual_row["reviewer"]
        result["reason"] = manual_row["reason"]
        result["reviewed_at"] = manual_row["reviewed_at"]
    else:
        result["manual_rank"] = None
    result["final_rank"] = resolve_final_rank(result["machine_rank"], result["manual_rank"])
    return result


def resolve_final_rank(machine_rank, manual_rank):
    """manual_rankがあれば常にそれを優先する。無ければmachine_rank。"""
    return manual_rank if manual_rank else machine_rank


REVIEW_MARGIN = 0  # Stage1閾値ちょうどのみを「境界」とみなす。
# 検証: margin=1だとスコア域が0-6と狭いPhase2 candidateでは300件中177件(59%)が
# HIGHになり「全件確認しない」の趣旨に反したため、margin=0（300件中105件=35%）へ調整した。
# 特徴量のスコア域が広がった場合は、しきい値からの相対距離で再検討すること。


def review_priority(machine_rank, machine_score, features, *, threshold=5, manual_rank=None):
    """全件を人間確認する運用は前提にしない。優先確認すべき理由をラベル付けする。

    優先確認対象（STEP12）:
    - C/D境界・low confidence: スコアが閾値の±REVIEW_MARGIN以内
    - 新しいD判定: machine_rankがD（現行では実質発生しないが将来のため残す）
    - A候補: SNS導線+独自LP・専門サイトが両方立っている等、A側に振れる強い根拠がある
    - 既に人間確認済み(manual_rank有)は優先度を下げる
    """
    if manual_rank:
        return {"priority": "LOW", "reason": "既に人間確認済み"}
    reasons = []
    if abs(machine_score - threshold) <= REVIEW_MARGIN:
        reasons.append("C/D境界（low confidence）")
    if machine_rank == "D":
        reasons.append("新しいD判定")
    if features.get("SNS導線") and features.get("独自LP・専門サイト"):
        reasons.append("A候補（SNS導線+独自LP・専門サイト）")
    if reasons:
        return {"priority": "HIGH", "reason": "・".join(reasons)}
    return {"priority": "NORMAL", "reason": ""}
