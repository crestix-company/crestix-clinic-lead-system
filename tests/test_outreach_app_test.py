from pathlib import Path
from streamlit.testing.v1 import AppTest


class Store:
    is_supabase_runtime = False

    def query(self, filters=None, limit=100, offset=0, as_of=None):
        return []

    def prefectures(self):
        return ["東京都"]


def test_outreach_screen_apptest_fails_closed_without_formal_email_connection():
    script = Path(__file__).with_name("_outreach_app_test_offline.py")
    script.write_text('''
from src.outreach_ui import outreach_email_page
class Store:
    is_supabase_runtime=False
    def query(self,*a,**k): return []
    def prefectures(self): return ["東京都"]
def formal_page(_page): raise RuntimeError("offline")
outreach_email_page(Store(), formal_email_page=formal_page)
''')
    try:
        at = AppTest.from_file(str(script)).run()
    finally:
        script.unlink(missing_ok=True)
    assert not at.exception
    assert any("正式利用可能数を取得できません" in element.value for element in at.error)


def test_outreach_screen_apptest_renders_dynamic_formal_count_and_history_hold():
    script = Path(__file__).with_name("_outreach_app_test_ok.py")
    script.write_text('''
from src.outreach_ui import outreach_email_page
class Store:
    is_supabase_runtime=False
    def query(self,*a,**k): return []
    def prefectures(self): return ["東京都"]
def formal_page(page, after_clinic_id=None):
    return {"rows": [(1,"clinic@example.jp")] if page==1 else [], "total":1, "page":page, "page_size":500, "pages":1}
outreach_email_page(Store(), formal_email_page=formal_page)
''')
    try:
        at = AppTest.from_file(str(script)).run()
    finally:
        script.unlink(missing_ok=True)
    assert not at.exception
    assert any(metric.label == "正式なEmail営業利用可能数（動的）" and metric.value == "1" for metric in at.metric)
    assert any("Campaign ID" in item.label for item in at.text_input)
