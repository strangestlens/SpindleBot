"""Read-only filesystem probes and derived path keys preserve approved plans."""
from pathlib import Path

import pytest

from spindlebot.core.enums import ActionKind, ContentKind, RunKind
from spindlebot.core.paths import path_key
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import action_repo, location_repo, run_repo
from spindlebot.services import location_paths


@pytest.mark.parametrize('sensitive', [False, True, None])
def test_path_keys_fold_only_a_proven_insensitive_location(sensitive):
    original = 'Artist/Beyonc\u00e9/Track.FLAC'
    alias = 'artist/BEYONCE\u0301/track.flac'
    assert (path_key(original, case_sensitive=sensitive)
            == path_key(alias, case_sensitive=sensitive)) == (sensitive is False)
    assert path_key(None, case_sensitive=sensitive) is None
    if sensitive is not False:
        assert path_key(original, case_sensitive=sensitive) == original


def test_probe_uses_existing_entries_without_writing(tmp_path):
    marker = tmp_path / '.spindlebot-location-test'
    marker.write_bytes(b'marker contents')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    # Independent filesystem check; the implementation must agree with reality.
    actual = not (tmp_path / marker.name.upper()).exists()
    assert location_paths.probe_case_sensitive(tmp_path) is actual
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_empty_unmarked_root_is_unknown_instead_of_probing_the_parent(tmp_path):
    assert location_paths.probe_case_sensitive(tmp_path) is None


def test_unreadable_root_is_unknown(tmp_path, monkeypatch):
    def denied(path):
        raise PermissionError('cannot enumerate this root')
    monkeypatch.setattr(Path, 'iterdir', denied)
    assert location_paths.probe_case_sensitive(tmp_path) is None


def test_distinct_case_entries_are_sensitive_even_if_they_are_hard_links(tmp_path):
    original = tmp_path / 'Marker'
    original.write_bytes(b'marker')
    if (tmp_path / 'mARKER').exists():
        pytest.skip('requires a case-sensitive filesystem')
    (tmp_path / 'mARKER').hardlink_to(original)
    assert location_paths.probe_case_sensitive(tmp_path) is True


def test_policy_and_root_changes_invalidate_only_derived_keys(tmp_path):
    conn = open_db(tmp_path / 'policy.db')
    try:
        loc = location_repo.upsert(conn, uuid='target', name='RetentionDrive', kind='local_drive',
                                   root_path=str(tmp_path / 'FirstRoot'))
        run = run_repo.start_run(conn, RunKind.RECONCILE, now=100)
        action = action_repo.add(conn, run_id=run, action_kind=ActionKind.COPY,
                                 content_kind=ContentKind.AUDIO, content_id=1,
                                 dest_location_id=loc.id, rel_path='Artist/Track.FLAC', now=100)
        action_repo.acknowledge(conn, [action.id], now=150)
        approved = action_repo.get(conn, action.id)

        def key():
            return conn.execute('SELECT dest_path_key FROM pending_action WHERE id=?',
                                (action.id,)).fetchone()[0]

        location_repo.set_case_sensitive(conn, loc.id, False)
        assert key() is None
        action_repo.prepare_destination_keys(conn, dest_location_id=loc.id)
        assert key() == 'artist/track.flac'
        location_repo.set_case_sensitive(conn, loc.id, True)
        assert key() is None
        action_repo.prepare_destination_keys(conn, dest_location_id=loc.id)
        assert key() == approved.rel_path
        location_repo.upsert(conn, uuid=loc.uuid, name=loc.name, kind=loc.kind,
                             root_path=str(tmp_path / 'DifferentRoot'))
        assert key() is None and location_repo.get_by_id(conn, loc.id).path_case_sensitive is None
        action_repo.prepare_destination_keys(conn, dest_location_id=loc.id)
        assert key() == approved.rel_path and action_repo.get(conn, action.id) == approved
    finally:
        conn.close()
