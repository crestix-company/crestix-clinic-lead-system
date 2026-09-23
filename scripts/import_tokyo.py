from pathlib import Path
import argparse
import sys
import json
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.enrichment.kouseikyoku_source import parse_kanto_excel, download_tokyo_master, OFFICIAL_INDEX
from src.utils.config import ROOT


def main():
    parser = argparse.ArgumentParser(description="東京都公式マスタの更新")
    parser.add_argument("--file", help="手動保存した東京都の厚生局Excel")
    args = parser.parse_args()
    if args.file:
        raw = Path(args.file).read_bytes()
        master = parse_kanto_excel(raw, source_url=OFFICIAL_INDEX)
    else:
        master, raw, name = download_tokyo_master()
        (ROOT/"data/raw"/name).write_bytes(raw)
    path = ROOT / "data/master/tokyo_current.csv"
    tmp = path.with_suffix(".tmp")
    master.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)
    summary = {"records": len(master), "as_of": sorted(set(master["as_of"])),
               "status": master["status"].value_counts().to_dict(),
               "facility_type": master["facility_type"].value_counts().to_dict(),
               "source": OFFICIAL_INDEX}
    (ROOT/"data/master/tokyo_current.metadata.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
