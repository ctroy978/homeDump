"""Queries for teacher-facing claim log review."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

ClaimLogStatus = Literal["all", "success", "failed"]


@dataclass(frozen=True)
class ClaimLogEntry:
    """One row from the claim audit log."""

    id: int
    student_name: str
    assignment_id: int | None
    assignment_title: str | None
    period: int | None
    absence_date: str | None
    token: str | None
    success: bool
    message: str
    created_at: str


def list_claim_logs(
    conn: sqlite3.Connection,
    *,
    student_query: str | None = None,
    status: ClaimLogStatus = "all",
    limit: int = 200,
) -> list[ClaimLogEntry]:
    """
    Return recent claim attempts, newest first.

    Optionally filter by student name substring and success/failure.
    """
    clauses: list[str] = []
    params: list[object] = []

    if student_query:
        clauses.append("cl.student_name LIKE ?")
        params.append(f"%{student_query.strip()}%")

    if status == "success":
        clauses.append("cl.success = 1")
    elif status == "failed":
        clauses.append("cl.success = 0")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT
            cl.id,
            cl.student_name,
            cl.assignment_id,
            a.title AS assignment_title,
            cl.period,
            cl.absence_date,
            cl.token,
            cl.success,
            cl.message,
            cl.created_at
        FROM claim_logs cl
        LEFT JOIN assignments a ON a.id = cl.assignment_id
        {where_sql}
        ORDER BY cl.created_at DESC, cl.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return [
        ClaimLogEntry(
            id=int(row["id"]),
            student_name=str(row["student_name"]),
            assignment_id=row["assignment_id"],
            assignment_title=row["assignment_title"],
            period=row["period"],
            absence_date=row["absence_date"],
            token=row["token"],
            success=bool(row["success"]),
            message=str(row["message"] or ""),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]