from contextlib import nullcontext

import pandas as pd

import app_v2


class FakeStreamlit:
    def __init__(self):
        self.session_state = {"normal_hp_active_job": "job-1"}
        self.labels = []
        self.frames = []
        self.buttons = []

    def __getattr__(self, name):
        if name in {"header", "caption", "metric", "subheader", "info", "warning", "error"}:
            return lambda *args, **kwargs: self.labels.append((name, args[0] if args else ""))
        if name == "selectbox":
            return lambda label, options, **kwargs: (self.labels.append((name, label)) or list(options)[0])
        if name == "number_input":
            return lambda label, **kwargs: (self.labels.append((name, label)) or kwargs["value"])
        if name == "button":
            return lambda label, **kwargs: (self.buttons.append(label) or False)
        if name == "dataframe":
            return lambda frame, **kwargs: self.frames.append(frame)
        if name == "expander":
            return lambda *args, **kwargs: nullcontext()
        if name == "rerun":
            return lambda: None
        raise AttributeError(name)


def test_normal_hp_screen_searches_candidates_and_reads_selected_job_progress(monkeypatch):
    fake_st = FakeStreamlit()
    monkeypatch.setattr(app_v2, "st", fake_st)
    monkeypatch.setattr(app_v2, "recent_jobs", lambda _store: [{"id": "job-1", "kind": "hp", "status": "RUNNING"}])
    monkeypatch.setattr(app_v2, "job_status", lambda _store, jid: {
        "id": jid, "status": "RUNNING", "counts": {"PENDING": 1}, "total": 2,
    })
    progress = []
    monkeypatch.setattr(app_v2, "show_job_progress", lambda _store, jid: progress.append(jid))
    monkeypatch.setattr(app_v2, "runner_for", lambda _path: type("Runner", (), {"running": lambda self: False})())
    monkeypatch.setattr(app_v2, "_maps_hp_available_count", lambda *_args: 1)

    class Store:
        is_supabase_runtime = True
        path = "supabase"

        def prefectures(self, *, read_only_ui=False):
            assert read_only_ui
            return ["東京都"]

        def maps_hp_candidate_preview(self, *args, **kwargs):
            return [{"id": 7, "clinic_name": "医院A", "address": "東京", "medical_type": "医科",
                     "maps_website_url": "https://clinic.example"}]

    app_v2.hp_normal_research_ui(Store())

    assert progress == ["job-1", "job-1"]  # current job and selectable history progress
    assert any("未調査対象" in str(label) for _kind, label in fake_st.labels)
    assert any(isinstance(frame, pd.DataFrame) and "医院名" in frame.columns for frame in fake_st.frames)
    assert "HP調査Jobを作成" in fake_st.buttons
    assert "このJobのWorkerを開始" in fake_st.buttons
    assert all("Email" not in label and "Maps結果を取り込む" not in label for label in fake_st.buttons)
