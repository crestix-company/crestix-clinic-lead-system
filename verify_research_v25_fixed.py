from src.enrichment.hp_analysis import Page
from src.enrichment.profiles import estimate_profile_age
from src.scoring.research_scoring import hp_signals, treatments

print("=== v25 quick check ===")

# 1) 年齢推定：院長本人の経歴ブロックを優先できるか
record = {
    "clinic_name": "テストクリニック",
    "phone": "0312345678",
    "address": "東京都千代田区1-1",
    "manager_name": "吉野 真未",
    "designation_date": "2003-04-01",
    "registration_reason": "新規",
    "owner_manager_equal": False,
}

age_html = """
<section>
  <h2>院長 吉野 真未</h2>
  <p>経歴 1994年 東海大学 医学部 卒業 / 医師国家試験合格</p>
</section>
<section>
  <h2>理事長 深川 和己</h2>
  <p>経歴 1989年 慶應義塾大学医学部卒業</p>
</section>
"""

age = estimate_profile_age(
    record,
    [Page("https://example.com/doctor", age_html)],
    current_year=2026,
)

age_year = age.get("graduation_year")
print("age_graduation_year =", age_year)
print("age_person_split_pass =", age_year == 1994)

# 2) 集客施策：終了済みオンライン診療 / WordPress / 二重加点を除外できるか
signal_page = Page(
    "https://example.com/",
    """
    <html><body>
      <h1>テストクリニック</h1>
      <p>0312345678 東京都千代田区1-1</p>
      <a href="https://doctorsfile.jp/h/1/">Doctors File クリニックホームページ制作</a>
      <a href="https://ja.wordpress.org/">Powered by WordPress</a>
      <a href="/online">オンライン診療終了のお知らせ</a>
    </body></html>
    """,
)

signals = hp_signals(record, [signal_page])
signal_names = [x.get("name") for x in signals]

print("marketing_signals =", signal_names)
print(
    "marketing_guard_pass =",
    "Doctors File掲載" in signal_names
    and "HP制作会社の制作実績" not in signal_names
    and "オンライン診療" not in signal_names,
)

# 3) 内視鏡：美容等の「内視鏡下手術」だけでは消化器内視鏡にしない
non_gi_page = Page(
    "https://example.com/",
    """
    <html><body>
      <h1>テストクリニック</h1>
      <a href="/op">内視鏡下手術</a>
    </body></html>
    """,
)

gi_page = Page(
    "https://example.com/",
    """
    <html><body>
      <h1>テストクリニック</h1>
      <a href="/endo">大腸内視鏡検査</a>
    </body></html>
    """,
)

non_gi_categories = treatments([non_gi_page], record=record).get("treatment_categories", [])
gi_categories = treatments([gi_page], record=record).get("treatment_categories", [])

print("endoscopy_non_gi =", non_gi_categories)
print("endoscopy_gi =", gi_categories)
print(
    "endoscopy_guard_pass =",
    "内視鏡" not in non_gi_categories and "内視鏡" in gi_categories,
)

all_pass = (
    age_year == 1994
    and "Doctors File掲載" in signal_names
    and "HP制作会社の制作実績" not in signal_names
    and "オンライン診療" not in signal_names
    and "内視鏡" not in non_gi_categories
    and "内視鏡" in gi_categories
)

print("=== all pass? ===")
print(all_pass)
