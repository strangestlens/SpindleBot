"""Tests for spindlebot.core.vclock (pure version-vector logic)."""
from __future__ import annotations

from spindlebot.core import vclock as vc


def test_bump_creates_and_increments_without_mutating():
    base = {"A": 1}
    once = vc.bump(base, "A")
    assert once == {"A": 2}
    assert base == {"A": 1}            # input untouched
    assert vc.bump(base, "B") == {"A": 1, "B": 1}
    assert vc.bump(base, "A", by=3) == {"A": 4}


def test_normalize_drops_zeroes():
    assert vc.normalize({"A": 0, "B": 2}) == {"B": 2}
    assert vc.normalize({}) == {}


def test_merge_is_componentwise_max_and_commutative():
    a, b = {"A": 2, "B": 1}, {"A": 1, "B": 3, "C": 1}
    assert vc.merge(a, b) == {"A": 2, "B": 3, "C": 1}
    assert vc.merge(a, b) == vc.merge(b, a)


def test_dominates_is_reflexive_and_follows_bump():
    v = {"A": 1, "B": 2}
    assert vc.dominates(v, v)                  # reflexive
    nv = vc.bump(v, "A")
    assert vc.dominates(nv, v)                 # a bump descends from its base
    assert not vc.dominates(v, nv)
    assert vc.strictly_dominates(nv, v)
    assert not vc.strictly_dominates(v, v)


def test_concurrent_divergent_edits():
    base = {"shared": 1}
    a = vc.bump(base, "A")     # {shared:1, A:1}
    b = vc.bump(base, "B")     # {shared:1, B:1}
    assert vc.concurrent(a, b)
    assert vc.concurrent(b, a)
    assert not vc.dominates(a, b) and not vc.dominates(b, a)


def test_merge_of_concurrent_dominates_both():
    a = {"shared": 1, "A": 1}
    b = {"shared": 1, "B": 1}
    m = vc.merge(a, b)
    assert vc.dominates(m, a) and vc.dominates(m, b)
    assert not vc.concurrent(m, a)


def test_equal_ignores_zero_entries():
    assert vc.equal({"A": 1, "B": 0}, {"A": 1})
    assert not vc.equal({"A": 1}, {"A": 2})


def test_json_roundtrip_is_deterministic_and_idempotent():
    v = {"B": 2, "A": 1, "Z": 0}
    s = vc.to_json(v)
    assert s == '{"A":1,"B":2}'                # sorted, zeros dropped
    assert vc.from_json(s) == {"A": 1, "B": 2}
    assert vc.to_json(vc.from_json(s)) == s    # idempotent
    assert vc.from_json(None) == {} and vc.from_json("") == {}
