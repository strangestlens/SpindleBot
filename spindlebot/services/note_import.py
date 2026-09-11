"""Import a markdown document of listening notes.

Split into PLAN and APPLY so `--dry-run` and the real run walk identical code:
the table a dry run prints is the plan that would be executed, not a separate
approximation of it.

Two rules, both aimed at the same failure — losing writing without anyone
noticing:

**Atomic by default.** If any note fails to resolve, nothing is written. An
import that silently files 17 of 20 notes and drops 3 is the worst outcome
available; the caller must either fix the document, pass `--new`, or opt in
explicitly to skipping.

**Idempotent.** A note whose subject already carries that exact body is a
DUPLICATE, not a second note, so re-running an import is safe.
"""
from __future__ import annotations

from dataclasses import dataclass

from spindlebot.core.collection import LibraryAlbum
from spindlebot.core.enums import ImportRowStatus
from spindlebot.core.note_markdown import ParsedNote
from spindlebot.core.notes import NoteSubjectRef, body_sha256
from spindlebot.db.repositories import note_repo, note_subject_repo
from spindlebot.services.note_resolve import ResolutionStatus, resolve


@dataclass(frozen=True)
class ImportRow:
    parsed: ParsedNote
    status: ImportRowStatus
    subject: NoteSubjectRef | None = None
    detail: str = ""
    note_id: int | None = None

    @property
    def label(self) -> str:
        return self.subject.label if self.subject else self.parsed.label


@dataclass(frozen=True)
class ImportPlan:
    rows: tuple[ImportRow, ...] = ()

    def count(self, status: ImportRowStatus) -> int:
        return sum(1 for r in self.rows if r.status is status)

    @property
    def unresolved(self) -> tuple[ImportRow, ...]:
        return tuple(r for r in self.rows if r.status is ImportRowStatus.UNRESOLVED)

    @property
    def writable(self) -> tuple[ImportRow, ...]:
        return tuple(r for r in self.rows if r.status is ImportRowStatus.READY)


def plan_import(
    conn,
    library: list[LibraryAlbum],
    notes: list[ParsedNote],
    *,
    allow_new: bool = False,
) -> ImportPlan:
    """Resolve every parsed note and classify it. Writes nothing."""
    rows = []
    for parsed in notes:
        try:
            resolution = resolve(
                library, artist=parsed.artist, album=parsed.album,
                track=parsed.track, allow_new=allow_new,
            )
        except ValueError as e:
            rows.append(ImportRow(parsed, ImportRowStatus.UNRESOLVED, detail=str(e)))
            continue

        if resolution.status is not ResolutionStatus.RESOLVED:
            detail = resolution.reason
            if resolution.candidates:
                detail += f" (did you mean: {resolution.candidates[0].label}?)"
            rows.append(ImportRow(parsed, ImportRowStatus.UNRESOLVED, detail=detail))
            continue

        subject = resolution.subject
        existing = note_subject_repo.get(conn, subject.kind, subject.subject_key)
        duplicate = (
            note_repo.find_by_subject_and_sha(conn, existing.id, body_sha256(parsed.body))
            if existing else None
        )
        if duplicate is not None:
            rows.append(ImportRow(
                parsed, ImportRowStatus.DUPLICATE, subject=subject,
                detail="already imported", note_id=duplicate.id,
            ))
            continue

        rows.append(ImportRow(parsed, ImportRowStatus.READY, subject=subject))
    return ImportPlan(rows=tuple(rows))


def apply_import(
    conn, plan: ImportPlan, *, session_id: int | None = None, now: int | None = None
) -> ImportPlan:
    """Write every READY row. The caller commits."""
    from spindlebot.services.notes import add_note

    rows = []
    for row in plan.rows:
        if row.status is not ImportRowStatus.READY:
            rows.append(row)
            continue
        view = add_note(
            conn, subject=row.subject, body=row.parsed.body,
            now=now, session_id=session_id,
        )
        rows.append(ImportRow(
            row.parsed, ImportRowStatus.IMPORTED, subject=row.subject,
            detail=row.detail, note_id=view.id,
        ))
    return ImportPlan(rows=tuple(rows))
