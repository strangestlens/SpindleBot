"""
Lyric causal lineage — infer, per doc, which lyric_version each location holds
and how each relates to the current head (current / behind / concurrent).

Pure DB reasoning: reads observed .lrc shas + prior version-presence, writes
lyric_version / head / lyric_version_presence rows, and returns a typed lineage.
No bytes, no files, no `print`. Propagation (making shas match) and the conflicts
CLI are Phase 4.1 / 4.2; this phase only decides causality.

Per location L's observed sha S for a track's .lrc:

  - S matches an existing version V:
      V is head  -> L is CURRENT.
      V != head  -> L is BEHIND: the head *dominates* V, so L holds an older
                    version — a future propagation target, never a conflict and
                    never a new head.
  - S is new (no version has it) -> L EDITED. Its `base` is the version L held on
    the PRIOR scan (lyric_version_presence, read before we update it this pass):
      base == head (or no head yet)
                 -> LINEAR EDIT: the new version is bump(head, L), which strictly
                    dominates the old head; it becomes the new head. No conflict.
      base older / None with a differing head
                 -> CONCURRENT: an independent edit off an older (or unknown)
                    base — bump(base, L) is concurrent with head. A real conflict.

Causality is decided only by core.vclock. mtime never decides who wins (it is a
last-resort tiebreaker only, and this phase never needs it).
"""
from __future__ import annotations

from dataclasses import dataclass

from spindlebot.core import vclock
from spindlebot.db.repositories import lyric_repo, lyric_version_presence_repo


@dataclass(frozen=True)
class LyricObservation:
    """A track's .lrc observed present at one location, with its per-copy sha."""
    location_id: int
    uuid: str            # STABLE vclock actor key — survives a location rename
    name: str            # human-readable, for messaging/logs only (never causality)
    sha: str
    observed_utc: int    # when THIS location's copy was last confirmed by a scan
                         # (its sidecar_presence.observed_utc) — not the reconcile
                         # run time; the target is fresh, other locations may be old


@dataclass(frozen=True)
class HeldVersion:
    """Which version a location resolved to this pass, classified against head."""
    location_id: int
    location_name: str
    version_id: int
    is_head: bool        # holds the current head — up to date
    is_behind: bool      # holds an older, head-dominated version — propagate to it
    is_concurrent: bool  # holds a version concurrent with head — a real conflict


@dataclass(frozen=True)
class DocLineage:
    doc_id: int
    head_version_id: int | None
    held: list[HeldVersion]

    @property
    def concurrent(self) -> list[HeldVersion]:
        return [h for h in self.held if h.is_concurrent]

    @property
    def behind(self) -> list[HeldVersion]:
        return [h for h in self.held if h.is_behind]


def reconcile_doc(
    conn,
    *,
    audio_id: int,
    observations: list[LyricObservation],
    now: int,
) -> DocLineage:
    """Fold a set of per-location observed .lrc shas into the doc's lineage.

    Idempotent: re-running with unchanged shas creates no new versions and flips
    no heads (every sha already matches a recorded version). Deterministic: the
    observations are processed in location_id order so head assignment for a fresh
    divergence is reproducible.

    `now` stamps versions minted this pass (when SpindleBot recorded them). Each
    location's version-presence, by contrast, is stamped with that observation's
    own `observed_utc` (when its .lrc was last confirmed by a scan) — consistent
    with the other presence tables and honest for cached, non-target locations.
    """
    doc = lyric_repo.ensure_doc(conn, audio_id, now)

    # Read PRIOR per-location versions before we touch presence this pass — a
    # location's "base" must be what it held on the previous scan.
    prior = {p.location_id: p for p in lyric_version_presence_repo.list_for_doc(conn, doc.id)}
    existing_versions = lyric_repo.list_versions(conn, doc.id)
    by_sha = {v.sha256: v for v in existing_versions}
    head = lyric_repo.head_version(conn, doc.id)

    # REPAIR legacy docs: the OLD reconciler minted versions but never set a head.
    # Left NULL, `head is None` would classify every held version as concurrent —
    # a flood of spurious conflicts on the first post-v7 reconcile. Adopt the
    # latest version (highest id) as head; a single-version doc then reads as
    # current, a genuinely divergent one still surfaces exactly one real conflict.
    if head is None and existing_versions:
        head = existing_versions[-1]
        lyric_repo.set_head(conn, doc.id, head.id, now)

    resolved: list[tuple[LyricObservation, int]] = []
    for obs in sorted(observations, key=lambda o: o.location_id):
        existing = by_sha.get(obs.sha)
        if existing is not None:
            version = existing
        else:
            base_row = prior.get(obs.location_id)
            base = (
                lyric_repo.get_version(conn, base_row.version_id)
                if base_row is not None else None
            )
            linear = head is None or (base is not None and base.id == head.id)
            if linear:
                new_vc = (
                    vclock.bump(vclock.from_json(head.vclock_json), obs.uuid)
                    if head is not None else {obs.uuid: 1}
                )
                version = lyric_repo.add_version(
                    conn, doc_id=doc.id, sha256=obs.sha,
                    vclock_json=vclock.to_json(new_vc), source="scan", now=now,
                )
                lyric_repo.set_head(conn, doc.id, version.id, now)
                head = version
            else:
                # Independent edit off L's own (older/None) base — concurrent with
                # a head that advanced elsewhere.
                base_vc = vclock.from_json(base.vclock_json) if base is not None else {}
                new_vc = vclock.bump(base_vc, obs.uuid)
                version = lyric_repo.add_version(
                    conn, doc_id=doc.id, sha256=obs.sha,
                    vclock_json=vclock.to_json(new_vc), source="scan", now=now,
                )
            by_sha[obs.sha] = version

        lyric_version_presence_repo.upsert(
            conn, doc_id=doc.id, location_id=obs.location_id,
            version_id=version.id, observed_utc=obs.observed_utc,
        )
        resolved.append((obs, version.id))

    # Classify every held version against the FINAL head (a later linear edit in
    # this pass may have advanced it past an earlier-classified location).
    head = lyric_repo.head_version(conn, doc.id)
    head_vc = vclock.from_json(head.vclock_json) if head is not None else {}
    held: list[HeldVersion] = []
    for obs, version_id in resolved:
        is_head = head is not None and version_id == head.id
        if is_head:
            is_behind = is_concurrent = False
        else:
            v = lyric_repo.get_version(conn, version_id)
            v_vc = vclock.from_json(v.vclock_json) if v is not None else {}
            # Decide by the true vclock relation (the lineage model's definition),
            # not `not is_behind`: a version that DOMINATES a stale head — an
            # inconsistent state the head invariants normally prevent — is neither
            # behind nor concurrent, and must not read as a spurious conflict.
            is_behind = head is not None and vclock.dominates(head_vc, v_vc)
            is_concurrent = head is not None and vclock.concurrent(head_vc, v_vc)
        held.append(HeldVersion(
            location_id=obs.location_id, location_name=obs.name,
            version_id=version_id, is_head=is_head,
            is_behind=is_behind, is_concurrent=is_concurrent,
        ))

    return DocLineage(
        doc_id=doc.id,
        head_version_id=head.id if head is not None else None,
        held=held,
    )
