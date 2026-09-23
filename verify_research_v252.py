from src.enrichment.hp_analysis import Page
from src.enrichment.profiles import estimate_profile_age, graduation_evidence
from src.scoring.research_scoring import hp_signals, treatments

print("=== v25.2 verification ===")

# 半蔵門胃腸クリニック実ページ構造の再現:
# 院長の1982年卒業の後に、特別顧問の1960年卒業・1963年渡米が続く。
record={
    "clinic_name":"医療法人社団 荘和会 半蔵門胃腸クリニック",
    "phone":"0332655566",
    "address":"東京都千代田区麹町1-7-25",
    "manager_name":"掛谷 和俊",
    "designation_date":"2004-10-01",
    "registration_reason":"組織変更",
    "owner_manager_equal":True,
}
html="""
<html><body>
<h2>ドクター紹介</h2>
<h3>院長　掛谷和俊</h3>
<p>1982年宮崎大学医学部卒業。消化器癌の研究で博士号を取得後、新谷弘実教授に師事し米国に留学。</p>
<p>20万例以上の胃・大腸内視鏡検査及びポリープ切除術を行っています。</p>
<h3>特別顧問　新谷弘実</h3>
<p>1960年順天堂大学医学部卒業。1963年に渡米し、内視鏡の挿入技術を考案。</p>
</body></html>
"""
pages=[Page("https://hanzomon-icho-clinic.com/doctor.html",html)]
ev=graduation_evidence(record,pages)
age=estimate_profile_age(record,pages,current_year=2026)
print("hanzomon_evidence =", [(x.get("year"),x.get("doctor_name")) for x in ev])
print("hanzomon_graduation_year =", age.get("graduation_year"))
hanzomon_pass=(age.get("graduation_year")==1982 and {x.get("year") for x in ev}=={1982})
print("hanzomon_person_boundary_pass =", hanzomon_pass)

# 一般的な複数医師ページでも次の「医師 氏名」で区切る
multi_record={
    "clinic_name":"テスト眼科",
    "phone":"0312345678",
    "address":"東京都",
    "manager_name":"吉野 真未",
    "designation_date":"2003-04-01",
    "registration_reason":"新規",
    "owner_manager_equal":False,
}
multi_html="""
<html><body>
<h3>院長 吉野真未</h3><p>1994年 東海大学医学部 卒業。</p>
<h3>医師 岩崎美紀</h3><p>2013年 慶應義塾大学医学部 卒業。</p>
</body></html>
"""
multi=estimate_profile_age(multi_record,[Page("https://example.com/doctor",multi_html)],current_year=2026)
print("multi_doctor_graduation_year =",multi.get("graduation_year"))
multi_pass=(multi.get("graduation_year")==1994)
print("multi_doctor_boundary_pass =",multi_pass)

# v25.1の誤検出ガードを壊していないことも確認
sig_page=Page(
    "https://example.com/",
    """<h1>テストクリニック</h1>
    <a href="https://doctorsfile.jp/h/1/">Doctors File</a>
    <a href="https://ja.wordpress.org/">Powered by WordPress</a>
    <a href="/online">オンライン診療終了のお知らせ</a>"""
)
signals=[x.get("name") for x in hp_signals(multi_record,[sig_page])]
marketing_pass=(
    "Doctors File掲載" in signals
    and "オンライン診療" not in signals
    and "HP制作会社の制作実績" not in signals
)
print("marketing_guard_pass =",marketing_pass)

non_gi=Page("https://example.com/",'<h1>テストクリニック</h1><a href="/op">内視鏡下前額除皺術</a>')
gi=Page("https://example.com/",'<h1>テストクリニック</h1><a href="/endo">大腸内視鏡検査</a>')
non_gi_cats=treatments([non_gi],record=multi_record).get("treatment_categories",[])
gi_cats=treatments([gi],record=multi_record).get("treatment_categories",[])
endo_pass=("内視鏡" not in non_gi_cats and "内視鏡" in gi_cats)
print("endoscopy_guard_pass =",endo_pass)

print("=== ALL PASS ===")
print(hanzomon_pass and multi_pass and marketing_pass and endo_pass)
