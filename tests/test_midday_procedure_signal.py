"""昼の専用枠の判定境界と、既存の加点・保存・絞り込みへの接続を確認。"""
import pytest
import unicodedata
from src.enrichment.consultation_schedule import midday_procedure,SIGNAL_NAME
from src.enrichment.hp_analysis import Page
from src.enrichment.researcher import Researcher
from src.master.filters import Filters
from src.master.samples import sample_records
from src.master.store import ClinicStore
from src.scoring.research_scoring import hp_signals,finalize_result,signal


def simple_table(procedure='検査・手術',start='12:00',end='15:00',morning_end='12:00',afternoon_start='15:00'):
    return f'''<table><caption>診療時間</caption>
      <tr><th>午前診療</th><td>9:00～{morning_end}</td></tr>
      <tr><th>{procedure}</th><td>{start}～{end}</td></tr>
      <tr><th>午後診療</th><td>{afternoon_start}～18:00</td></tr></table>'''


def page(html,url='https://clinic.example/'):
    r=sample_records()[0]
    return Page(url,f'<title>{r["clinic_name"]}</title><h1>{r["clinic_name"]}</h1><p>{r["phone"]}</p>'+html)


@pytest.mark.parametrize('label',['検査・手術','内視鏡検査（予約制）','手術専用','大腸カメラ','外来休診・検査専用'])
def test_dedicated_procedure_between_two_consultations(label):
    result=midday_procedure(simple_table(label))
    assert result is not None
    assert result['morning_consultation']=='09:00～12:00'
    assert result['procedure_slot']=='12:00～15:00'
    assert result['afternoon_consultation']=='15:00～18:00'
    assert unicodedata.normalize('NFKC',label) in result['source_excerpt']


@pytest.mark.parametrize('label',['昼休み','検査予約受付','手術の相談','検査は行っていません','検査実施なし','検査は休止','手術予定','一般診療・検査','通常外来と手術','手術は行っておりません','検査は実施しておりません','検査・手術なし','検査の説明'])
def test_break_negation_reservations_and_parallel_care_not_dedicated(label):
    assert midday_procedure(simple_table(label)) is None


@pytest.mark.parametrize('start,end',[('11:00','14:00'),('14:00','16:00'),('18:00','19:00'),('13:00','13:00'),('25:00','26:00')])
def test_overlapping_after_hours_or_invalid_interval_not_midday(start,end):
    assert midday_procedure(simple_table(start=start,end=end)) is None


def test_gap_can_include_breaks_around_the_procedure():
    result=midday_procedure(simple_table(start='13:00',end='14:00',afternoon_start='15:30'))
    assert result['procedure_slot']=='13:00～14:00'


def test_procedure_only_in_afternoon_without_later_consultation_not_counted():
    html='<table><tr><th>午前診療</th><td>9:00～12:00</td></tr><tr><th>午後手術</th><td>13:00～18:00</td></tr></table>'
    assert midday_procedure(html) is None


def test_days_across_columns_must_intersect():
    html='''<table><tr><th>診療時間</th><th>月</th><th>火</th></tr>
    <tr><th>9:00～12:00</th><td>●</td><td>●</td></tr>
    <tr><th>12:00～15:00</th><td>検査・手術</td><td>休診</td></tr>
    <tr><th>15:00～18:00</th><td>休診</td><td>●</td></tr></table>'''
    assert midday_procedure(html) is None
    html=html.replace('<td>休診</td><td>●</td>','<td>●</td><td>●</td>')
    assert midday_procedure(html)['schedule_day']=='月曜'


def test_weekday_row_layout():
    html='''<table><tr><th>曜日</th><th>午前診療</th><th>検査・手術</th><th>午後診療</th></tr>
    <tr><th>火曜日</th><td>9:00～12:00</td><td>13:00～15:00</td><td>15:30～18:00</td></tr></table>'''
    assert midday_procedure(html)['schedule_day']=='火曜'


def test_merged_cells_keep_the_same_days():
    html='''<table><tr><th>診療時間</th><th>月</th><th>火</th><th>水</th></tr>
    <tr><th>9:00～12:00</th><td>●</td><td>●</td><td>休診</td></tr>
    <tr><th>12:00～15:00</th><td colspan="2">検査・手術</td><td>休診</td></tr>
    <tr><th>15:00～18:00</th><td>●</td><td>●</td><td>休診</td></tr></table>'''
    assert midday_procedure(html)['procedure_slot']=='12:00～15:00'


def test_unexplained_symbol_not_surgery():
    html='''<table><tr><th>診療時間</th><th>月</th><th>火</th></tr>
    <tr><th>9:00～12:00</th><td>●</td><td>●</td></tr>
    <tr><th>12:00～15:00</th><td>★</td><td>★</td></tr>
    <tr><th>15:00～18:00</th><td>●</td><td>●</td></tr></table>'''
    assert midday_procedure(html) is None


@pytest.mark.parametrize('html',[
    '<section><h2>診療時間</h2><p>午前診療 9:00～12:00</p><p>検査・手術 13:00～15:00</p><p>午後診療 15:00～18:00</p></section>',
    '<div>午前診療 <span>９：００</span>～１２：００<br>検査・手術 １３：００～１５：００<br>午後診療 １５：００～１８：００</div>',
    '<section><h2>診療時間</h2><p>午前診療 9:00～12:00 / 検査・手術 13:00～15:00 / 午後診療 15:00～18:00</p></section>',
    '<dl><dt>午前診療</dt><dd>9時～12時</dd><dt>検査・手術</dt><dd>13時～15時</dd><dt>午後診療</dt><dd>午後3時～6時</dd></dl>',
])
def test_text_schedule_and_fullwidth_times(html):
    result=midday_procedure(html)
    assert result and result['procedure_slot']=='13:00～15:00'


def test_text_different_days_or_sections_not_combined():
    html='<section><p>月曜 午前診療 9:00～12:00</p><p>火曜 検査・手術 13:00～15:00</p><p>月曜 午後診療 15:00～18:00</p></section>'
    assert midday_procedure(html) is None
    html='<section><h2>A院</h2><p>午前診療 9:00～12:00</p><p>検査・手術 13:00～15:00</p><h2>B院</h2><p>午後診療 15:00～18:00</p></section>'
    assert midday_procedure(html) is None


def test_images_and_generic_surgery_mention_do_not_add_point():
    html='<h2>日帰り手術に対応</h2><img src="hours.png" alt="診療時間">'
    assert midday_procedure(html) is None
    html='<section><p>午前診療 9:00～12:00</p><p>昼休みは検査と手術を行います。</p><p>午後診療 15:00～18:00</p></section>'
    assert midday_procedure(html) is None


def test_point_is_once_per_clinic_and_requires_two_for_hot():
    p=page(simple_table())
    signals=hp_signals(sample_records()[0],[p,p])
    assert [s['name'] for s in signals]==[SIGNAL_NAME]
    assert signals[0]['evidence_url']==p.url
    assert '12:00～15:00' in signals[0]['evidence']
    result=finalize_result({'marketing_signals':signals})
    assert result['marketing_signal_count']==1 and result['hot_status']=='集客投資シグナルあり'
    result=finalize_result({'marketing_signals':signals+[signal('YouTube公式運用',p.url,'test','公式チャンネル')]})
    assert result['marketing_signal_count']==2 and result['hot_status']=='アツい'


def test_nonofficial_directory_cannot_add_schedule_point():
    assert hp_signals(sample_records()[0],[page(simple_table(),url='https://job-medley.com/facility/1/')])==[]


def test_hp_research_storage_and_filter_without_new_search(tmp_path):
    store=ClinicStore(tmp_path/'clinics.db')
    store.import_master(sample_records()[:1])
    record=store.query(Filters(hp_only=False))[0]
    class Search:
        def search(self,*args,**kwargs):
            raise AssertionError('This feature must not add a search for a known HP')
    class Fetcher:
        def fetch(self,url,allowed_host=None):
            return page(simple_table(),url=url)
    result,pages=Researcher(Search(),Fetcher()).hp({**record,'hp_url':'https://clinic.example/'})
    store.save_research(record['id'],result,pages)
    selected=store.query(Filters(hp_only=True,signals=[SIGNAL_NAME]))
    assert len(selected)==1 and selected[0]['marketing_signal_count']==1
    assert selected[0]['hot_status']=='集客投資シグナルあり'
    store.override(record['id'],'marketing_signals',[SIGNAL_NAME,'YouTube公式運用'],note='目視確認')
    assert store.get(record['id'])['hot_status']=='アツい'
