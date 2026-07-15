import time

import pytest
import trimesh

from stlsplit.sessions import create_session, cut_piece, rotate_piece, sweep_expired_sessions, undo_cut


def _box():
    return trimesh.creation.box(extents=[20, 20, 60])


def test_create_session_has_single_leaf_root():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    assert session.leaves == {"root"}
    assert set(session.pieces.keys()) == {"root"}
    assert session.applied_cuts == []


def test_cut_piece_replaces_leaf_with_children():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    children = cut_piece(session, "root", "z", [0.0])
    assert len(children) == 2
    assert "root" not in session.leaves
    assert session.leaves == {c.id for c in children}
    assert len(session.applied_cuts) == 1
    root = session.pieces["root"]
    assert set(root.children_ids) == {c.id for c in children}
    # root itself is still in the tree, just no longer a leaf
    assert "root" in session.pieces


def test_cut_piece_on_non_leaf_raises_keyerror():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    cut_piece(session, "root", "z", [0.0])
    with pytest.raises(KeyError):
        cut_piece(session, "root", "x", [0.0])


def test_cutting_a_child_further_produces_correct_leaf_count():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    children = cut_piece(session, "root", "z", [0.0])
    first_child = children[0]
    grandchildren = cut_piece(session, first_child.id, "x", [0.0])

    assert len(grandchildren) == 2
    # 1 leaf remaining from the first cut + 2 new leaves from the second cut
    assert len(session.leaves) == 3
    assert first_child.id not in session.leaves
    assert children[1].id in session.leaves
    assert all(g.id in session.leaves for g in grandchildren)
    # total pieces ever created: root + 2 (first cut) + 2 (second cut) = 5
    assert len(session.pieces) == 5
    assert len(session.applied_cuts) == 2


def test_undo_cut_restores_leaf_and_removes_applied_cuts():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    children = cut_piece(session, "root", "z", [0.0])
    assert len(session.applied_cuts) == 1

    undo_cut(session, "root")
    assert session.leaves == {"root"}
    assert session.applied_cuts == []
    assert session.pieces["root"].children_ids == []
    for c in children:
        assert c.id not in session.pieces


def test_undo_cut_with_no_children_raises_keyerror():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    with pytest.raises(KeyError):
        undo_cut(session, "root")


def test_undo_cut_cascades_through_grandchildren():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    children = cut_piece(session, "root", "z", [0.0])
    first_child = children[0]
    cut_piece(session, first_child.id, "x", [0.0])
    assert len(session.applied_cuts) == 2

    # Undoing the ROOT's cut must cascade: first_child was cut further, so
    # undoing root's cut has to remove first_child's own cut (and its
    # children) before first_child itself can be removed.
    undo_cut(session, "root")
    assert session.leaves == {"root"}
    assert session.applied_cuts == []
    assert len(session.pieces) == 1


def test_sweep_expired_sessions_removes_idle_sessions():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    session.last_activity = time.time() - 3600
    removed = sweep_expired_sessions()
    assert removed >= 1

    from stlsplit.sessions import get_session
    assert get_session(session.id) is None


def test_tree_summary_reflects_current_state():
    from stlsplit.sessions import tree_summary

    session = create_session(_box(), "test.stl", scale_factor=1.0)
    children = cut_piece(session, "root", "z", [0.0])
    summary = tree_summary(session)

    assert summary["session_id"] == session.id
    assert sorted(summary["leaves"]) == sorted(c.id for c in children)
    assert summary["pieces"]["root"]["is_leaf"] is False
    assert set(summary["pieces"]["root"]["children_ids"]) == {c.id for c in children}
    for c in children:
        assert summary["pieces"][c.id]["is_leaf"] is True
        assert summary["pieces"][c.id]["parent_id"] == "root"


def test_rotate_piece_swaps_extents_for_a_90_degree_rotation():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    before = session.pieces["root"].mesh.extents.copy()

    node = rotate_piece(session, "root", "x", 90.0)

    after = node.mesh.extents
    # rotating 90 degrees about X swaps the Y and Z extents, leaves X alone
    assert abs(after[0] - before[0]) < 1e-6
    assert abs(after[1] - before[2]) < 1e-6
    assert abs(after[2] - before[1]) < 1e-6
    # rotation is in place -- same piece_id, still a leaf, no tree change
    assert session.leaves == {"root"}
    assert node.id == "root"


def test_rotate_piece_preserves_volume_and_watertightness():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    original_volume = session.pieces["root"].mesh.volume

    node = rotate_piece(session, "root", "y", 37.0)  # an arbitrary, non-90 angle

    assert abs(node.mesh.volume - original_volume) < 1e-6
    assert node.mesh.is_watertight


def test_rotate_piece_on_non_leaf_raises_keyerror():
    session = create_session(_box(), "test.stl", scale_factor=1.0)
    cut_piece(session, "root", "z", [0.0])
    with pytest.raises(KeyError):
        rotate_piece(session, "root", "x", 90.0)
