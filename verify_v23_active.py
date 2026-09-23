from pathlib import Path
import hashlib

print("=== v23 local verification ===")

root = Path.cwd()
rs = root / "src/scoring/research_scoring.py"
cs = root / "src/enrichment/consultation_schedule.py"

EXPECTED_RS = "35c5d28c8f31d577703c12b128df7b5c0ba519f411ba25accb6053e0e1521b04"
EXPECTED_CS = "c977e4a41cc39d339adb73f36b0a59bf79f5cc6ec944680c7eb7efb561998cf4"

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

for label, path, expected in [
    ("research_scoring.py", rs, EXPECTED_RS),
    ("consultation_schedule.py", cs, EXPECTED_CS),
]:
    if not path.exists():
        print(label, "MISSING")
        continue
    digest = sha(path)
    print(label)
    print("  sha256 =", digest)
    print("  expected =", expected)
    print("  source_match =", digest == expected)

try:
    from src.scoring import research_scoring as r
    cfg = r.read_config(r.ROOT / "config/treatment_keywords.yml")

    diabetes = r._category_term(
        "糖尿病",
        "糖尿病網膜症",
        r._treatment_terms("糖尿病", cfg["糖尿病"]),
    )
    uro = r._category_term(
        "泌尿器科",
        "ED治療・男性更年期",
        r._treatment_terms("泌尿器科", cfg["泌尿器科"]),
    )

    print()
    print("糖尿病網膜症 -> 糖尿病カテゴリ:", diabetes)
    print("ED治療・男性更年期 -> 泌尿器科カテゴリ:", uro)
    print("category_guard_pass =", diabetes is None and uro is None)

except Exception as e:
    print("category test error =", repr(e))

try:
    from src.enrichment.consultation_schedule import midday_procedure

    html = """
    <html><body>
    <table>
      <tr><th>時間</th><th>内容</th></tr>
      <tr><td>午前 09:00～12:00</td><td>診療</td></tr>
      <tr><td></td><td>手術</td></tr>
      <tr><td>午後 15:00～18:00</td><td>診療</td></tr>
    </table>
    </body></html>
    """

    result = midday_procedure(html)
    print()
    print("midday synthetic result =", result)
    print("midday_guard_pass =", bool(result))

except Exception as e:
    print("midday test error =", repr(e))

print()
print("=== 判定 ===")
print("上の source_match / category_guard_pass / midday_guard_pass がすべて True なら v23 本体は正しく入っています。")
print("どれか False なら、今回の v23 パッチが実際のアプリ本体に反映されていません。")
