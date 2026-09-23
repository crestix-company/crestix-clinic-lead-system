"""環境・DB・秘密・大きなキャッシュを除いた配布ZIPを生成。"""
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[1]
SKIP_DIRS={".venv","venv","__pycache__",".pytest_cache",".git","node_modules",".cache"}


def build(output=None):
    output=Path(output or ROOT.parent/"clinic-list-filter-complete.zip")
    count=0
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(ROOT.rglob("*")):
            rel=path.relative_to(ROOT)
            if not path.is_file() or SKIP_DIRS.intersection(rel.parts):
                continue
            name=path.name.lower()
            if name.startswith(".env") or any(x in name for x in [".sqlite",".db-wal",".db-shm"]) or path.suffix.lower() in {".db",".bak",".lock",".log",".tmp",".pyc"}:
                continue
            data=path.read_bytes()
            if data.startswith(b"SQLite format 3"):
                continue
            if len(data)>15_000_000:
                raise ValueError(f"想定外の大きなファイル: {rel}")
            if re.search(rb"tvly-[A-Za-z0-9_-]{24,}",data):
                raise ValueError("APIキーらしい値が含まれるため、配布を停止しました。")
            archive.writestr(str(Path("clinic-list-filter-complete")/rel),data)
            count+=1
    print(f"{output.name}: {count} files, {output.stat().st_size:,} bytes")
    return output


if __name__=="__main__":
    build()
