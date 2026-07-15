"""In-memory session/piece-tree store for the interactive split UI.

A Session holds every piece ever produced by cutting, as a tree: the
originally-uploaded (scaled) mesh is the root; cutting a leaf piece replaces
it with its children in `leaves` but keeps the piece itself in `pieces` (as
an internal/non-leaf node) so the tree can be inspected or undone. Only
`leaves` are eligible to be cut further or included in the final
connector/export pass (see `connectors.add_connectors_after_cuts`, which
already knows how to place connectors across an arbitrary multi-level cut
tree given the full list of `ResolvedCut`s applied anywhere in it).
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, field

import trimesh

from .cutting import cut_mesh
from .geometry import Cut, ResolvedCut, axis_index, resolve_cuts
from .progress import ProgressReporter

_SESSION_TTL_SECONDS = 30 * 60  # idle sessions are swept after this long


@dataclass
class PieceNode:
    id: str
    mesh: trimesh.Trimesh
    parent_id: str | None
    # The cut-group that produced this piece as a child (None for the root).
    # Every child produced by the same cut_piece() call shares one
    # cut_group_id, so undo_cut can remove exactly (and only) that
    # operation's effects.
    cut_group_id: str | None
    children_ids: list[str] = field(default_factory=list)


@dataclass
class CutGroup:
    id: str
    piece_id: str  # the piece that was cut to produce this group's children
    child_ids: list[str]
    resolved_cuts: list[ResolvedCut]  # removed from Session.applied_cuts on undo


@dataclass
class Session:
    id: str
    filename: str
    scale_factor: float
    pieces: dict[str, PieceNode] = field(default_factory=dict)
    leaves: set[str] = field(default_factory=set)
    cut_groups: dict[str, CutGroup] = field(default_factory=dict)
    applied_cuts: list[ResolvedCut] = field(default_factory=list)
    last_activity: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_activity = time.time()


_SESSIONS: dict[str, Session] = {}
_SESSIONS_LOCK = threading.Lock()


def create_session(mesh: trimesh.Trimesh, filename: str, scale_factor: float) -> Session:
    session_id = uuid.uuid4().hex
    root = PieceNode(id="root", mesh=mesh, parent_id=None, cut_group_id=None)
    session = Session(id=session_id, filename=filename, scale_factor=scale_factor)
    session.pieces["root"] = root
    session.leaves.add("root")
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = session
    return session


def get_session(session_id: str) -> Session | None:
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
    if session is not None:
        session.touch()
    return session


def close_session(session_id: str) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)


def sweep_expired_sessions(now: float | None = None) -> int:
    """Remove sessions idle longer than _SESSION_TTL_SECONDS. Returns the
    count removed. Call this periodically (see web.py's background sweep) —
    full meshes live in server memory for the session's lifetime, so
    unbounded sessions is a real memory leak, not a theoretical one."""
    now = now if now is not None else time.time()
    with _SESSIONS_LOCK:
        expired = [sid for sid, s in _SESSIONS.items() if now - s.last_activity > _SESSION_TTL_SECONDS]
        for sid in expired:
            del _SESSIONS[sid]
    return len(expired)


def cut_piece(
    session: Session,
    piece_id: str,
    axis: str,
    cuts: list["Cut | float"],
    progress: ProgressReporter | None = None,
    allow_floating_regions: bool = False,
) -> list[PieceNode]:
    """Cut the leaf piece `piece_id` along `axis` at `cuts`, replacing it with
    its children in `session.leaves`. Raises KeyError if `piece_id` isn't a
    current leaf (stale client state — e.g. it was already cut or undone by
    another request), CutPlacementError if the cut itself fails (same as the
    existing single-shot pipeline — surface this to the caller unchanged)."""
    if piece_id not in session.leaves:
        raise KeyError(f"'{piece_id}' is not a current leaf piece")
    node = session.pieces[piece_id]
    resolved = resolve_cuts(node.mesh, axis, cuts)
    sub_pieces = cut_mesh(node.mesh, resolved, progress=progress, allow_floating_regions=allow_floating_regions)

    group_id = uuid.uuid4().hex
    child_nodes = []
    for sub in sub_pieces:
        child_id = uuid.uuid4().hex
        child = PieceNode(id=child_id, mesh=sub, parent_id=piece_id, cut_group_id=group_id)
        session.pieces[child_id] = child
        node.children_ids.append(child_id)
        child_nodes.append(child)

    session.cut_groups[group_id] = CutGroup(
        id=group_id, piece_id=piece_id, child_ids=[c.id for c in child_nodes], resolved_cuts=resolved
    )
    session.leaves.discard(piece_id)
    session.leaves.update(c.id for c in child_nodes)
    session.applied_cuts.extend(resolved)
    session.touch()
    return child_nodes


def rotate_piece(session: Session, piece_id: str, axis: str, degrees: float) -> PieceNode:
    """Rotate the leaf piece `piece_id` by `degrees` around `axis`, in
    place — mutates its mesh but not the tree structure (same piece_id, same
    node, just reoriented), so it doesn't touch `applied_cuts`/`cut_groups`
    at all. Only ever offered for pieces with no children yet: rotating a
    piece that's already been cut further would leave its (already-fixed)
    children's geometry out of sync with the parent's new orientation, and
    there's no sane way to "rotate the cut tree below it" after the fact.
    Pivots around the piece's own bounding-box center, so the piece stays
    roughly where it was in the viewer rather than swinging off to wherever
    the origin happens to be. Raises KeyError if `piece_id` isn't a current
    leaf."""
    if piece_id not in session.leaves:
        raise KeyError(f"'{piece_id}' is not a current leaf piece")
    node = session.pieces[piece_id]
    axis_vec = [0.0, 0.0, 0.0]
    axis_vec[axis_index(axis)] = 1.0
    center = (node.mesh.bounds[0] + node.mesh.bounds[1]) / 2.0
    transform = trimesh.transformations.rotation_matrix(math.radians(degrees), axis_vec, point=center)
    rotated = node.mesh.copy()
    rotated.apply_transform(transform)
    node.mesh = rotated
    session.touch()
    return node


def undo_cut(session: Session, piece_id: str) -> None:
    """Undo the cut that produced `piece_id`'s children: remove the children
    (and, recursively, anything cut from them — a piece can't be un-cut while
    its own children still exist, so this cascades down first), restore
    `piece_id` as a leaf, and remove that cut's ResolvedCuts from
    session.applied_cuts. Raises KeyError if `piece_id` has no children (never
    cut, or already undone)."""
    node = session.pieces[piece_id]
    if not node.children_ids:
        raise KeyError(f"'{piece_id}' has no cut to undo")

    # Cascade: recursively undo any grandchildren first (a child that was
    # itself cut further must be un-cut before it can be removed).
    for child_id in list(node.children_ids):
        child = session.pieces.get(child_id)
        if child and child.children_ids:
            undo_cut(session, child_id)

    group_id = session.pieces[node.children_ids[0]].cut_group_id
    group = session.cut_groups.pop(group_id)
    for child_id in group.child_ids:
        session.pieces.pop(child_id, None)
        session.leaves.discard(child_id)
    node.children_ids.clear()
    session.leaves.add(piece_id)

    # Remove exactly this group's ResolvedCuts (by identity, not equality —
    # ResolvedCut isn't safe/meaningful to dedupe by value here) from
    # applied_cuts.
    removed_ids = {id(c) for c in group.resolved_cuts}
    session.applied_cuts = [c for c in session.applied_cuts if id(c) not in removed_ids]
    session.touch()


def tree_summary(session: Session) -> dict:
    """JSON-serializable snapshot of the whole tree, for GET /sessions/{id}
    (reconstructing UI state after a page refresh) — piece meshes themselves
    are NOT included here (too large); the piece-preview endpoint fetches an
    individual piece's STL bytes on demand."""
    return {
        "session_id": session.id,
        "leaves": sorted(session.leaves),
        "pieces": {
            pid: {
                "parent_id": node.parent_id,
                "children_ids": node.children_ids,
                "is_leaf": pid in session.leaves,
                "extents": node.mesh.extents.tolist(),
                "volume": float(node.mesh.volume),
                "bounds": node.mesh.bounds.tolist(),
            }
            for pid, node in session.pieces.items()
        },
    }
