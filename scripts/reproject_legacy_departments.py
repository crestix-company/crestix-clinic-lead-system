"""旧13,970件（既存営業リスト）のdepartments_jsonを、現行normalize_departments()で再計算するscript。

デフォルトはdry-run（Production DBはmode=ro + PRAGMA query_only=ONで開き、一切書き込まない）。
--apply を明示しない限りWRITEは実行しない。--apply でも次のいずれかに違反すればROLLBACKして
STOPする（fail-closed）: integrity_check!="ok" / journal_mode=walで-wal未チェックポイント
（main fileのsha256が信頼できない） / --expected-source-sha256不一致 / candidate件数不一致。
これらはwriter lock（BEGIN IMMEDIATE）取得の前後で二重に検証する。

変更対象は原則 clinics.departments_json 列のみ。HP/Maps/UUID/medical_key/Comdesk/
research/base_json/effective_json/first_seen_at/source_records/status 等は触らない。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.master.scope import LEGACY_PRE_NATIONAL_CUTOFF  # noqa: E402
from src.normalizer.departments import normalize_departments  # noqa: E402

BEAUTY_UNION_LABEL = "美容外科系（美容整形外科+美容外科の合算・audit専用、Production列には書き込まない）"
BEAUTY_UNION_MEMBERS = ("美容整形外科", "美容外科")


def norm_key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_readonly(path):
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def wal_file_size(db_path):
    wal_path = Path(str(db_path) + "-wal")
    return wal_path.stat().st_size if wal_path.exists() else 0


def snapshot(path):
    path = Path(path)
    stat = path.stat()
    conn = open_readonly(path)
    try:
        clinics = conn.execute("SELECT count(*) FROM clinics").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    return {
        "sha256": file_sha256(path),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "clinics": clinics,
        "integrity": integrity,
        "journal_mode": journal_mode,
        "wal_size": wal_file_size(path),
    }


def apply_precondition_violation(snap, expected_source_sha256):
    """apply前提（integrity/WAL状態/source sha256）がsnapshotの時点で満たされているか判定する。

    問題なければNone、問題があれば理由の文字列を返す（呼び出し側でApplyAbortedにする）。
    journal_mode=walで-walが未チェックポイント（size!=0）の場合、main file単体のsha256は
    DBの実際の内容を表さないため、sha256の一致判定より先にこの状態を検出してSTOPする。
    """
    if snap["integrity"] != "ok":
        return f"DB整合性チェックがokではありません（integrity_check={snap['integrity']}）。"
    if snap["journal_mode"] == "wal" and snap["wal_size"] != 0:
        return (
            f"journal_mode=walで-walファイルが未チェックポイントです（wal_size={snap['wal_size']}バイト）。"
            "main fileのsha256がDBの現在状態を正しく表さないため信頼できません。"
        )
    if expected_source_sha256 is not None and snap["sha256"] != expected_source_sha256:
        return f"Production DBのsha256が想定と異なります（期待={expected_source_sha256} 実際={snap['sha256']}）。"
    return None


def fetch_legacy_rows(conn):
    return conn.execute(
        "SELECT id, base_json, departments_json FROM clinics WHERE first_seen_at<?",
        (LEGACY_PRE_NATIONAL_CUTOFF,),
    ).fetchall()


class Classification:
    def __init__(self):
        self.legacy_total = 0
        self.raw_nonempty = 0
        self.raw_empty = 0
        self.stored_fresh_equal = 0
        self.normalizer_unresolved = 0
        self.candidate_updates = 0
        self.candidates = []  # (clinic_id, raw, stored, fresh)
        self.before = {}
        self.after = {}

    def _tally(self, table, departments):
        for d in departments:
            table[d] = table.get(d, 0) + 1

    def add(self, clinic_id, raw, stored, fresh, raw_is_empty):
        self.legacy_total += 1
        self._tally(self.before, stored)
        if raw_is_empty:
            self.raw_empty += 1
            self._tally(self.after, stored)
            return
        self.raw_nonempty += 1
        self._tally(self.after, fresh)
        if norm_key(stored) == norm_key(fresh):
            if fresh == ["その他"]:
                self.normalizer_unresolved += 1
            else:
                self.stored_fresh_equal += 1
        else:
            self.candidate_updates += 1
            self.candidates.append((clinic_id, raw, stored, fresh))

    def department_rows(self):
        names = sorted(set(self.before) | set(self.after))
        return [(name, self.before.get(name, 0), self.after.get(name, 0)) for name in names]


def classify(rows):
    result = Classification()
    beauty_before_ids = set()
    beauty_after_ids = set()
    for clinic_id, base_json, stored_json in rows:
        base = json.loads(base_json)
        raw = base.get("departments", "")
        stored = json.loads(stored_json or "[]")
        raw_is_empty = not str(raw or "").strip()
        fresh = stored if raw_is_empty else normalize_departments(raw)
        result.add(clinic_id, raw, stored, fresh, raw_is_empty)
        if any(d in BEAUTY_UNION_MEMBERS for d in stored):
            beauty_before_ids.add(clinic_id)
        if any(d in BEAUTY_UNION_MEMBERS for d in fresh):
            beauty_after_ids.add(clinic_id)
    result.beauty_union_before = len(beauty_before_ids)
    result.beauty_union_after = len(beauty_after_ids)
    return result


def write_artifacts(out_dir, classification, snapshot_before, snapshot_after=None, expected_candidates=8086):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "legacy_total": classification.legacy_total,
        "raw_nonempty": classification.raw_nonempty,
        "raw_empty": classification.raw_empty,
        "stored_fresh_equal": classification.stored_fresh_equal,
        "normalizer_unresolved": classification.normalizer_unresolved,
        "candidate_updates": classification.candidate_updates,
        "expected_candidates": expected_candidates,
        "candidate_match": classification.candidate_updates == expected_candidates,
        "snapshot_before": snapshot_before,
        "snapshot_after": snapshot_after,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    with (out_dir / "candidate_updates.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["clinic_id", "raw_departments", "stored_departments", "fresh_departments"])
        for clinic_id, raw, stored, fresh in classification.candidates:
            writer.writerow([clinic_id, raw, norm_key(stored), norm_key(fresh)])

    with (out_dir / "department_counts_before_after.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["department", "before", "after"])
        for name, before, after in classification.department_rows():
            writer.writerow([name, before, after])
        writer.writerow([BEAUTY_UNION_LABEL, classification.beauty_union_before, classification.beauty_union_after])

    return summary


def run_dry_run(db_path, out_dir, expected_candidates=8086):
    before = snapshot(db_path)
    conn = open_readonly(db_path)
    try:
        rows = fetch_legacy_rows(conn)
    finally:
        conn.close()
    classification = classify(rows)
    after = snapshot(db_path)
    summary = write_artifacts(out_dir, classification, before, after, expected_candidates)
    unchanged = (
        before["sha256"] == after["sha256"] and before["mtime"] == after["mtime"]
        and before["size"] == after["size"] and before["clinics"] == after["clinics"]
        and after["integrity"] == "ok"
    )
    summary["db_unchanged"] = unchanged
    (Path(out_dir) / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary, classification


class ApplyAborted(RuntimeError):
    pass


def run_apply(db_path, out_dir, expected_candidates, expected_source_sha256=None):
    """Production apply実装。

    fail-closed guardをwriter lock（BEGIN IMMEDIATE）取得の前後で二重に検証する：
      - integrity_check == "ok"
      - journal_mode=walなら-walファイルが空（チェックポイント済み）であること
        （そうでない場合main fileのsha256はDBの実際の内容を表さない）
      - --expected-source-sha256を指定していればsha256が一致すること
      - candidate件数がexpected_candidatesと一致すること（writer lock取得後、フレッシュに再計算）
    事前チェック（lock取得前）は不要な書き込みロック取得を避けるための早期fail-fast。
    lock取得後の再チェックは、事前チェックからBEGIN IMMEDIATEまでの間に別プロセスが
    DBを書き換えた場合のTOCTOUを閉じるためのもの。いずれかに違反した場合はROLLBACKして
    ApplyAbortedを送出し、departments_jsonへのUPDATEは実行しない。
    """
    before = snapshot(db_path)
    reason = apply_precondition_violation(before, expected_source_sha256)
    if reason:
        raise ApplyAborted(reason + " apply前提が崩れているためSTOPしました（writer lock取得前）。")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        locked_snapshot = {
            "sha256": file_sha256(db_path),
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "wal_size": wal_file_size(db_path),
        }
        reason = apply_precondition_violation(locked_snapshot, expected_source_sha256)
        if reason:
            conn.rollback()
            raise ApplyAborted(reason + " apply前提が崩れているためSTOPしました（writer lock取得後の再検証）。")

        rows = fetch_legacy_rows(conn)
        classification = classify(rows)
        if classification.candidate_updates != expected_candidates:
            conn.rollback()
            raise ApplyAborted(
                f"candidate件数が期待値と不一致のためROLLBACKしました（期待={expected_candidates} "
                f"実際={classification.candidate_updates}）。"
            )
        conn.executemany(
            "UPDATE clinics SET departments_json=? WHERE id=?",
            [(norm_key(fresh), clinic_id) for clinic_id, _raw, _stored, fresh in classification.candidates],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    after = snapshot(db_path)
    write_artifacts(out_dir, classification, before, after, expected_candidates)
    return classification


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="clinics.sqlite3の絶対パス")
    parser.add_argument("--out", default="/tmp/clinic_legacy13k_reproject", help="dry-run成果物の出力先ディレクトリ")
    parser.add_argument("--expected-candidates", type=int, default=None,
                         help="candidate_updates期待値。--apply時は省略不可（fail-closed）。dry-runで省略した場合は8086を使う。")
    parser.add_argument("--apply", action="store_true", help="明示した場合のみWRITEを行う。デフォルトはdry-run。")
    parser.add_argument("--expected-source-sha256", default=None,
                         help="--apply時、この値とProduction DBの現在のsha256が一致しない場合はSTOPする。")
    args = parser.parse_args(argv)
    if args.apply and args.expected_candidates is None:
        parser.error("--apply時は--expected-candidatesの指定が必須です（省略不可。8,086件guardをCLIレベルでfail-closedにするため）。")
    if args.expected_candidates is None:
        args.expected_candidates = 8086
    return args


def main(argv=None):
    args = parse_args(argv)
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"DBファイルが見つかりません: {db_path}", file=sys.stderr)
        return 2

    if not args.apply:
        summary, classification = run_dry_run(db_path, args.out, args.expected_candidates)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not summary["candidate_match"]:
            print(
                f"FAIL CLOSED: candidate_updates={classification.candidate_updates} "
                f"!= expected={args.expected_candidates}",
                file=sys.stderr,
            )
            return 1
        if not summary["db_unchanged"]:
            print("FAIL CLOSED: dry-run前後でDBのsha256/mtime/size/clinics/integrityが変化しました。", file=sys.stderr)
            return 1
        return 0

    try:
        classification = run_apply(db_path, args.out, args.expected_candidates, args.expected_source_sha256)
    except ApplyAborted as exc:
        print(f"APPLY ABORTED: {exc}", file=sys.stderr)
        return 1
    print(f"APPLY OK: {classification.candidate_updates}件のdepartments_jsonを更新しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
