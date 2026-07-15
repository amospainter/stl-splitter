import trimesh
from fastapi.testclient import TestClient

from stlsplit.web import app

client = TestClient(app)


def _box_bytes(extents=(20, 20, 60)):
    mesh = trimesh.creation.box(extents=list(extents))
    return trimesh.exchange.stl.export_stl(mesh)


def _create_session(extents=(20, 20, 60)):
    resp = client.post(
        "/sessions",
        files={"file": ("box.stl", _box_bytes(extents), "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_session_returns_root_piece():
    data = _create_session()
    assert data["piece_id"] == "root"
    assert "session_id" in data
    assert len(data["extents"]) == 3


def test_piece_preview_returns_stl_bytes():
    data = _create_session()
    resp = client.get(f"/sessions/{data['session_id']}/pieces/root/preview")
    assert resp.status_code == 200
    assert "data_base64" in resp.json()


def test_cut_piece_then_tree_reflects_children():
    data = _create_session()
    sid = data["session_id"]

    resp = client.post(f"/sessions/{sid}/pieces/root/cut", data={"axis": "z", "cut_planes": "0.0"})
    assert resp.status_code == 200, resp.text
    children = resp.json()["children"]
    assert len(children) == 2

    tree = client.get(f"/sessions/{sid}").json()
    assert sorted(tree["leaves"]) == sorted(c["piece_id"] for c in children)
    assert tree["pieces"]["root"]["is_leaf"] is False


def test_cutting_a_child_on_a_different_axis_then_finish_end_to_end():
    # This is the exact user story: cut on Z, pick one resulting piece, cut
    # IT on a different axis, then finish.
    data = _create_session()
    sid = data["session_id"]

    resp = client.post(f"/sessions/{sid}/pieces/root/cut", data={"axis": "z", "cut_planes": "0.0"})
    children = resp.json()["children"]
    first_child_id = children[0]["piece_id"]

    resp2 = client.post(f"/sessions/{sid}/pieces/{first_child_id}/cut", data={"axis": "x", "cut_planes": "0.0"})
    assert resp2.status_code == 200, resp2.text
    grandchildren = resp2.json()["children"]
    assert len(grandchildren) == 2

    tree = client.get(f"/sessions/{sid}").json()
    assert len(tree["leaves"]) == 3  # 1 untouched sibling + 2 grandchildren

    finish_resp = client.post(f"/sessions/{sid}/finish", data={"no_connectors": "true"})
    assert finish_resp.status_code == 200, finish_resp.text
    job_id = finish_resp.json()["job_id"]

    # Poll the job via the module's own in-memory store directly rather than
    # parsing the SSE stream -- this is a same-process TestClient test, so
    # there's no real concurrency to synchronize with the browser over.
    import time

    from stlsplit.web import _JOBS
    job = _JOBS[job_id]
    for _ in range(200):
        if job.status != "running":
            break
        time.sleep(0.05)
    assert job.status == "done", (job.status, job.error)
    assert job.result["piece_count"] == 3


def test_undo_restores_leaf():
    data = _create_session()
    sid = data["session_id"]
    client.post(f"/sessions/{sid}/pieces/root/cut", data={"axis": "z", "cut_planes": "0.0"})

    resp = client.post(f"/sessions/{sid}/pieces/root/undo")
    assert resp.status_code == 200, resp.text

    tree = client.get(f"/sessions/{sid}").json()
    assert tree["leaves"] == ["root"]


def test_cutting_a_non_leaf_piece_returns_400():
    data = _create_session()
    sid = data["session_id"]
    client.post(f"/sessions/{sid}/pieces/root/cut", data={"axis": "z", "cut_planes": "0.0"})

    # root is no longer a leaf -- cutting it again must fail cleanly, not 500
    resp = client.post(f"/sessions/{sid}/pieces/root/cut", data={"axis": "x", "cut_planes": "0.0"})
    assert resp.status_code == 400


def test_unknown_session_returns_404():
    resp = client.get("/sessions/does-not-exist")
    assert resp.status_code == 404
    resp = client.post("/sessions/does-not-exist/pieces/root/cut", data={"axis": "z", "cut_planes": "0.0"})
    assert resp.status_code == 404


def test_delete_session_then_unknown():
    data = _create_session()
    sid = data["session_id"]
    resp = client.delete(f"/sessions/{sid}")
    assert resp.status_code == 200
    resp = client.get(f"/sessions/{sid}")
    assert resp.status_code == 404


def test_create_session_with_hollow_wall_reduces_volume():
    resp = client.post(
        "/sessions",
        data={"hollow_wall": "1.0"},
        files={"file": ("sphere.stl", trimesh.exchange.stl.export_stl(trimesh.creation.icosphere(subdivisions=2, radius=20)), "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    original_volume = trimesh.creation.icosphere(subdivisions=2, radius=20).volume
    assert data["volume"] < original_volume * 0.7


def test_rotate_piece_endpoint_swaps_extents():
    data = _create_session(extents=(20, 20, 60))
    sid = data["session_id"]
    resp = client.post(f"/sessions/{sid}/pieces/root/rotate", data={"axis": "x", "degrees": "90"})
    assert resp.status_code == 200, resp.text
    rotated = resp.json()
    assert abs(rotated["extents"][1] - 60) < 1e-6
    assert abs(rotated["extents"][2] - 20) < 1e-6

    tree = client.get(f"/sessions/{sid}").json()
    assert tree["leaves"] == ["root"]  # still a leaf, no tree change


def test_rotate_non_leaf_piece_returns_400():
    data = _create_session()
    sid = data["session_id"]
    client.post(f"/sessions/{sid}/pieces/root/cut", data={"axis": "z", "cut_planes": "0.0"})
    resp = client.post(f"/sessions/{sid}/pieces/root/rotate", data={"axis": "x", "degrees": "90"})
    assert resp.status_code == 400
