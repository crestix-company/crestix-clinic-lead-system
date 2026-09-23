"""手動保存HTMLの構造差を報告。2ページの差だけから契約の有無は学習しない。"""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from src.enrichment.epark_checker import EparkChecker, canonical_epark_url
from src.utils.config import settings


def features(path):
    raw = Path(path).read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    url = canonical_epark_url(canonical.get("href", "")) if canonical else ""
    if not url:
        raise ValueError("canonical URLを含むEPARKの保存HTMLが必要です。")
    result = EparkChecker(settings()["epark"]).inspect_html(url, raw)
    classes = sorted({cls for tag in soup.find_all(True) for cls in tag.get("class", [])})
    return json.loads(result.features_json), set(classes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paid", required=True)
    parser.add_argument("--free", required=True)
    parser.add_argument("--output", default="epark_comparison.json")
    args = parser.parse_args()
    pf, pc = features(args.paid); ff, fc = features(args.free)
    result = {"paid_features":pf, "free_features":ff, "only_paid_classes":sorted(pc-fc),
              "only_free_classes":sorted(fc-pc), "conclusion":"構造の比較のみ。課金専用要素の検証は別途必要"}
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("HTML比較結果を保存しました。課金判定ルールは自動変更していません。")


if __name__=="__main__":
    main()
