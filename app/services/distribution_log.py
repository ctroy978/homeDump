"""Queries for teacher-facing distribution scan audit logs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

DistributionOutcome = Literal[
    "all", "success", "partial", "all_duplicate", "failure"
]


@dataclass(frozen=True)
class DistributionLogEntry:
    """One row from the distribution events ledger."""

    id: int
    scanned_at: str
    assigned_date: str
    github_repo: str
    github_path: str
    display_title: str
    periods_requested: list[int]
    periods_added: list[int]
    periods_skipped: list[int]
    assignment_id: int | None
    outcome: str
    error_message: str | None
    client_ip: str | None


def _parse_period_list(raw: str) -> list[int]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    return [int(value) for value in values]


def list_distribution_events(
    conn: sqlite3.Connection,
    *,
    repo_query: str | None = None,
    assigned_date: str | None = None,
    outcome: DistributionOutcome = "all",
    limit: int = 200,
) -> list[DistributionLogEntry]:
    """Return recent distribution scans, newest first."""
    clauses: list[str] = []
    params: list[object] = []

    if repo_query:
        clauses.append("de.github_repo LIKE ?")
        params.append(f"%{repo_query.strip()}%")

    if assigned_date:
        clauses.append("de.assigned_date = ?")
        params.append(assigned_date.strip())

    if outcome != "all":
        clauses.append("de.outcome = ?")
        params.append(outcome)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT
            de.id,
            de.scanned_at,
            de.assigned_date,
            de.github_repo,
            de.github_path,
            de.display_title,
            de.periods_requested,
            de.periods_added,
            de.periods_skipped,
            de.assignment_id,
            de.outcome,
            de.error_message,
            de.client_ip
        FROM distribution_events de
        {where_sql}
        ORDER BY de.scanned_at DESC, de.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return [
        DistributionLogEntry(
            id=int(row["id"]),
            scanned_at=str(row["scanned_at"]),
            assigned_date=str(row["assigned_date"]),
            github_repo=str(row["github_repo"]),
            github_path=str(row["github_path"]),
            display_title=str(row["display_title"]),
            periods_requested=_parse_period_list(str(row["periods_requested"])),
            periods_added=_parse_period_list(str(row["periods_added"])),
            periods_skipped=_parse_period_list(str(row["periods_skipped"])),
            assignment_id=row["assignment_id"],
            outcome=str(row["outcome"]),
            error_message=row["error_message"],
            client_ip=row["client_ip"],
        )
        for row in rows
    ]