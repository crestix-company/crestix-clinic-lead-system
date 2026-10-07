"""HP再調査バッチ（Stage4 Canary）。

目的: hp_url取得済みだがHP本文調査/HP ABC特徴量取得が未完了の医院について、
1回のfetch結果からTreatment ResearchとHP ABC v2 candidate特徴量の両方を導出する
（同一医院を二重fetchしない）。

Production DB: READ ONLYのみ（本番clinics.sqlite3へは一切書き込まない。hp_rankも更新しない）。
既存Treatment sidecar（treatment_research_final.sqlite3）: READ ONLYのみ（候補選定の参考情報
としてだけ使う。書き込まない）。

保存先: 本モジュール専用の新規sidecar（Production/既存Treatment sidecarとは別ファイル。
デフォルトは artifacts/hp_research_batch/hp_abc_batch_sidecar.sqlite3）。clinic_id単位で
PRIMARY KEYのためidempotent。fetch_status='OK'の行、またはattempts>=MAX_ATTEMPTSの行は
次回実行時に候補から自動的に除外される（二重処理防止・無限retry防止）。

HP ABC v2 candidate（v2-precision-first）はあくまで参考値。Production hp_rankへは反映しない。
"""
import json
import sqlite3
import time
from urllib.parse import urlsplit
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.enrichment.safe_web import SafeFetcher, crawl, WebError, validate_url
from src.scoring.research_scoring import treatments, dedupe_signals, hp_signals, media_signals, rank_hp
from src.scoring.hp_rank_v2_features import extract_v2_features
from src.scoring.hp_rank_recalibration_v2 import candidate_hp_rank_v2
from src.master.research_sidecar import CLINIC_RESEARCH_STATUS_TABLE

ENGINE_VERSION = "hp_research_batch_v1+hp_rank_v2:precision_first"
MAX_ATTEMPTS = 3
DEFAULT_SIDECAR_PATH = Path(__file__).resolve().parents[2] / "artifacts" / "hp_research_batch" / "hp_abc_batch_sidecar.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hp_research_batch_results(
  clinic_id INTEGER PRIMARY KEY,
  hp_url TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL,
  final_url TEXT NOT NULL DEFAULT '',
  treatment_status TEXT NOT NULL DEFAULT '',
  treatment_categories TEXT NOT NULL DEFAULT '[]',
  hp_abc_candidate TEXT NOT NULL DEFAULT '',
  hp_abc_score TEXT NOT NULL DEFAULT '',
  candidate_rank_1 TEXT NOT NULL DEFAULT '',
  candidate_rank_2 TEXT NOT NULL DEFAULT '',
  ambiguity_reason TEXT NOT NULL DEFAULT '',
  feature_json TEXT NOT NULL DEFAULT '[]',
  researched_at TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  error_detail TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  elapsed_seconds REAL NOT NULL DEFAULT 0
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect_sidecar(path=None):
    """新規sidecarへの唯一の書き込み経路。Production DB・既存Treatment sidecarはここでは開かない。"""
    path = Path(path or DEFAULT_SIDECAR_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def _production_readonly_uri(path):
    return f"file:{path}?mode=ro&immutable=1"


def select_candidates(production_db_path, sidecar_conn, limit, treatment_sidecar_path=None):
    """優先順位（降順）:
    1. hp_urlあり（clinics.hp_url確定済み）
    2. HP ABC未評価（=本sidecarに未処理 or attempts超過していない。常に前提として適用）
    3. Treatment未調査（既存sidecarのclinic_research_statusに行が無い医院を優先）
    4. hp_urlなし・maps_website_url由来のpipeline gap回復（1の次に優先度を下げて含める）
    5. 古いengine_version（本バッチは新規のため今回は対象外。将来の再処理用に予約）
    READ ONLYのみ。MAX_ATTEMPTS回失敗した行は無限retryを防ぐため除外する。
    """
    con = sqlite3.connect(_production_readonly_uri(production_db_path), uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT id, hp_url, maps_website_url FROM clinics WHERE hp_url<>'' OR maps_website_url<>''")
        all_candidates = cur.fetchall()
    finally:
        con.close()

    excluded = set()
    for row in sidecar_conn.execute(
        "SELECT clinic_id FROM hp_research_batch_results WHERE fetch_status='OK' OR attempts>=?", (MAX_ATTEMPTS,)
    ):
        excluded.add(row["clinic_id"])

    researched_ids = set()
    if treatment_sidecar_path and Path(treatment_sidecar_path).exists():
        tcon = sqlite3.connect(f"file:{treatment_sidecar_path}?mode=ro", uri=True)
        try:
            researched_ids = {r[0] for r in tcon.execute(f"SELECT clinic_id FROM {CLINIC_RESEARCH_STATUS_TABLE}")}
        except sqlite3.Error:
            pass
        finally:
            tcon.close()

    pool = []
    for cid, hp_url, maps_url in all_candidates:
        if cid in excluded:
            continue
        has_confirmed_url = bool(hp_url)
        url_to_fetch = hp_url or maps_url
        if not url_to_fetch:
            continue
        tier1 = 0 if has_confirmed_url else 1
        tier3 = 0 if cid not in researched_ids else 1
        tier4 = 0 if has_confirmed_url else 1  # hp_urlなし(=1)は4番目の優先度として一段落とす
        pool.append((tier1, tier3, tier4, cid, url_to_fetch, has_confirmed_url))

    pool.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    return [{"clinic_id": r[3], "url": r[4], "hp_url_confirmed": r[5]} for r in pool[:limit]]


def fetch_clinic(url, fetcher=None):
    """1回だけfetch。戻り値: (pages, fetch_status, final_url, error_detail)。"""
    fetcher = fetcher or SafeFetcher()
    try:
        validate_batch_url(url)
        first = fetcher.fetch(url)
        pages, _errors = crawl(first, fetcher, max_pages=6)
        return pages, "OK", first.url, None
    except InvalidBatchUrl as exc:
        return [], "INVALID_URL", "", str(exc)
    except WebError as exc:
        status = "TIMEOUT" if "timeout" in str(exc).lower() else "ERROR"
        return [], status, "", str(exc)
    except Exception as exc:  # 想定外のparse例外等もfetch失敗として記録する（バッチを止めない）
        return [], "ERROR", "", f"EXC:{exc}"


class InvalidBatchUrl(ValueError):
    pass


def validate_batch_url(url):
    """fetch対象が明白な公開HTTP(S) URLであることをDNSアクセス前に確認する。"""
    text = str(url or "").strip()
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname or ""
        validate_url(text, resolve=False)
    except (ValueError, UnicodeError, WebError):
        raise InvalidBatchUrl("HTTP/HTTPS URLとして解析できません。") from None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise InvalidBatchUrl("http:// または https:// で始まる有効なURLではありません。")
    if any(ch.isspace() for ch in parsed.netloc) or len(hostname) > 253 or "." not in hostname:
        raise InvalidBatchUrl("ホスト名がURLではなく文章または不正な文字列です。")
    return text


def derive_results(pages, record=None, engine_version=ENGINE_VERSION):
    """同一fetch結果からTreatment Research判定とHP ABC v2 candidate特徴量を導出する純粋関数。
    ネットワークアクセスなし。単体テスト可能。
    """
    record = record or {"clinic_name": ""}
    treatment = treatments(pages, record=record)
    signals = dedupe_signals(hp_signals(record, pages) + media_signals(record, []))
    rank_result = rank_hp(pages, treatment, signals)
    old_feats = {r["feature"] for r in rank_result.get("hp_rank_reasons", [])}
    new_feats = extract_v2_features(pages, record)
    all_feats = old_feats | new_feats
    v2 = candidate_hp_rank_v2(all_feats, preset="precision_first")
    return {
        "treatment_status": "DONE",
        "treatment_categories": treatment.get("treatment_categories", []),
        "hp_abc_candidate": v2.hp_rank,
        "hp_abc_score": f"pos={v2.positive_score},neg={v2.negative_score}",
        "candidate_rank_1": v2.candidate_rank_1 or "",
        "candidate_rank_2": v2.candidate_rank_2 or "",
        "ambiguity_reason": v2.ambiguity_reason or "",
        "feature_json": json.dumps(sorted(all_feats), ensure_ascii=False),
    }


def process_one(clinic_id, url, hp_url_confirmed, fetcher=None, record=None):
    """1医院分のfetch+導出。戻り値はsidecarへそのままUPSERTできるdictとelapsed秒。"""
    t0 = time.monotonic()
    pages, fetch_status, final_url, error_detail = fetch_clinic(url, fetcher=fetcher)
    if fetch_status == "OK":
        try:
            derived = derive_results(pages, record=record)
        except (ValueError, UnicodeError) as exc:
            # 実サイト内の壊れたhref等も医院単位で記録し、バッチ全体を止めない。
            fetch_status = "INVALID_URL"
            error_detail = f"取得ページ内の不正URL: {exc}"
            derived = None
        except Exception as exc:
            fetch_status = "ANALYSIS_ERROR"
            error_detail = f"解析エラー: {type(exc).__name__}: {exc}"
            derived = None
    else:
        derived = None
    if derived is None:
        derived = {
            "treatment_status": "FETCH_FAILED", "treatment_categories": [],
            "hp_abc_candidate": "", "hp_abc_score": "", "candidate_rank_1": "", "candidate_rank_2": "",
            "ambiguity_reason": "", "feature_json": "[]",
        }
    elapsed = time.monotonic() - t0
    return {
        "clinic_id": clinic_id, "hp_url": url if hp_url_confirmed else "", "fetch_status": fetch_status,
        "final_url": final_url or "", "treatment_status": derived["treatment_status"],
        "treatment_categories": json.dumps(derived["treatment_categories"], ensure_ascii=False),
        "hp_abc_candidate": derived["hp_abc_candidate"], "hp_abc_score": derived["hp_abc_score"],
        "candidate_rank_1": derived["candidate_rank_1"], "candidate_rank_2": derived["candidate_rank_2"],
        "ambiguity_reason": derived["ambiguity_reason"], "feature_json": derived["feature_json"],
        "researched_at": now_iso(), "engine_version": ENGINE_VERSION,
        "error_detail": error_detail or "", "elapsed_seconds": round(elapsed, 3),
    }


def upsert_result(sidecar_conn, result):
    """clinic_id単位でidempotentにUPSERTする（attemptsは既存値+1）。新規sidecarのみ書き込む。"""
    row = sidecar_conn.execute(
        "SELECT attempts FROM hp_research_batch_results WHERE clinic_id=?", (result["clinic_id"],)
    ).fetchone()
    prior_attempts = row["attempts"] if row else 0
    sidecar_conn.execute(
        """
        INSERT INTO hp_research_batch_results
          (clinic_id, hp_url, fetch_status, final_url, treatment_status, treatment_categories,
           hp_abc_candidate, hp_abc_score, candidate_rank_1, candidate_rank_2, ambiguity_reason,
           feature_json, researched_at, engine_version, error_detail, attempts, elapsed_seconds)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(clinic_id) DO UPDATE SET
          hp_url=excluded.hp_url, fetch_status=excluded.fetch_status, final_url=excluded.final_url,
          treatment_status=excluded.treatment_status, treatment_categories=excluded.treatment_categories,
          hp_abc_candidate=excluded.hp_abc_candidate, hp_abc_score=excluded.hp_abc_score,
          candidate_rank_1=excluded.candidate_rank_1, candidate_rank_2=excluded.candidate_rank_2,
          ambiguity_reason=excluded.ambiguity_reason, feature_json=excluded.feature_json,
          researched_at=excluded.researched_at, engine_version=excluded.engine_version,
          error_detail=excluded.error_detail, attempts=excluded.attempts, elapsed_seconds=excluded.elapsed_seconds
        """,
        (
            result["clinic_id"], result["hp_url"], result["fetch_status"], result["final_url"],
            result["treatment_status"], result["treatment_categories"], result["hp_abc_candidate"],
            result["hp_abc_score"], result["candidate_rank_1"], result["candidate_rank_2"],
            result["ambiguity_reason"], result["feature_json"], result["researched_at"],
            result["engine_version"], result["error_detail"], prior_attempts + 1, result["elapsed_seconds"],
        ),
    )
    sidecar_conn.commit()


def _build_hp_repo(sidecar_conn):
    """Stage4-D Gate2 WRITE backend selector for this worker. Same env var/default (sqlite)
    as app_v2.py's write_repositories_for(), consistent if both are configured together."""
    from src.repository.write_backend import active_write_backend, WRITE_BACKEND_SQLITE
    if active_write_backend() == WRITE_BACKEND_SQLITE:
        from src.repository.hp_sqlite_write_adapter import SqliteHpWriteRepository
        return SqliteHpWriteRepository(sidecar_conn)
    import os as _os
    from src.repository.supabase_adapter import connect
    from src.repository.hp_supabase_write_adapter import SupabaseHpWriteRepository
    url = _os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL is not set")
    return SupabaseHpWriteRepository(connect(url, autocommit=False))


@dataclass
class BatchRunReport:
    target: int
    attempted: int
    succeeded: int
    failed: int
    retried: int
    elapsed_seconds: list


def run_batch(production_db_path, limit, batch_size=20, sidecar_path=None, treatment_sidecar_path=None,
              max_workers=10, progress_cb=None):
    """limit件を上限に候補を選定し、batch_size件ずつ処理する。resume可能・idempotent。
    Production DB・既存Treatment sidecarはREAD ONLYのみ（このモジュール全体で変更しない）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with connect_sidecar(sidecar_path) as sidecar_conn:
        hp_repo = _build_hp_repo(sidecar_conn)
        candidates = select_candidates(production_db_path, sidecar_conn, limit, treatment_sidecar_path)
        attempted = succeeded = failed = retried = 0
        elapsed_list = []
        done = 0
        for chunk_start in range(0, len(candidates), batch_size):
            chunk = candidates[chunk_start: chunk_start + batch_size]
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {
                    ex.submit(process_one, c["clinic_id"], c["url"], c["hp_url_confirmed"]): c
                    for c in chunk
                }
                for fut in as_completed(futs):
                    candidate = futs[fut]
                    try:
                        result = fut.result()
                    except Exception as exc:
                        # 最後の防壁。1医院の想定外例外で他医院の結果を失わない。
                        result = {
                            "clinic_id": candidate["clinic_id"],
                            "hp_url": candidate["url"] if candidate["hp_url_confirmed"] else "",
                            "fetch_status": "ANALYSIS_ERROR", "final_url": "",
                            "treatment_status": "FETCH_FAILED", "treatment_categories": "[]",
                            "hp_abc_candidate": "", "hp_abc_score": "", "candidate_rank_1": "",
                            "candidate_rank_2": "", "ambiguity_reason": "", "feature_json": "[]",
                            "researched_at": now_iso(), "engine_version": ENGINE_VERSION,
                            "error_detail": f"未捕捉解析エラー: {type(exc).__name__}: {exc}",
                            "elapsed_seconds": 0.0,
                        }
                    prior = sidecar_conn.execute(
                        "SELECT attempts FROM hp_research_batch_results WHERE clinic_id=?", (result["clinic_id"],)
                    ).fetchone()
                    if prior and prior["attempts"] > 0:
                        retried += 1
                    # Stage4-D Gate2: persistent WRITE goes through the Repository (same SQL,
                    # same idempotent ON CONFLICT upsert -- see upsert_result() above, now
                    # called from inside SqliteHpWriteRepository rather than directly here).
                    hp_repo.upsert_result(result)
                    attempted += 1
                    if result["fetch_status"] == "OK":
                        succeeded += 1
                    else:
                        failed += 1
                    elapsed_list.append(result["elapsed_seconds"])
                    done += 1
                    if progress_cb:
                        progress_cb(done, len(candidates))
    return BatchRunReport(
        target=limit, attempted=attempted, succeeded=succeeded, failed=failed,
        retried=retried, elapsed_seconds=elapsed_list,
    )
