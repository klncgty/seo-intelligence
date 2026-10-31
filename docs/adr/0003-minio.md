# ADR 0003 — MinIO (S3) object storage
Status: accepted. Raw provider responses (lineage/reprocessing), WARC, HTML snapshots, reports/exports.
Content-addressed keys `<kind>/<sha[:2]>/<sha>.<ext>` built only from a fixed kind/extension allowlist
(`app/infra/storage.py`) → no caller-controlled paths, path traversal impossible; identical bytes dedupe.
Lifecycle/retention rules configured per kind (Phase 12 runbook).
