import csv
import json
import sqlite3
from pathlib import Path

DB=Path('data')/'clinics.sqlite3'
OUT=Path('hp_research_results_v25.csv')


def load_json(value):
    try:
        return json.loads(value or '{}')
    except Exception:
        return {}


def join(items):
    return ' / '.join(str(x) for x in items if x not in (None,'',[]))

con=sqlite3.connect(str(DB))
con.row_factory=sqlite3.Row
rows=con.execute('''
SELECT c.id AS clinic_id,c.clinic_name,c.designation_date,c.maps_website_url,r.result_json,r.rowid
FROM research_results r JOIN clinics c ON c.id=r.clinic_id
WHERE COALESCE(TRIM(c.maps_website_url),'')<>''
ORDER BY r.rowid DESC
''').fetchall()
latest={}
for row in rows:
    latest.setdefault(row['clinic_id'],row)

out=[]
for row in latest.values():
    d=load_json(row['result_json'])
    evidence=[]
    for e in d.get('treatment_evidence',[]) or []:
        if isinstance(e,dict) and float(e.get('confidence',0) or 0)>=.9:
            evidence.append(f"{e.get('category','')}｜{e.get('source','')}｜{e.get('label') or e.get('keyword','')}｜{e.get('reason','')}｜{e.get('url','')}")
    signals=[s for s in d.get('marketing_signals',[]) or [] if isinstance(s,dict)]
    midday=next((s for s in signals if s.get('name')=='昼の検査・手術専用枠'),{})
    out.append({
        'clinic_id':row['clinic_id'],'調査ロジック':'v25','医院名':row['clinic_name'],'指定年月日':row['designation_date'],
        'Google Maps HP':row['maps_website_url'],'解析HP':d.get('hp_url',''),'research_status':d.get('research_status',''),'hp_status':d.get('hp_status',''),
        'HPランク':d.get('hp_rank',''),'HPスコア':d.get('hp_score',''),
        '治療カテゴリ':join(d.get('treatment_categories',[]) or []),
        '医院名に治療名': 'あり' if d.get('treatment_name_hot') else '',
        '医院名治療カテゴリ':join(d.get('treatment_name_hot_categories',[]) or []),
        '治療カテゴリ根拠':join(evidence),
        '院長名':d.get('doctor_name',''),'卒業年':d.get('graduation_year',''),'医籍登録年':d.get('license_registration_year',''),
        '59歳以下確率':f"{d['age_probability_under_59']*100:.1f}%" if isinstance(d.get('age_probability_under_59'),(int,float)) else '',
        '年齢根拠':d.get('age_estimation_source',''),'年齢判定理由':d.get('age_estimation_reason',''),'年齢信頼度':d.get('age_estimation_confidence',''),
        '集客シグナル数':d.get('marketing_signal_count',len(signals)),
        '集客シグナル':join(s.get('name','') for s in signals),
        '集客根拠':join(f"{s.get('name','')}｜{s.get('evidence_type','')}｜{s.get('evidence','')}" for s in signals),
        'アツさ':d.get('hot_status',''),
        '昼の検査・手術枠':midday.get('procedure_slot',''),
        '昼枠曜日':midday.get('schedule_day',''),'昼枠根拠':midday.get('evidence',''),
        'HP本人確認理由':join(d.get('hp_match_reason',[]) or []),
    })

headers=list(out[0]) if out else []
with OUT.open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=headers);w.writeheader();w.writerows(out)
print(f'作成しました: {OUT.resolve()}')
print(f'全結果 {len(out)}件')
print('このCSVをChatGPTにアップロードしてください。')
