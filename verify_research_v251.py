from src.enrichment.hp_analysis import Page
from src.enrichment.profiles import estimate_profile_age
from src.scoring.research_scoring import hp_signals, treatments

print('=== v25.1 verification ===')

record_age={
    'clinic_name':'半蔵門胃腸クリニック','phone':'0332655566','address':'東京都千代田区麹町1-7-25',
    'manager_name':'掛谷 和俊','designation_date':'2004-10-01','registration_reason':'組織変更','owner_manager_equal':True,
}
age_html='''<html><body>
<h3>院長　掛谷 和俊</h3><p>1982年宮崎大学医学部卒業。消化器癌の研究で博士号を取得後、米国留学。</p>
<h3>特別顧問　新谷 弘実</h3><p>1960年順天堂大学医学部卒業。1963年に渡米。</p>
</body></html>'''
age=estimate_profile_age(record_age,[Page('https://example.com/doctor.html',age_html)],current_year=2026)
print('age_graduation_year =',age.get('graduation_year'))
print('age_pass =',age.get('graduation_year')==1982)

record={'clinic_name':'テスト眼科','phone':'0312345678','address':'東京都千代田区1-1'}
home=Page('https://example.com/','''<html><body><h1>テスト眼科</h1><p>0312345678 東京都千代田区1-1</p>
<p>【オンライン診療終了のお知らせ】当院でのオンライン診療は終了いたしました。</p>
<a href="/online/index.html">こちら オンライン診療を導入いたしました。詳しくはこちら</a></body></html>''')
online=Page('https://example.com/online/index.html','<h1>オンライン診療</h1><p>オンライン診療は終了しました。</p>')
names=[s['name'] for s in hp_signals(record,[home,online])]
print('marketing_signals =',names)
print('online_end_guard_pass =','オンライン診療' not in names)

media=Page('https://example.com/','''<h1>テスト眼科</h1><p>0312345678 東京都千代田区1-1</p>
<a href="https://doctorsfile.jp/h/1/">Doctors File クリニックホームページ制作</a>
<a href="https://ja.wordpress.org/">Powered by WordPress</a>
<div><a href="https://www.dr-bridge.co.jp/">DR.BRIDGE｜クリニックホームページ制作</a></div>''')
names2=[s['name'] for s in hp_signals(record,[media])]
print('production_signals =',names2)
print('production_guard_pass =',names2.count('HP制作会社の制作実績')==1 and 'Doctors File掲載' in names2)

clinica=Page('https://clinica-ichigaya.com/treatment/','''<h1>美容外科 施術一覧</h1>
<a href="/treatment/shiwa_naishikyo.php">内視鏡下前額除皺術</a>''')
endo=treatments([clinica],record={'clinic_name':'クリニカ市ヶ谷'})['treatment_categories']
print('clinica_ichigaya_categories =',endo)
print('endoscopy_guard_pass =','内視鏡' not in endo)

gi=Page('https://example.com/','<h1>消化器クリニック</h1><a href="/endo">大腸内視鏡検査</a>')
gi_cats=treatments([gi],record={'clinic_name':'消化器クリニック'})['treatment_categories']
print('gi_endoscopy_categories =',gi_cats)
print('gi_endoscopy_keep_pass =','内視鏡' in gi_cats)

all_pass=(
    age.get('graduation_year')==1982
    and 'オンライン診療' not in names
    and names2.count('HP制作会社の制作実績')==1
    and 'Doctors File掲載' in names2
    and '内視鏡' not in endo
    and '内視鏡' in gi_cats
)
print('=== ALL PASS ===')
print(all_pass)
