# Repository restructure move manifest

Only paths with a confirmed role move are listed. Each line records the tracked path at the baseline HEAD and its destination. Reasons are grouped by responsibility below.

## Reasons

- `src/repository/` → `src/database/`: these modules own persistence adapters, read repositories, write adapters, connection selection, and CRUD boundaries.
- `src/master/` → `src/services/`: business rules and workflows; Maps acquisition/queue modules and fixed exporter are split into their specific packages.
- `src/enrichment/` → `src/research/`: HP, email, identity, and treatment research; the MHLW source reader moves to collectors.
- UI modules move to `src/ui/`; output writers move to `src/exporters/`; `src/pipeline.py` moves to `src/services/pipeline.py`.
- Windows scripts, import commands, and maintenance-only scripts move under their corresponding `scripts/` subfolders. Old call paths are retained as wrappers where users or setup scripts depend on them.
- V1 `app.py`, synthetic sample files, and obsolete one-time patchers move to `old/`; `scripts/build_release.py` excludes the entire `old/` tree.
- `src/models/` is intentionally not created: shared data types are still defined next to the service that owns them, and there is no pure model-only module to move without adding abstraction.

## File-by-file moves

- `app.py` → `old/legacy_apps/app.py`
- `samples/html/hpl900000002.html` → `old/samples/html/hpl900000002.html`
- `samples/html/hpl900000003.html` → `old/samples/html/hpl900000003.html`
- `samples/sample_comdesk.csv` → `old/samples/sample_comdesk.csv`
- `samples/sample_comdesk.xlsx` → `old/samples/sample_comdesk.xlsx`
- `samples/sample_comdesk_cp932.csv` → `old/samples/sample_comdesk_cp932.csv`
- `samples/sample_doctor_license.csv` → `old/samples/sample_doctor_license.csv`
- `samples/sample_enrichment.csv` → `old/samples/sample_enrichment.csv`
- `samples/sample_epark_confirmed.csv` → `old/samples/sample_epark_confirmed.csv`
- `samples/sample_kouseikyoku.csv` → `old/samples/sample_kouseikyoku.csv`
- `scripts/create_windows_desktop_shortcut.ps1` → `scripts/windows/create_windows_desktop_shortcut.ps1`
- `scripts/generate_samples.py` → `scripts/maintenance/generate_samples.py`
- `scripts/import_tokyo.py` → `scripts/import/import_tokyo.py`
- `scripts/launch_v2_windows.ps1` → `scripts/windows/launch_v2_windows.ps1`
- `scripts/manual_ops/test_midday_real_clinic.py` → `scripts/maintenance/manual_midday_real_clinic.py`
- `scripts/national_append_only_import.py` → `scripts/import/national_append_only_import.py`
- `scripts/update_and_launch_windows.ps1` → `scripts/windows/update_and_launch_windows.ps1`
- `setup_v2_windows.ps1` → `scripts/windows/setup_v2_windows.ps1`
- `src/call_desk_ui.py` → `src/ui/call_desk_ui.py`
- `src/comdesk_hold_review_ui.py` → `src/ui/comdesk_hold_review_ui.py`
- `src/comdesk_uuid_preview_ui.py` → `src/ui/comdesk_uuid_preview_ui.py`
- `src/dental_sales_tags_ui.py` → `src/ui/dental_sales_tags_ui.py`
- `src/email_human_review_ui.py` → `src/ui/email_human_review_ui.py`
- `src/email_research_ui.py` → `src/ui/email_research_ui.py`
- `src/enrichment/__init__.py` → `src/research/__init__.py`
- `src/enrichment/alias_rescue.py` → `src/research/alias_rescue.py`
- `src/enrichment/candidate_map.py` → `src/research/candidate_map.py`
- `src/enrichment/consultation_schedule.py` → `src/research/consultation_schedule.py`
- `src/enrichment/doctor_license.py` → `src/research/doctor_license.py`
- `src/enrichment/email_research/__init__.py` → `src/research/email_research/__init__.py`
- `src/enrichment/email_research/ai_overview_adapter.py` → `src/research/email_research/ai_overview_adapter.py`
- `src/enrichment/email_research/candidate_prefetch.py` → `src/research/email_research/candidate_prefetch.py`
- `src/enrichment/email_research/candidate_verifier.py` → `src/research/email_research/candidate_verifier.py`
- `src/enrichment/email_research/clinic_identity.py` → `src/research/email_research/clinic_identity.py`
- `src/enrichment/email_research/email_context_extractor.py` → `src/research/email_research/email_context_extractor.py`
- `src/enrichment/email_research/email_extraction.py` → `src/research/email_research/email_extraction.py`
- `src/enrichment/email_research/email_role_classifier.py` → `src/research/email_research/email_role_classifier.py`
- `src/enrichment/email_research/formal_pipeline.py` → `src/research/email_research/formal_pipeline.py`
- `src/enrichment/email_research/official_hp_email_crawler.py` → `src/research/email_research/official_hp_email_crawler.py`
- `src/enrichment/email_research/official_site_trust.py` → `src/research/email_research/official_site_trust.py`
- `src/enrichment/email_research/pipeline.py` → `src/research/email_research/pipeline.py`
- `src/enrichment/email_research/primary_email_selector.py` → `src/research/email_research/primary_email_selector.py`
- `src/enrichment/email_research/search_candidate_adapter.py` → `src/research/email_research/search_candidate_adapter.py`
- `src/enrichment/email_research/search_provider_factory.py` → `src/research/email_research/search_provider_factory.py`
- `src/enrichment/epark_checker.py` → `src/research/epark_checker.py`
- `src/enrichment/hp_analysis.py` → `src/research/hp_analysis.py`
- `src/enrichment/kouseikyoku_source.py` → `src/collectors/kouseikyoku_source.py`
- `src/enrichment/profiles.py` → `src/research/profiles.py`
- `src/enrichment/researcher.py` → `src/research/researcher.py`
- `src/enrichment/safe_web.py` → `src/research/safe_web.py`
- `src/enrichment/search_provider.py` → `src/research/search_provider.py`
- `src/enrichment/treatment_context.py` → `src/research/treatment_context.py`
- `src/enrichment/treatment_taxonomy.py` → `src/research/treatment_taxonomy.py`
- `src/hp_human_review_ui.py` → `src/ui/hp_human_review_ui.py`
- `src/io/output_writer.py` → `src/exporters/output_writer.py`
- `src/master/__init__.py` → `src/services/__init__.py`
- `src/master/clinic_identity.py` → `src/services/clinic_identity.py`
- `src/master/comdesk.py` → `src/services/comdesk.py`
- `src/master/comdesk_cloud_import.py` → `src/services/comdesk_cloud_import.py`
- `src/master/comdesk_hold_review.py` → `src/services/comdesk_hold_review.py`
- `src/master/comdesk_reconciliation.py` → `src/services/comdesk_reconciliation.py`
- `src/master/comdesk_snapshot.py` → `src/services/comdesk_snapshot.py`
- `src/master/comdesk_snapshot_import.py` → `src/services/comdesk_snapshot_import.py`
- `src/master/comdesk_sync.py` → `src/services/comdesk_sync.py`
- `src/master/data_paths.py` → `src/services/data_paths.py`
- `src/master/dental_sales_tags.py` → `src/services/dental_sales_tags.py`
- `src/master/email_auto_run.py` → `src/services/email_auto_run.py`
- `src/master/email_human_review.py` → `src/services/email_human_review.py`
- `src/master/email_research_config.py` → `src/services/email_research_config.py`
- `src/master/email_sales_eligibility.py` → `src/services/email_sales_eligibility.py`
- `src/master/filters.py` → `src/services/filters.py`
- `src/master/fixed_export.py` → `src/exporters/fixed_export.py`
- `src/master/google_maps.py` → `src/collectors/google_maps.py`
- `src/master/hp_auto_run.py` → `src/services/hp_auto_run.py`
- `src/master/hp_auto_state.py` → `src/services/hp_auto_state.py`
- `src/master/hp_batch_metrics.py` → `src/services/hp_batch_metrics.py`
- `src/master/hp_effective_rank.py` → `src/services/hp_effective_rank.py`
- `src/master/hp_human_review.py` → `src/services/hp_human_review.py`
- `src/master/hp_research_batch.py` → `src/services/hp_research_batch.py`
- `src/master/hp_site_type.py` → `src/services/hp_site_type.py`
- `src/master/identity_contract.py` → `src/services/identity_contract.py`
- `src/master/jobs.py` → `src/services/jobs.py`
- `src/master/kouseikyoku_cloud_import.py` → `src/services/kouseikyoku_cloud_import.py`
- `src/master/maps_cloud_import.py` → `src/services/maps_cloud_import.py`
- `src/master/maps_collection_tasks.py` → `src/services/maps_collection_tasks.py`
- `src/master/matching.py` → `src/services/matching.py`
- `src/master/national_maps_queue.py` → `src/collectors/national_maps_queue.py`
- `src/master/reintegration.py` → `src/services/reintegration.py`
- `src/master/research_sidecar.py` → `src/services/research_sidecar.py`
- `src/master/sales_classification.py` → `src/services/sales_classification.py`
- `src/master/sales_treatments.py` → `src/services/sales_treatments.py`
- `src/master/samples.py` → `src/services/samples.py`
- `src/master/scope.py` → `src/services/scope.py`
- `src/master/store.py` → `src/services/store.py`
- `src/master/treatment_incremental.py` → `src/services/treatment_incremental.py`
- `src/outreach/auto_draft_ui.py` → `src/ui/outreach/auto_draft_ui.py`
- `src/outreach/pre_send_ui.py` → `src/ui/outreach/pre_send_ui.py`
- `src/outreach_ui.py` → `src/ui/outreach_ui.py`
- `src/pipeline.py` → `src/services/pipeline.py`
- `src/repository/__init__.py` → `src/database/__init__.py`
- `src/repository/backend.py` → `src/database/backend.py`
- `src/repository/comdesk_preview.py` → `src/database/comdesk_preview.py`
- `src/repository/comdesk_snapshot_repository.py` → `src/database/comdesk_snapshot_repository.py`
- `src/repository/comdesk_writer_safety.py` → `src/database/comdesk_writer_safety.py`
- `src/repository/connection_diagnostics.py` → `src/database/connection_diagnostics.py`
- `src/repository/contracts.py` → `src/database/contracts.py`
- `src/repository/cutover.py` → `src/database/cutover.py`
- `src/repository/dental_sales_tag_repository.py` → `src/database/dental_sales_tag_repository.py`
- `src/repository/email_cohort.py` → `src/database/email_cohort.py`
- `src/repository/email_database.py` → `src/database/email_database.py`
- `src/repository/email_human_review_repository.py` → `src/database/email_human_review_repository.py`
- `src/repository/email_research_repository.py` → `src/database/email_research_repository.py`
- `src/repository/email_target_reader.py` → `src/database/email_target_reader.py`
- `src/repository/errors.py` → `src/database/errors.py`
- `src/repository/high_range_id.py` → `src/database/high_range_id.py`
- `src/repository/hp_auto_run_repository.py` → `src/database/hp_auto_run_repository.py`
- `src/repository/hp_human_review_repository.py` → `src/database/hp_human_review_repository.py`
- `src/repository/hp_sqlite_write_adapter.py` → `src/database/hp_sqlite_write_adapter.py`
- `src/repository/hp_supabase_write_adapter.py` → `src/database/hp_supabase_write_adapter.py`
- `src/repository/hp_targets.py` → `src/database/hp_targets.py`
- `src/repository/hp_write_backend.py` → `src/database/hp_write_backend.py`
- `src/repository/hp_write_contracts.py` → `src/database/hp_write_contracts.py`
- `src/repository/maps_collection_tasks_repository.py` → `src/database/maps_collection_tasks_repository.py`
- `src/repository/outreach_delivery_repository.py` → `src/database/outreach_delivery_repository.py`
- `src/repository/runtime_store.py` → `src/database/runtime_store.py`
- `src/repository/shadow.py` → `src/database/shadow.py`
- `src/repository/sqlite_adapter.py` → `src/database/sqlite_adapter.py`
- `src/repository/sqlite_write_adapter.py` → `src/database/sqlite_write_adapter.py`
- `src/repository/supabase_adapter.py` → `src/database/supabase_adapter.py`
- `src/repository/supabase_filters.py` → `src/database/supabase_filters.py`
- `src/repository/supabase_pool.py` → `src/database/supabase_pool.py`
- `src/repository/supabase_write_adapter.py` → `src/database/supabase_write_adapter.py`
- `src/repository/treatment_sqlite_write_adapter.py` → `src/database/treatment_sqlite_write_adapter.py`
- `src/repository/treatment_supabase_write_adapter.py` → `src/database/treatment_supabase_write_adapter.py`
- `src/repository/treatment_write_backend.py` → `src/database/treatment_write_backend.py`
- `src/repository/treatment_write_contracts.py` → `src/database/treatment_write_contracts.py`
- `src/repository/write_backend.py` → `src/database/write_backend.py`
- `src/repository/write_contracts.py` → `src/database/write_contracts.py`
- `update_hp_research_fix.py` → `old/legacy_scripts/update_hp_research_fix.py`
- `update_midday_procedure_signal.py` → `old/legacy_scripts/update_midday_procedure_signal.py`

## Archived in `old/`

- `app.py` → `old/legacy_apps/app.py`: V1 entrypoint; `start_windows.bat` now forwards to the supported V2 entrypoint.
- `samples/` → `old/samples/`: only the archived V1 app and synthetic sample generator used these paths.
- `update_hp_research_fix.py` and `update_midday_procedure_signal.py` → `old/legacy_scripts/`: one-time hash-patching tools target the pre-restructure code; their historical docs now warn against running them.
- No clinic DB, production backup, credential, `config/`, `data/master/`, or `data/raw/` file was moved.


## Supabase/SQLite CRUD file map

- **Runtime read selection and store facade:** `src/database/backend.py`, `runtime_store.py`, `supabase_adapter.py`, `supabase_filters.py`, `sqlite_adapter.py`.
- **Clinic/import/provenance writes:** `src/database/write_backend.py`, `supabase_write_adapter.py`, `sqlite_write_adapter.py`, `contracts.py`, `write_contracts.py`.
- **HP research state and writes:** `src/database/hp_write_backend.py`, `hp_supabase_write_adapter.py`, `hp_sqlite_write_adapter.py`, `hp_auto_run_repository.py`, `hp_human_review_repository.py`.
- **Treatment research writes:** `src/database/treatment_write_backend.py`, `treatment_supabase_write_adapter.py`, `treatment_sqlite_write_adapter.py`, `treatment_write_contracts.py`.
- **Email/review/readers:** `email_database.py`, `email_target_reader.py`, `email_research_repository.py`, `email_human_review_repository.py`, `email_cohort.py`.
- **Comdesk/outreach persistence:** `comdesk_snapshot_repository.py`, `outreach_delivery_repository.py`; Cloud Run request writers remain in `src/cloud_run/` as platform adapters.
- SQLite remains in `sqlite_adapter.py` and the corresponding SQLite write adapters. No SQL schema, table, CRUD operation, transaction, or permission behavior was changed by this reorganization.

## Dependency boundary check

`src/database/` has no UI imports. Current code has zero module-level Python import cycles in the static AST check. Some existing function-local imports connect shared service and persistence contracts; those were preserved to avoid changing database behavior without the deleted regression suite.
