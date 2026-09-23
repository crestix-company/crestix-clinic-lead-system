from streamlit.testing.v1 import AppTest
from src.utils.config import ROOT


def test_streamlit_demo_and_stale_results():
    app=AppTest.from_file(str(ROOT/"app.py"), default_timeout=45).run()
    assert not app.exception
    next(b for b in app.button if b.label=="サンプルで試す").click().run()
    assert not app.exception
    next(b for b in app.button if b.label=="判定開始").click().run()
    assert not app.exception
    assert not app.error
    assert any("最終インポート対象：6件" in x.value for x in app.success)
    assert app.session_state["completed"]["result"].final_indices==[0,1,2,3,12,13]
    next(c for c in app.checkbox if c.label=="継承・次世代院長を含める").uncheck().run()
    assert any("入力・条件が変わりました" in x.value for x in app.info)
    next(b for b in app.button if b.label=="判定開始").click().run()
    assert not app.exception
    assert 2 not in app.session_state["completed"]["result"].final_indices
