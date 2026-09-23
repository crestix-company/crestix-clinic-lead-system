from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def read_config(path):
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("設定ファイルはYAMLの辞書形式にしてください。")
    return value


def settings():
    return read_config(ROOT / "config/settings.yml")
