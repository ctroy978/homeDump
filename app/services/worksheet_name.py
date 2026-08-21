"""Stamp a student name into the OCR worksheet header."""

from __future__ import annotations

import fitz

STUDENT_NAME_LABEL = "Student Name:"
NAME_SLOT_WIDTH_PT = 1.85 * 72.0
NAME_SLOT_GAP_PT = 4.0
NAME_FONTSIZE = 10.0
NAME_FONT = "helv"
MISSING_NAME_FIELD_MESSAGE = (
    "This PDF does not have the Student Name field the grader expects. "
    "Named copies only work with OCR worksheets that include that header."
)


class WorksheetNameError(Exception):
    """Raised when a worksheet cannot receive a student name in the header."""


def _fontsize_for_name(name: str, max_width: float) -> float:
    size = NAME_FONTSIZE
    while size > 7.0:
        width = fitz.get_text_length(name, fontname=NAME_FONT, fontsize=size)
        if width <= max_width:
            return size
        size -= 0.5
    return 7.0


def assert_student_name_fields(document: fitz.Document) -> None:
    """Require every page to have the OCR Student Name header."""
    for page in document:
        if not page.search_for(STUDENT_NAME_LABEL):
            raise WorksheetNameError(MISSING_NAME_FIELD_MESSAGE)


def stamp_student_name(page: fitz.Page, student_name: str) -> None:
    """Write the student name to the right of the Student Name: label."""
    hits = page.search_for(STUDENT_NAME_LABEL)
    if not hits:
        raise WorksheetNameError(MISSING_NAME_FIELD_MESSAGE)
    label = hits[0]
    # insert_textbox fails in the tight OCR header; draw on the label baseline.
    page.insert_text(
        (label.x1 + NAME_SLOT_GAP_PT, label.y1),
        student_name,
        fontsize=_fontsize_for_name(student_name, NAME_SLOT_WIDTH_PT),
        fontname=NAME_FONT,
    )

