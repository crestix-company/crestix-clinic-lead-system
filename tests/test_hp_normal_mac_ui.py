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
        if name == "multiselect":
            def multiselect(label, options, **kwargs):
                self.labels.append((name, label))
                if "Job対象" in label:
                    return [7] if 7 in options else []
                return kwargs.get("default", [])
            return multiselect
        if name == "checkbox":
            return lambda label, **kwargs: (self.labels.append((name, label)) or True)
        if name == "columns":
            return lambda count: [type("Column", (), {"metric": lambda self, *a, **k: None})() for _ in range(count)]
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
    monkeypatch.setattr(app_v2, "job_status", lambda _store, jid: {
        "id": jid, "status": "RUNNING", "counts": {"PENDING": 1}, "total": 2,
    })
    progress = []
    monkeypatch.setattr(app_v2, "show_job_progress", lambda _store, jid, **_kwargs: progress.append(jid))
    monkeypatch.setattr(app_v2, "runner_for", lambda _path: type("Runner", (), {"running": lambda self: False})())
    monkeypatch.setattr(app_v2, "_maps_hp_available_count", lambda *_args: 1)

    class Store:
        is_supabase_runtime = True
        path = "supabase"

        def prefectures(self, *, read_only_ui=False):
            assert read_only_ui
            return ["東京都"]

        def maps_hp_status_count(self, _pref, _medical_types, statuses):
            return 1 if statuses == ["UNRESEARCHED"] else 0

        def maps_hp_status_candidates(self, *_args, **_kwargs):
            return [{"id": 7, "clinic_name": "医院A", "address": "東京", "medical_type": "医科",
                     "maps_website_url": "https://clinic.example", "hp_status": "UNRESEARCHED"}]

        def hp_normal_recent_jobs(self):
            return [{"id": "job-1", "kind": "hp", "status": "RUNNING", "created_at": "now"}]

        def hp_normal_job_status(self, jid):
            return {"id": jid, "status": "RUNNING", "counts": {"PENDING": 1}, "total": 2}

        def hp_normal_job_results(self, jid):
            return [{"clinic_id": 7, "clinic_name": "医院A", "address": "東京", "item_state": "DONE",
                     "item_result": "SUCCESS", "fetch_status": "OK", "published_hp_rank": "A",
                     "final_url": "https://clinic.example/"}]

    app_v2.hp_normal_research_ui(Store())

    assert progress == ["job-1", "job-1"]  # current job and selectable history progress
    assert any("状態" in str(label) for _kind, label in fake_st.labels)
    assert any(isinstance(frame, pd.DataFrame) and "HP調査状態" in frame.columns for frame in fake_st.frames)
    assert any(isinstance(frame, pd.DataFrame) and "公開ランク" in frame.columns for frame in fake_st.frames)
    assert "選択医院でHP調査Jobを作成" in fake_st.buttons
    assert "このJobのWorkerを開始" in fake_st.buttons
    assert all("Email" not in label and "Maps結果を取り込む" not in label for label in fake_st.buttons)
