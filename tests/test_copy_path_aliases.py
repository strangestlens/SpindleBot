"""Queue equality follows the mounted destination's proven path semantics."""
from pathlib import Path

import pytest

from spindlebot.core.enums import RunKind
from spindlebot.db.repositories import action_repo, run_repo
from spindlebot.services.inventory import inventory_location
from spindlebot.services.sync import execute_pending
from tests.test_copy_retry_dedup import (
    _copy, _fail_copy, _make_restoration, _pending, _push, library as _library,
)
from tests.test_push import REL


@pytest.fixture
def library(tmp_path):
    yield from _library.__wrapped__(tmp_path)


def _require_case(tmp_path, sensitive):
    probe = tmp_path / 'CaseProbe'
    probe.write_bytes(b'probe')
    actual = not (tmp_path / 'caseprobe').exists()
    if actual != sensitive:
        pytest.skip(f'requires a case-{"sensitive" if sensitive else "insensitive"} filesystem')


def _change_case(root):
    (root / 'Artist').rename(root / 'artist')
    (root / 'artist/Album').rename(root / 'artist/album')
    for path in (root / 'artist/album').iterdir():
        path.rename(path.with_name(path.name.upper()))


@pytest.mark.parametrize('sensitive', [False, True])
def test_destination_aliases_are_equivalent_only_on_insensitive_filesystems(library, tmp_path, sensitive):
    _require_case(tmp_path, sensitive)
    conn, _, target = library
    preview = _push(conn, target, dry_run=True, now=200)
    generic = run_repo.start_run(conn, RunKind.RECONCILE, now=250)
    for a in action_repo.list_for_run(conn, preview.run_id):
        action_repo.add(
            conn, run_id=generic, action_kind=a.action_kind, content_kind=a.content_kind,
            content_id=a.content_id, source_location_id=a.source_location_id,
            dest_location_id=a.dest_location_id, rel_path=a.rel_path.swapcase(),
            source_rel_path=a.source_rel_path, expected_sha256=a.expected_sha256, now=250,
        )
    action_repo.acknowledge_run(conn, generic, now=250)
    old = _pending(conn, target)
    retry = _push(conn, target, copy_fn=_copy, now=300)
    assert retry.reconcile.copies == (4 if sensitive else 0)
    assert retry.sync.copied == (8 if sensitive else 4)
    assert all((Path(target.root_path) / a.rel_path).is_file() for a in old)
    assert (Path(target.root_path) / REL).is_file()


@pytest.mark.parametrize('sensitive', [False, True])
def test_stricter_approval_guards_aliases_without_retiring_distinct_paths(library, tmp_path, sensitive):
    _require_case(tmp_path, sensitive)
    conn, source, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    old = _pending(conn, target)
    for a in old:
        _copy(Path(source.root_path) / a.rel_path, Path(target.root_path) / a.rel_path)
    _change_case(Path(target.root_path))
    inventory_location(conn, location=target, root=target.root_path, now=250)
    for path in (Path(target.root_path) / 'artist/album').iterdir():
        path.unlink()
    preview = _push(conn, target, dry_run=True, now=300)
    action_repo.acknowledge_run(conn, preview.run_id, now=350)
    conn.commit()
    protected = Path(target.root_path) / 'artist/album/01.FLAC'
    protected.write_bytes(b'new replacement')
    result = execute_pending(conn, copy_fn=_copy, now=400, checkpoint=conn.commit)
    assert protected.read_bytes() == b'new replacement'
    assert result.failed == 1 and result.copied == (7 if sensitive else 3)
    for a in old:
        assert (action_repo.get(conn, a.id).superseded_utc is None) == sensitive


@pytest.mark.parametrize('sensitive', [False, True])
def test_source_case_aliases_reuse_only_a_still_equivalent_plan(library, tmp_path, sensitive):
    _require_case(tmp_path, sensitive)
    conn, source, target = library
    _make_restoration(conn, target)
    _push(conn, target, copy_fn=_fail_copy, now=300)
    _change_case(Path(source.root_path))
    retry = _push(conn, target, copy_fn=_copy, now=400)
    assert retry.reconcile.copies == (4 if sensitive else 0)
    assert retry.sync.copied == 4
    assert retry.sync.skipped == (4 if sensitive else 0)


def test_unknown_destination_policy_refuses_a_potentially_aliased_weaker_copy(library, monkeypatch):
    from spindlebot.db.repositories import location_repo
    from spindlebot.services import location_paths
    conn, source, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    old = _pending(conn, target)
    for a in old:
        _copy(Path(source.root_path) / a.rel_path, Path(target.root_path) / a.rel_path)
    _change_case(Path(target.root_path))
    inventory_location(conn, location=target, root=target.root_path, now=250)
    for path in (Path(target.root_path) / 'artist/album').iterdir():
        path.unlink()
    preview = _push(conn, target, dry_run=True, now=300)
    location_repo.set_case_sensitive(conn, target.id, None)
    action_repo.acknowledge_run(conn, preview.run_id, now=350)
    conn.commit()
    protected = Path(target.root_path) / 'artist/album/01.FLAC'
    protected.write_bytes(b'replacement must survive')
    monkeypatch.setattr(location_paths, 'probe_case_sensitive', lambda root: None)
    result = execute_pending(conn, copy_fn=_copy, now=400, checkpoint=conn.commit)
    assert protected.read_bytes() == b'replacement must survive'
    assert result.failed == 5 and result.copied == 3
    assert sum('path semantics unknown' in e for e in result.errors) == 4
    assert all(action_repo.get(conn, a.id).superseded_utc is None for a in old)


def test_unknown_empty_unmarked_root_keeps_ordinary_first_fill_working(library):
    conn, _, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    for path in Path(target.root_path).iterdir():
        path.unlink()  # only the location marker exists before this first fill
    result = execute_pending(conn, copy_fn=_copy, now=300, checkpoint=conn.commit)
    assert result.copied == 4 and result.failed == 0
    assert (Path(target.root_path) / REL).is_file()
