import csv
from scripts.audit_outreach_historical_choices import audit


def test_historical_audit_uses_reviewed_denominator(tmp_path):
    articles = tmp_path / "articles.csv"
    reviews = tmp_path / "reviews.csv"
    with articles.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["URL"])
        w.writeheader()
        w.writerow({"URL": "https://clinic.mynavi.jp/article/example/"})
    columns = ["No", "医院名", "選定状態", "人間選定URL",
               "AI第一推奨URL", "AI第2候補URL", "AI第3候補URL"]
    with reviews.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerow({"No": "1", "医院名": "A", "選定状態": "一致",
                    "人間選定URL": "https://clinic.mynavi.jp/article/example/",
                    "AI第一推奨URL": "https://clinic.mynavi.jp/article/example/"})
        w.writerow({"No": "2", "医院名": "B", "選定状態": "未レビュー"})
    summary = audit(reviews, articles)
    assert summary["reviewed"] == 1
    assert summary["top1_exact"] == 1
    assert summary["counts"]["未レビュー"] == 1
