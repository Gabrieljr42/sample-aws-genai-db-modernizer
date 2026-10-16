#!/usr/bin/env python3
"""Render the customer deliverables for a completed local job.

Writes, next to the job's synthesis ``report.json``: the decision report (HTML),
the engineering report (Markdown), the interactive analysis report (HTML), and the
executive summary (PDF). Deterministic, no LLM calls.

When the rendered version is the job's latest, it also fills the customer folder
``artifacts/<db>/<job>/deliverables/`` with stable names and an ``index.md``
(``src.report.deliverables.FOLDER_ENTRIES``). Model I/O, traces and the versioned
folders stay where they are.

Usage:
    uv run python scripts/run_report.py --job-id <id> --db <name>
    uv run python scripts/run_report.py --job-id <id> --db <name> --assignment-version 2
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_deliverables_folder(job_dir, db: str, job_id: str, version: int, rendered):
    """Copy the customer files into ``<job_dir>/deliverables/`` under their stable
    names and write the index. A documented file this round did not render is
    removed, so the folder never mixes rounds; any other file is left alone."""
    from src.report.deliverables import (
        DELIVERABLES_DIR,
        FOLDER_ENTRIES,
        INDEX_FILENAME,
        render_folder_index,
    )

    folder = job_dir / DELIVERABLES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    by_name = {d.name: d for d in rendered.items}
    written = []
    for entry in FOLDER_ENTRIES:
        target = folder / entry.filename
        item = by_name.get(entry.name)
        if item is None:
            target.unlink(missing_ok=True)
            continue
        target.write_bytes(item.content)
        written.append(entry)
    (folder / INDEX_FILENAME).write_text(
        render_folder_index(db, job_id, version, written), encoding="utf-8"
    )
    return folder


def run(job_id: str, db: str, artifact_root: str, assignment_version: int | None) -> dict:
    from pathlib import Path

    from src.report import analysis_report
    from src.report.deliverables import FOLDER_FILENAMES
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=artifact_root)
    try:
        report_key = analysis_report.synthesis_report_key(
            store, db, job_id, assignment_version or 0
        )
    except FileNotFoundError:
        return {
            "status": "error",
            "message": f"No synthesis report for {db}/{job_id}; run /synthesize first.",
        }
    m = re.search(r"/synthesis/v(\d+)/", report_key)
    version = int(m.group(1)) if m else 0

    try:
        from src.report.deliverables import render_deliverables

        # No graph_fetcher: the default always rebuilds the context graph from the
        # JSON contracts, which is right for a local store.
        rendered = render_deliverables(store, job_id, db, report_key, assignment_version=version)
        base = report_key.rsplit("/", 1)[0]
        files = []
        for d in rendered.items:
            if d.stage:
                key = f"{base}/{d.filename}"
                store.write_bytes(key, d.content)
                files.append(key)
        status = "partial" if (rendered.errors or rendered.warnings) else "complete"
        result = {
            "status": status,
            "report": report_key,
            "files": files,
            "paths": [str(Path(artifact_root, k).resolve()) for k in files],
            "errors": rendered.errors,
            "warnings": rendered.warnings,
            "journeys": rendered.journeys,
        }
        latest_key = analysis_report.synthesis_report_key(store, db, job_id, 0)
        if latest_key == report_key:
            folder = _write_deliverables_folder(
                Path(artifact_root, db, job_id), db, job_id, version, rendered
            )
            result["deliverables_dir"] = str(folder.resolve())
            result["deliverables"] = sorted(
                str(p.resolve()) for p in folder.iterdir() if p.name in FOLDER_FILENAMES
            )
        else:
            m_latest = re.search(r"/synthesis/v(\d+)/", latest_key)
            result["deliverables_dir"] = None
            result["deliverables_note"] = (
                f"deliverables/ left unchanged: it holds the latest version "
                f"(v{m_latest.group(1) if m_latest else '?'}), not v{version}"
            )
        return result
    except Exception as e:  # noqa: BLE001 - this script must always print JSON
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Render deliverables for a local job.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Synthesis version to render (default: the highest present)",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    args = parser.parse_args()

    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        print(json.dumps({"status": "error", "message": violation}))
        sys.exit(1)

    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    try:
        args.db = validate_database_name(args.db)
        args.job_id = validate_job_id(args.job_id)
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    result = run(args.job_id, args.db, args.artifact_root, args.assignment_version)
    print(json.dumps(result))
    sys.exit(0 if result["status"] != "error" else 1)


if __name__ == "__main__":
    main()
