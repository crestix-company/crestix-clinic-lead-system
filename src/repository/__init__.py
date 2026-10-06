"""Stage4-A Repository / DAL foundation.

UI/scripts -> Repository (contracts.py) -> Adapter (sqlite_adapter.py / supabase_adapter.py).

This package is purely additive: no existing runtime file (app_v2.py, src/master/store.py, ...)
is modified or imported-from-here-back. Default backend stays SQLite (see backend.py); Supabase
is only used when explicitly selected via CLINIC_DATA_BACKEND=supabase.
"""
