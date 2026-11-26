"""An expected marker must never hide a second, foreign marker."""
import pytest

from spindlebot.core.errors import MarkerMismatch
from spindlebot.services import volumes
from tests.test_volumes import _loc


def test_resolve_root_refuses_foreign_marker_even_when_ours_sorts_first(tmp_path):
    volumes.write_marker(tmp_path, uuid="aaa-ours", name="RetentionDrive", now=1)
    volumes.write_marker(tmp_path, uuid="zzz-foreign", name="OtherLocation", now=1)
    assert volumes.resolve_root(_loc(uuid="aaa-ours", root_path=tmp_path)) is None


def test_ensure_marker_refuses_to_refresh_when_any_foreign_marker_exists(tmp_path):
    ours = volumes.write_marker(tmp_path, uuid="aaa-ours", name="RetentionDrive", now=1)
    foreign = volumes.write_marker(tmp_path, uuid="zzz-foreign", name="OtherLocation", now=1)
    before = {path: path.read_bytes() for path in (ours, foreign)}
    with pytest.raises(MarkerMismatch):
        volumes.ensure_marker(tmp_path, uuid="aaa-ours", name="RetentionDrive", now=2)
    assert {path: path.read_bytes() for path in before} == before
