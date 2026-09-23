from pathlib import Path
from io import StringIO
import os
import tempfile
import pandas as pd
from filelock import FileLock


def read_cache(path, columns):
    path = Path(path)
    if not path.exists() or not path.stat().st_size:
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return frame.reindex(columns=list(dict.fromkeys(columns + list(frame.columns))), fill_value="")


def merge_cache(path, frame, columns, keys=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=10):
        existing = read_cache(path, columns)
        combined = pd.concat([existing, frame], ignore_index=True).fillna("").astype(str)
        combined = combined.drop_duplicates(subset=keys, keep="last") if keys else combined.drop_duplicates()
        handle, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as out:
                combined.to_csv(out, index=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return combined
