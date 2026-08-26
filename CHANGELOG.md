# Changelog

## 4.0.0 — 2026-08-03

- Simplified project registration to the approved 19-column project contract.
- Changed Operations catalogs to `foundry_dev` and `foundry_prod`.
- Removed exact SHA from the user approval and production authorization contract.
- Added current-branch revision capture for audit only.
- Added in-place development tag validation.
- Added three mandatory tags: `project_tag`, `environment`, `data_classification`.
- Added request-time Git/Bundle preflight.
- Added protected Databricks development-validator and production-dispatcher jobs.
- Added immediate App-triggered job wake-up and optional scheduled recovery.
- Retained default Databricks Asset Bundle development prefix behavior.
- Added team profile and per-profile production actor isolation.
- Added sample project and major migration documentation.

## 4.0.0 deployment-ready packaging revision

- Added `requirements.txt` so Databricks Apps installs the pinned runtime dependencies.
- Made Databricks production startup validate-only after the explicit SQL migration, avoiding broad registry DDL grants for the App service principal.
- Added Bundle-managed `CAN_USE` permissions for the development validator and production deployer service principals.
