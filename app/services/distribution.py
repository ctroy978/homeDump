"""Register GitHub worksheet distributions and append audit ledger rows."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.services.assignments import (
    add_periods_to_assignment,
    create_github_assignment,
    find_github_assignment,
    get_assignment_pdf_path,
    validate_periods,
    write_assignment_pdf,
)
from app.services.github_worksheets import (
    GitHubWorksheetError,
    assert_repo_allowed,
    display_title_from_path,
    fetch_pdf_bytes,
    list_filtered_repos,
    periods_to_json,
    validate_worksheet_locator,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DistributionResult:
    """Outcome of a teacher scan registration attempt."""

    assignment_id: int | None
    assigned_date: str
    display_title: str
    periods_added: list[int]
    periods_skipped: list[int]
    outcome: str
    message: str | None = None


def _compute_outcome(periods_added: list[int], periods_skipped: list[int]) -> str:
    if periods_added and not periods_skipped:
        return "success"
    if periods_added and periods_skipped:
        return "partial"
    if not periods_added and periods_skipped:
        return "all_duplicate"
    raise ValueError("Distribution outcome requires at least one requested period.")


def _insert_distribution_event(
    conn: sqlite3.Connection,
    *,
    assigned_date: str,
    github_repo: str,
    github_path: str,
    display_title: str,
    periods_requested: list[int],
    periods_added: list[int],
    periods_skipped: list[int],
    assignment_id: int | None,
    outcome: str,
    error_message: str | None,
    client_ip: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO distribution_events (
            assigned_date,
            github_repo,
            github_path,
            display_title,
            periods_requested,
            periods_added,
            periods_skipped,
            assignment_id,
            outcome,
            error_message,
            client_ip
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assigned_date,
            github_repo,
            github_path,
            display_title,
            periods_to_json(periods_requested),
            periods_to_json(periods_added),
            periods_to_json(periods_skipped),
            assignment_id,
            outcome,
            error_message,
            client_ip,
        ),
    )


def _commit_failure_ledger(
    conn: sqlite3.Connection,
    *,
    assigned_date: str,
    github_repo: str,
    github_path: str,
    display_title: str,
    periods_requested: list[int],
    error_message: str,
    client_ip: str | None,
) -> DistributionResult:
    try:
        _insert_distribution_event(
            conn,
            assigned_date=assigned_date,
            github_repo=github_repo,
            github_path=github_path,
            display_title=display_title,
            periods_requested=periods_requested,
            periods_added=[],
            periods_skipped=[],
            assignment_id=None,
            outcome="failure",
            error_message=error_message,
            client_ip=client_ip,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return DistributionResult(
        assignment_id=None,
        assigned_date=assigned_date,
        display_title=display_title,
        periods_added=[],
        periods_skipped=[],
        outcome="failure",
        message=error_message,
    )


def register_distribution(
    conn: sqlite3.Connection,
    *,
    github_repo: str,
    github_path: str,
    periods: list[int],
    client_ip: str | None = None,
    assigned_date: str | None = None,
) -> DistributionResult:
    """
    Register a worksheet distribution for the scan day.

    Commits assignment and ledger rows atomically, then writes original.pdf.
    """
    resolved_date = assigned_date or date.today().isoformat()
    display_title = display_title_from_path(github_path)
    periods_requested = list(periods)

    try:
        period_list = validate_periods(periods)
        validate_worksheet_locator(github_repo, github_path)

        allowed = list_filtered_repos()
        if not allowed:
            raise GitHubWorksheetError(
                "GitHub not configured or no worksheet repos available."
            )
        assert_repo_allowed(github_repo, allowed)

        existing_id = find_github_assignment(
            conn,
            github_repo,
            github_path,
            resolved_date,
        )

        pdf_to_write: bytes | None = None
        assignment_id: int
        periods_added: list[int]
        periods_skipped: list[int]

        if existing_id is None:
            pdf_to_write = fetch_pdf_bytes(github_repo, github_path)
            assignment_id = create_github_assignment(
                conn,
                periods=period_list,
                assigned_date=resolved_date,
                title=display_title,
                github_repo=github_repo,
                github_path=github_path,
                pdf_filename=Path(github_path).name,
            )
            periods_added = period_list
            periods_skipped = []
        else:
            assignment_id = existing_id
            periods_added, periods_skipped = add_periods_to_assignment(
                conn,
                assignment_id,
                period_list,
            )
            if not get_assignment_pdf_path(assignment_id).exists():
                pdf_to_write = fetch_pdf_bytes(github_repo, github_path)

        outcome = _compute_outcome(periods_added, periods_skipped)
        _insert_distribution_event(
            conn,
            assigned_date=resolved_date,
            github_repo=github_repo,
            github_path=github_path,
            display_title=display_title,
            periods_requested=period_list,
            periods_added=periods_added,
            periods_skipped=periods_skipped,
            assignment_id=assignment_id,
            outcome=outcome,
            error_message=None,
            client_ip=client_ip,
        )
        conn.commit()
    except (GitHubWorksheetError, ValueError) as exc:
        return _commit_failure_ledger(
            conn,
            assigned_date=resolved_date,
            github_repo=github_repo,
            github_path=github_path,
            display_title=display_title,
            periods_requested=periods_requested,
            error_message=str(exc),
            client_ip=client_ip,
        )
    except Exception:
        conn.rollback()
        raise

    pdf_write_warning: str | None = None
    if pdf_to_write is not None:
        try:
            write_assignment_pdf(assignment_id, pdf_to_write)
        except OSError:
            logger.exception(
                "Failed to write assignment PDF after commit (assignment_id=%s)",
                assignment_id,
            )
            pdf_write_warning = (
                "Assignment registered, but the worksheet PDF could not be saved. "
                "Scan this worksheet again to repair the file."
            )

    return DistributionResult(
        assignment_id=assignment_id,
        assigned_date=resolved_date,
        display_title=display_title,
        periods_added=periods_added,
        periods_skipped=periods_skipped,
        outcome=outcome,
        message=pdf_write_warning,
    )