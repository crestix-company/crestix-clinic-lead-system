class BackendNotSupportedError(NotImplementedError):
    """Raised when the active backend adapter cannot serve a request.

    Used for: (1) Filters fields the Supabase adapter does not yet translate
    (their SSOT either isn't part of the 19-table Stage3 migration, e.g. the
    MHLW sidecar and the Sales Classification CSV, or their translation was
    deliberately deferred to a later stage); (2) write paths on the Supabase
    adapter, since Stage4-A is read-only for Supabase.
    """
