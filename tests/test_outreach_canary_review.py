import csv
from pathlib import Path

from scripts.outreach_canary_review import build_report


def test_canary_review_requires_five_and_keeps_unconfirmed_candidates_in_review(tmp_path):
    master = tmp_path / "master.csv"
    with master.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["No.", "タイトル", "URL", "KW"])
        writer.writeheader()
        writer.writerow({
            "No.": "1", "タイトル": "東京都の内科", "URL": "https://clinic.mynavi.jp/article/tokyo/",
            "KW": "東京都 内科",
        })
    candidates = tmp_path / "candidates.csv"
    fields = [
        "clinic_name", "clinic_uuid", "email_candidate_id", "to_email", "article_status", "template",
        "director_name_source", "phone_history_status", "recommended_title", "recommended_url",
        "recommend_reason", "candidate_schedule", "blocker_reasons", "gmail_blockers", "subject", "body",
    ]
    with candidates.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for number in range(5):
            writer.writerow({
                "clinic_name": f"医院{number}", "clinic_uuid": f"uuid-{number}",
                "email_candidate_id": str(number), "to_email": f"clinic{number}@example.jp",
                "article_status": "未確認", "template": "RECOMMEND_RELATED_V2",
                "director_name_source": "MANAGER_NAME_CONFIRMED", "phone_history_status": "UNVERIFIED",
                "recommended_title": "東京都の内科", "recommended_url": "https://clinic.mynavi.jp/article/tokyo/",
                "recommend_reason": "都道府県一致 / 標榜科一致:内科", "candidate_schedule": "（候補日時未設定）",
                "blocker_reasons": "候補日時が未設定", "gmail_blockers": "OAuth未確認",
                "subject": "中立件名",
                "body": "この度、マイナビクリニックナビにて公開されている記事について、貴院の診療科目や地域に関連する内容がございましたので、ご紹介のためご連絡いたしました。",
            })

    report, counts = build_report(candidates, master)
    assert counts == {"candidates": 5, "url_valid": 5, "neutral_copy_valid": 5, "schedule_missing": 5, "ready": 0}
    assert report.count("判定: NEEDS_REVIEW") == 5
    assert "LISTED_FIXED_V1は不適用" in report
