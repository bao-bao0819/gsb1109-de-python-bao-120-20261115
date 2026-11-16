import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest

import meshio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import partition_check  # noqa: E402

TOL = {"coordinates": 1e-6, "field": 1e-6}


def _part0():
    return meshio.Mesh(
        points=[
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 2.0, 0.0],
            [1.0, 2.0, 0.0],
        ],
        cells=[
            ("quad", [[0, 1, 3, 2], [2, 3, 5, 4]]),
            ("line", [[1, 3], [3, 5]]),
        ],
        point_data={
            "global_node_id": [1, 2, 3, 4, 5, 6],
            "temperature": [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        },
        point_sets={"interface:mid": [1, 3, 5]},
        cell_sets={
            "interface:mid": [
                np.array([], dtype=int),
                np.array([0, 1]),
            ]
        },
    )


def _part1():
    return meshio.Mesh(
        points=[
            [1.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [2.0, 1.0, 0.0],
            [1.0, 2.0, 0.0],
            [2.0, 2.0, 0.0],
        ],
        cells=[
            ("quad", [[0, 1, 3, 2], [2, 3, 5, 4]]),
            ("line", [[2, 0], [4, 2]]),
        ],
        point_data={
            "global_node_id": [2, 7, 4, 8, 6, 9],
            "temperature": [1.0, 2.0, 1.0, 2.0, 1.0, 2.0],
        },
        point_sets={"interface:mid": [0, 2, 4]},
        cell_sets={
            "interface:mid": [
                np.array([], dtype=int),
                np.array([0, 1]),
            ]
        },
    )


def _manifest(part0, part1):
    return {
        "global_id_key": "global_node_id",
        "tolerances": dict(TOL),
        "fields": ["temperature"],
        "partitions": {"part0": part0, "part1": part1},
        "interfaces": [{"name": "mid", "sides": ["part0", "part1"]}],
    }


def _check(part0, part1):
    return partition_check.check_delivery(_manifest(part0, part1))


def test_clean_delivery_accepted():
    report = _check(_part0(), _part1())
    assert report["status"] == "ok"
    iface = report["interfaces"][0]
    assert iface["matched_nodes"] == 3
    assert iface["cracks"] == []
    assert iface["missing"] == []
    assert iface["orientation"] == []
    assert iface["field_discontinuities"] == []
    # 内部正常单元全部入仓
    assert report["acceptance"] == {
        "part0": {"accepted": 4, "quarantined": 0},
        "part1": {"accepted": 4, "quarantined": 0},
    }


def test_coordinate_gap_reported_as_crack():
    part1 = _part1()
    part1.points[0] = [1.0 + 1e-3, 0.0, 0.0]  # 全局点 2 偏移
    report = _check(_part0(), part1)
    assert report["status"] == "defective"
    cracks = report["interfaces"][0]["cracks"]
    assert len(cracks) == 1
    assert cracks[0]["kind"] == "crack"
    assert cracks[0]["global_id"] == 2
    assert cracks[0]["distance"] == pytest.approx(1e-3)
    # 裂缝两侧节点均涉缺陷, 触及的单元隔离, 分区内部正常单元仍可入仓
    assert report["acceptance"]["part0"] == {"accepted": 2, "quarantined": 2}
    assert report["acceptance"]["part1"] == {"accepted": 2, "quarantined": 2}


def test_same_id_opposite_coordinates_cannot_merge():
    part1 = _part1()
    part1.points[0] = [-1.0, 0.0, 0.0]  # 全局点 2 坐标相反
    report = _check(_part0(), part1)
    cracks = report["interfaces"][0]["cracks"]
    assert len(cracks) == 1
    assert cracks[0]["kind"] == "opposite_coordinates"
    assert "不能直接并" in cracks[0]["detail"]


def test_missing_node_recorded_as_shortage():
    part1 = _part1()
    part1.point_sets["interface:mid"] = [0, 2]  # 缺全局点 6
    report = _check(_part0(), part1)
    missing = report["interfaces"][0]["missing"]
    assert len(missing) == 1
    assert missing[0]["kind"] == "missing_node"
    assert missing[0]["global_id"] == 6
    assert missing[0]["missing_on"] == "part1"
    # 缺件节点触及的单元隔离, 其余入仓
    assert report["acceptance"]["part0"] == {"accepted": 2, "quarantined": 2}


def test_missing_interface_side_recorded_as_shortage():
    part1 = _part1()
    part1.point_sets = {}
    part1.cell_sets = {}
    report = _check(_part0(), part1)
    missing = report["interfaces"][0]["missing"]
    assert any(
        item["kind"] == "missing_interface" and item["side"] == "part1"
        for item in missing
    )
    assert report["status"] == "defective"


def _tet_pair(iface_order_b):
    """共享三角面 (全局点 2,3,4) 的两个四面体分区."""
    part_a = meshio.Mesh(
        points=[
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        cells=[
            ("tetra", [[0, 1, 2, 3]]),
            ("triangle", [[1, 2, 3]]),
        ],
        point_data={"global_node_id": [1, 2, 3, 4], "temperature": [0.0, 1.0, 0.0, 0.0]},
        point_sets={"interface:mid": [1, 2, 3]},
        cell_sets={"interface:mid": [np.array([], dtype=int), np.array([0])]},
    )
    part_b = meshio.Mesh(
        points=[
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 1.0, 1.0],
        ],
        cells=[
            ("tetra", [[0, 1, 2, 3]]),
            ("triangle", [iface_order_b]),
        ],
        point_data={"global_node_id": [2, 3, 4, 5], "temperature": [1.0, 0.0, 0.0, 1.0]},
        point_sets={"interface:mid": [0, 1, 2]},
        cell_sets={"interface:mid": [np.array([], dtype=int), np.array([0])]},
    )
    return part_a, part_b


def test_opposite_orientation_faces_compatible():
    part_a, part_b = _tet_pair([0, 2, 1])  # 全局点序 (2,4,3), 与对侧相反
    report = _check(part_a, part_b)
    assert report["interfaces"][0]["orientation"] == []
    assert report["status"] == "ok"


def test_same_orientation_faces_incompatible():
    part_a, part_b = _tet_pair([0, 1, 2])  # 全局点序 (2,3,4), 与对侧同向
    report = _check(part_a, part_b)
    orientation = report["interfaces"][0]["orientation"]
    assert len(orientation) == 1
    assert orientation[0]["kind"] == "same_orientation"
    assert orientation[0]["face"] == [2, 3, 4]


def test_field_discontinuity_reported():
    part1 = _part1()
    part1.point_data["temperature"][2] = 3.5  # 全局点 4 场值跳变
    report = _check(_part0(), part1)
    discontinuities = report["interfaces"][0]["field_discontinuities"]
    assert len(discontinuities) == 1
    assert discontinuities[0]["field"] == "temperature"
    assert discontinuities[0]["global_id"] == 4
    assert discontinuities[0]["difference"] == pytest.approx(2.5)


def test_cli_on_vtu_delivery(tmp_path):
    part0, part1 = _part0(), _part1()
    part1.points[0] = [1.0 + 1e-3, 0.0, 0.0]  # 制造一条裂缝
    meshio.write(tmp_path / "part0.vtu", copy.deepcopy(part0))
    meshio.write(tmp_path / "part1.vtu", copy.deepcopy(part1))
    manifest = _manifest("part0.vtu", "part1.vtu")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = tmp_path / "report.json"

    exit_code = partition_check.main([str(manifest_path), "-o", str(report_path)])

    assert exit_code == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "defective"
    cracks = report["interfaces"][0]["cracks"]
    assert len(cracks) == 1
    assert cracks[0]["global_id"] == 2
    assert report["acceptance"]["part1"]["quarantined"] == 2


def test_cli_clean_delivery_exit_zero(tmp_path):
    meshio.write(tmp_path / "part0.vtu", copy.deepcopy(_part0()))
    meshio.write(tmp_path / "part1.vtu", copy.deepcopy(_part1()))
    manifest = _manifest("part0.vtu", "part1.vtu")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert partition_check.main([str(manifest_path)]) == 0
