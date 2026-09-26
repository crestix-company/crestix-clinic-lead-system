"""
全国厚生局Masterのappend-onlyインポーター（staging専用）。

既存medical_key -> SKIP（既存行は1セルも変更しない）
新規medical_key -> INSERT
補助判定で既存の重複が濃厚 -> duplicate（INSERTしない・既存行も変更しない）
一意に確定できない -> REVIEW（INSERTしない）

本番DB(data/clinics.sqlite3)は読み取り専用で扱い、実際の書き込みは
STAGING（既定: /tmp/clinics_national_append_only.sqlite3）にのみ行う。
実行前後で本番DBのSHA-256とmtimeを検証し、変化があれば異常終了する。
"""
import sys, shutil, hashlib, csv, json, sqlite3, argparse
from pathlib import Path
from collections import Counter, defaultdict

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.master.store import ClinicStore, now, dumps, digest
from src.master.matching import medical_key
from src.normalizer.phone import tel_match_key
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.address import normalize_address
from src.utils.date_utils import parse_date


def sha256(path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


PROTECTED_DATA_DIR = (Path(__file__).resolve().parents[1] / "data")


def assert_staging_is_safe(prod_path, staging_path):
    """staging_pathが本番DB（またはdata/配下の他の本番ファイル）を指していないことを保証する。

    コマンドの指定ミス1つで本番DBへ直接INSERTしてしまう事故を防ぐための必須ガード。
    書き込み先(staging_path)が本番ファイルそのもの、もしくはリポジトリのdata/配下
    （clinics.sqlite3・demo.sqlite3など本番相当のファイルが置かれる場所）を指す場合は
    無条件で中止する。回避オプションは提供しない。
    """
    prod_resolved = Path(prod_path).resolve()
    staging_resolved = Path(staging_path).resolve()
    if staging_resolved == prod_resolved:
        raise SystemExit(f"ABORT: staging_pathが本番DBと同一パスです({staging_resolved})。処理を中止しました。")
    try:
        staging_resolved.relative_to(PROTECTED_DATA_DIR.resolve())
        raise SystemExit(
            f"ABORT: staging_pathがdata/配下（本番DB用ディレクトリ: {PROTECTED_DATA_DIR}）を指しています"
            f"({staging_resolved})。staging用のパスは必ずdata/の外（例: /tmp配下）を指定してください。"
        )
    except ValueError:
        pass  # data/配下ではない -> 安全


def run(prod_path, staging_path, csv_path, expected_hash=None, test_unique_index=True, reset_staging=True):
    """reset_staging=False で既存のstaging_pathをそのまま使う（本番からの再コピーをしない）。
    同一importをもう一度実行してidempotency（2回目のnew_insert=0）を確認する用途に使う。
    """
    assert_staging_is_safe(prod_path, staging_path)
    report = {}

    before_hash = sha256(prod_path)
    before_mtime = prod_path.stat().st_mtime
    report["prod_hash_before"] = before_hash
    report["prod_mtime_before"] = before_mtime
    if expected_hash and before_hash != expected_hash:
        raise SystemExit("ABORT: 本番DBのハッシュが期待値と異なります。処理を中止しました。")

    if reset_staging:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(staging_path) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy2(prod_path, staging_path)
    elif not Path(staging_path).exists():
        raise SystemExit("ABORT: reset_staging=False ですが staging_path が存在しません。")

    con = sqlite3.connect(f"file:{staging_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = [r[1] for r in con.execute("PRAGMA table_info(clinics)")]
    orig_rows = {r["id"]: tuple(r[c] for c in cols) for r in con.execute("SELECT * FROM clinics")}
    orig_total = len(orig_rows)

    existing_keys = set(r[0] for r in con.execute("SELECT medical_key FROM clinics WHERE medical_key<>''"))

    # medical_keyが空の行だけ、既存DB全体と照合する（task4）。
    phone_index_full = defaultdict(list)
    for r in con.execute("SELECT id, tel_match_key FROM clinics WHERE tel_match_key<>''"):
        phone_index_full[r["tel_match_key"]].append(r["id"])
    na_index_full = defaultdict(list)
    for r in con.execute("SELECT id, name_norm, address_norm FROM clinics WHERE name_norm<>'' AND address_norm<>''"):
        na_index_full[(r["name_norm"], r["address_norm"])].append(r["id"])

    # 自分自身が新規medical_keyを持つ行は、medical_keyが無い既存レコード（コムデスクのみ等）
    # とだけ照合する。既に別のmedical_keyを持つ既存医院とは、電話・名称・住所が似ていても
    # matching.compatible_medical_identity() と同じ考え方で別施設として扱う。
    phone_index_nokey = defaultdict(list)
    for r in con.execute("SELECT id, tel_match_key FROM clinics WHERE tel_match_key<>'' AND medical_key=''"):
        phone_index_nokey[r["tel_match_key"]].append(r["id"])
    na_index_nokey = defaultdict(list)
    for r in con.execute("SELECT id, name_norm, address_norm FROM clinics WHERE name_norm<>'' AND address_norm<>'' AND medical_key=''"):
        na_index_nokey[(r["name_norm"], r["address_norm"])].append(r["id"])
    con.close()

    all_rows, errors = [], 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            row["_row_number"] = i
            if not row.get("clinic_name") or not row.get("prefecture") or row.get("medical_type") not in {"医科", "歯科"} or not parse_date(row.get("as_of")):
                errors += 1
                continue
            all_rows.append(row)
    master_total = i + 1

    existing_skip = 0
    new_insert_rows, review_rows, duplicate_rows = [], [], []
    inserted_keys_this_run, dup_within_csv = set(), 0

    for row in all_rows:
        mk = medical_key(row)
        phone = tel_match_key(row.get("phone"))
        name = normalize_clinic_name(row.get("clinic_name"))
        addr = normalize_address(row.get("address"))

        if mk:
            if mk in existing_keys:
                existing_skip += 1
                continue
            if mk in inserted_keys_this_run:
                dup_within_csv += 1
                duplicate_rows.append((mk, row, set()))
                continue
            candidates = set()
            if phone and phone in phone_index_nokey:
                candidates |= set(phone_index_nokey[phone])
            if name and addr and (name, addr) in na_index_nokey:
                candidates |= set(na_index_nokey[(name, addr)])
            if len(candidates) == 1:
                duplicate_rows.append((mk, row, candidates))
                continue
            if len(candidates) > 1:
                review_rows.append(row)
                continue
            new_insert_rows.append(row)
            inserted_keys_this_run.add(mk)
        else:
            candidates = set()
            if phone and phone in phone_index_full:
                candidates |= set(phone_index_full[phone])
            if name and addr and (name, addr) in na_index_full:
                candidates |= set(na_index_full[(name, addr)])
            if len(candidates) == 1:
                duplicate_rows.append((mk, row, candidates))
            else:
                review_rows.append(row)

    report.update(master_total=master_total, errors=errors, existing_skip=existing_skip,
                  new_insert=len(new_insert_rows), review=len(review_rows),
                  duplicate=len(duplicate_rows), dup_within_csv=dup_within_csv,
                  orig_total=orig_total)

    store = ClinicStore(staging_path)
    batch_id = digest(["national_append_only", str(csv_path), now()])
    ts = now()
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for row in new_insert_rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            cid = c.execute(
                "INSERT INTO clinics(uuid,base_json,first_seen_at,last_seen_at,source_as_of_date,is_new) VALUES(?,?,?,?,?,?)",
                ("", dumps(clean), ts, ts, clean.get("as_of", ""), 1)
            ).lastrowid
            store._history(c, cid, "全国マスター追加(append-only)", {}, clean, "medical_key新規: " + medical_key(clean))
            store._project(c, cid)
            c.execute(
                "INSERT INTO source_records(clinic_id,source,record_json,source_hash,row_number,match_status,match_reason,match_score,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, "厚生局", dumps(clean), batch_id, row["_row_number"], "NEW", "全国Master append-only import", 100.0, ts)
            )
        c.execute(
            "INSERT INTO import_batches VALUES(?,?,?,?)",
            (batch_id, "厚生局(全国Master-append-only)",
             dumps({"existing_skip": existing_skip, "new_insert": len(new_insert_rows),
                    "review": len(review_rows), "duplicate": len(duplicate_rows), "error": errors}),
             ts)
        )

    con2 = sqlite3.connect(f"file:{staging_path}?mode=ro", uri=True)
    con2.row_factory = sqlite3.Row
    after_total = con2.execute("SELECT COUNT(*) FROM clinics").fetchone()[0]
    changed_records = changed_cells = 0
    diffs_sample = []
    for cid, before in orig_rows.items():
        after_row = con2.execute("SELECT * FROM clinics WHERE id=?", (cid,)).fetchone()
        after = tuple(after_row[c] for c in cols)
        if after != before:
            changed_records += 1
            changed_cells += sum(1 for a, b in zip(before, after) if a != b)
            if len(diffs_sample) < 5:
                diffs_sample.append((cid, [(c, b, a) for c, b, a in zip(cols, before, after) if b != a]))

    new_ids = [r[0] for r in con2.execute(
        "SELECT id FROM clinics WHERE id NOT IN (%s)" % ",".join("?" for _ in orig_rows), list(orig_rows.keys())
    )] if orig_rows else []

    def in_new(select):
        return con2.execute(select % ",".join("?" for _ in new_ids), new_ids) if new_ids else []

    new_keys = [r[0] for r in in_new("SELECT medical_key FROM clinics WHERE id IN (%s)")]
    new_key_dupe_count = len(new_keys) - len(set(new_keys))
    new_pref = Counter(r[0] for r in in_new("SELECT prefecture FROM clinics WHERE id IN (%s)"))
    new_medtype = Counter(r[0] for r in in_new("SELECT medical_type FROM clinics WHERE id IN (%s)"))
    new_uuid_nonempty = con2.execute(
        "SELECT COUNT(*) FROM clinics WHERE id IN (%s) AND uuid<>''" % ",".join("?" for _ in new_ids), new_ids
    ).fetchone()[0] if new_ids else 0
    new_maps_touched = con2.execute(
        "SELECT COUNT(*) FROM clinics WHERE id IN (%s) AND (maps_website_url<>'' OR maps_presence_status<>'')" % ",".join("?" for _ in new_ids), new_ids
    ).fetchone()[0] if new_ids else 0
    new_hp_touched = con2.execute(
        "SELECT COUNT(*) FROM clinics WHERE id IN (%s) AND hp_status<>'UNRESEARCHED'" % ",".join("?" for _ in new_ids), new_ids
    ).fetchone()[0] if new_ids else 0
    new_facility, new_status = Counter(), Counter()
    for r in in_new("SELECT base_json FROM clinics WHERE id IN (%s)"):
        d = json.loads(r[0])
        new_facility[d.get("facility_type", "")] += 1
        new_status[d.get("status", "")] += 1

    report.update(after_total=after_total, changed_records=changed_records, changed_cells=changed_cells,
                  diffs_sample=diffs_sample, new_key_dupe_count=new_key_dupe_count,
                  new_pref=dict(new_pref), new_medtype=dict(new_medtype), new_uuid_nonempty=new_uuid_nonempty,
                  new_maps_touched=new_maps_touched, new_hp_touched=new_hp_touched,
                  new_facility=dict(new_facility), new_status=dict(new_status))

    unique_index_ok, unique_index_error = None, None
    if test_unique_index:
        try:
            con2.close()
            con3 = sqlite3.connect(staging_path)
            con3.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clinic_medical_key_unique_TEST ON clinics(medical_key) WHERE medical_key<>'' AND merged_into IS NULL")
            con3.commit()
            unique_index_ok = True
            con3.execute("DROP INDEX idx_clinic_medical_key_unique_TEST")
            con3.commit()
            con3.close()
        except sqlite3.IntegrityError as e:
            unique_index_ok = False
            unique_index_error = str(e)
    report["unique_index_creatable_on_staging"] = unique_index_ok
    report["unique_index_error"] = unique_index_error

    after_hash = sha256(prod_path)
    after_mtime = prod_path.stat().st_mtime
    report["prod_hash_after"] = after_hash
    report["prod_mtime_after"] = after_mtime
    report["prod_unchanged"] = (after_hash == before_hash) and (after_mtime == before_mtime)

    return report, duplicate_rows, review_rows


def main():
    parser = argparse.ArgumentParser(description="全国厚生局Masterのappend-onlyインポート（staging専用）")
    parser.add_argument("--prod", default=str(REPO / "data/clinics.sqlite3"))
    parser.add_argument("--staging", default="/tmp/clinics_national_append_only.sqlite3")
    parser.add_argument("--csv", default=str(REPO / "national_maps_output/20260918_201021/national_kouseikyoku_master.csv"))
    parser.add_argument("--expect-hash", default=None, help="本番DBの想定SHA-256。指定時は不一致で中止する。")
    parser.add_argument("--no-reset", action="store_true", help="staging_pathを本番から再コピーせず、既存のstagingにそのまま追記する（idempotency検証用）。")
    args = parser.parse_args()

    report, duplicate_rows, review_rows = run(Path(args.prod), Path(args.staging), Path(args.csv), args.expect_hash,
                                               reset_staging=not args.no_reset)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    print("\n=== DUPLICATE ROWS (up to 25) ===")
    for mk, row, cand in duplicate_rows[:25]:
        print(f"mk={mk!r} name={row.get('clinic_name')!r} addr={row.get('address')!r} phone={row.get('phone')!r} pref={row.get('prefecture')} candidates={sorted(cand)}")

    print("\n=== REVIEW ROWS (up to 25) ===")
    for row in review_rows[:25]:
        print(f"name={row.get('clinic_name')!r} addr={row.get('address')!r} phone={row.get('phone')!r} pref={row.get('prefecture')} medtype={row.get('medical_type')}")


if __name__ == "__main__":
    main()
