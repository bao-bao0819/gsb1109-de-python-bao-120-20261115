"""分区交付验收检查工具.

按交付清单 (manifest) 读取各分区网格, 依据全局点号和分区接口声明检查:
  - 两侧接口面节点的一一对应 (按全局点号);
  - 坐标重合性 (容差之外报裂缝; 点号相同但坐标相反者不能直接并, 记不相容);
  - 接口面方向 (两侧面法向应相反, 同向记不相容);
  - 场值连续性 (按给定容差报不相容项);
  - 缺一侧接口或缺节点记缺件.

分区内部未触及缺陷项的正常单元仍可入仓, 触及缺陷项的单元隔离待处理.

用法:
    python tools/partition_check.py manifest.json [-o report.json]

manifest 示例:
    {
      "global_id_key": "global_node_id",
      "tolerances": {"coordinates": 1e-6, "field": 1e-6},
      "fields": ["temperature"],
      "partitions": {"part0": "part0.vtu", "part1": "part1.vtu"},
      "interfaces": [{"name": "mid", "sides": ["part0", "part1"]}]
    }

接口成员声明: point_sets["interface:<name>"] (或 VTU 落地后的
point_data["interface:<name>"], 取值 >= 0 为成员); 接口面声明:
cell_sets["interface:<name>"] (或对应的 cell_data).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

import meshio

DEFAULT_ID_KEYS = ("global_node_id", "global_id", "gmsh:global")
INTERFACE_SET_PREFIX = "interface:"


def _interface_set_name(name: str) -> str:
    return f"{INTERFACE_SET_PREFIX}{name}"


def _resolve_id_key(mesh: meshio.Mesh, requested: str | None) -> str:
    if requested is not None:
        if requested not in mesh.point_data:
            raise KeyError(
                f"分区网格缺少全局点号数组 '{requested}' "
                f"(现有: {sorted(mesh.point_data)})"
            )
        return requested
    for key in DEFAULT_ID_KEYS:
        if key in mesh.point_data:
            return key
    raise KeyError(
        "分区网格缺少全局点号数组, 尝试过: " + ", ".join(DEFAULT_ID_KEYS)
    )


def _interface_node_indices(mesh: meshio.Mesh, set_name: str):
    """接口节点局部编号; 未声明返回 None."""
    if set_name in mesh.point_sets:
        return np.asarray(mesh.point_sets[set_name], dtype=np.int64)
    if set_name in mesh.point_data:
        flags = np.asarray(mesh.point_data[set_name])
        return np.nonzero(flags >= 0)[0].astype(np.int64)
    return None


def _interface_faces(mesh: meshio.Mesh, set_name: str, id_key: str) -> dict:
    """接口面 {排序后的全局点号元组: 声明顺序的全局点号列表}."""
    gids = np.asarray(mesh.point_data[id_key])
    faces: dict[tuple, list] = {}
    if set_name in mesh.cell_sets:
        blocks = (
            (block, np.asarray(mask))
            for block, mask in zip(mesh.cells, mesh.cell_sets[set_name])
            if mask is not None
        )
        for block, mask in blocks:
            for cell in np.asarray(block.data)[mask]:
                order = [int(g) for g in gids[cell]]
                faces.setdefault(tuple(sorted(order)), order)
    elif set_name in mesh.cell_data:
        for block, flags in zip(mesh.cells, mesh.cell_data[set_name]):
            flags = np.asarray(flags)
            for cell in np.asarray(block.data)[flags >= 0]:
                order = [int(g) for g in gids[cell]]
                faces.setdefault(tuple(sorted(order)), order)
    return faces


def _is_cyclic_rotation(reference: list, candidate: list) -> bool:
    if len(reference) != len(candidate) or not reference:
        return False
    doubled = reference + reference
    n = len(reference)
    return any(doubled[i : i + n] == candidate for i in range(n))


def _check_interface(
    iface: dict,
    meshes: dict,
    id_keys: dict,
    coord_tol: float,
    field_tol: float,
    fields,
    defect_nodes: dict,
) -> dict:
    name = iface["name"]
    side_a, side_b = iface["sides"]
    result = {
        "name": name,
        "sides": [side_a, side_b],
        "status": "ok",
        "matched_nodes": 0,
        "cracks": [],
        "missing": [],
        "orientation": [],
        "field_discontinuities": [],
    }
    set_name = _interface_set_name(name)

    side_nodes = {}
    for side in (side_a, side_b):
        mesh = meshes[side]
        idx = _interface_node_indices(mesh, set_name)
        if idx is None:
            result["missing"].append(
                {
                    "kind": "missing_interface",
                    "side": side,
                    "detail": f"分区 {side} 缺少接口 '{name}' 的声明, 记缺件",
                }
            )
            side_nodes[side] = None
        else:
            gids = np.asarray(mesh.point_data[id_keys[side]], dtype=np.int64)[idx]
            side_nodes[side] = (idx, gids)

    if side_nodes[side_a] is None or side_nodes[side_b] is None:
        for side in (side_a, side_b):
            if side_nodes[side] is not None:
                defect_nodes[side].update(int(i) for i in side_nodes[side][0])
        result["status"] = "defective"
        return result

    idx_a, gid_a = side_nodes[side_a]
    idx_b, gid_b = side_nodes[side_b]
    map_a = dict(zip(gid_a.tolist(), idx_a.tolist()))
    map_b = dict(zip(gid_b.tolist(), idx_b.tolist()))

    for gid in sorted(set(map_a) - set(map_b)):
        result["missing"].append(
            {
                "kind": "missing_node",
                "global_id": int(gid),
                "present_on": side_a,
                "missing_on": side_b,
            }
        )
        defect_nodes[side_a].add(map_a[gid])
    for gid in sorted(set(map_b) - set(map_a)):
        result["missing"].append(
            {
                "kind": "missing_node",
                "global_id": int(gid),
                "present_on": side_b,
                "missing_on": side_a,
            }
        )
        defect_nodes[side_b].add(map_b[gid])

    common = sorted(set(map_a) & set(map_b))
    points_a = np.asarray(meshes[side_a].points, dtype=float)
    points_b = np.asarray(meshes[side_b].points, dtype=float)
    for gid in common:
        ia, ib = map_a[gid], map_b[gid]
        xa, xb = points_a[ia], points_b[ib]
        distance = float(np.linalg.norm(xa - xb))
        result["matched_nodes"] += 1
        if distance <= coord_tol:
            continue
        entry = {
            "global_id": int(gid),
            "coords_" + side_a: xa.tolist(),
            "coords_" + side_b: xb.tolist(),
            "distance": distance,
        }
        if float(np.linalg.norm(xa + xb)) <= coord_tol:
            entry["kind"] = "opposite_coordinates"
            entry["detail"] = "点号相同但坐标相反, 不能直接并, 需先作坐标变换"
        else:
            entry["kind"] = "crack"
            entry["gap_vector"] = (xb - xa).tolist()
        result["cracks"].append(entry)
        defect_nodes[side_a].add(ia)
        defect_nodes[side_b].add(ib)

    faces_a = _interface_faces(meshes[side_a], set_name, id_keys[side_a])
    faces_b = _interface_faces(meshes[side_b], set_name, id_keys[side_b])
    for key in sorted(set(faces_a) | set(faces_b)):
        if key not in faces_b:
            result["missing"].append(
                {"kind": "missing_face", "face": list(key), "missing_on": side_b}
            )
            defect_nodes[side_a].update(
                map_a[g] for g in key if g in map_a
            )
            continue
        if key not in faces_a:
            result["missing"].append(
                {"kind": "missing_face", "face": list(key), "missing_on": side_a}
            )
            defect_nodes[side_b].update(
                map_b[g] for g in key if g in map_b
            )
            continue
        order_a, order_b = faces_a[key], faces_b[key]
        if _is_cyclic_rotation(order_a[::-1], order_b):
            continue
        kind = (
            "same_orientation"
            if _is_cyclic_rotation(order_a, order_b)
            else "incompatible_ordering"
        )
        result["orientation"].append(
            {
                "kind": kind,
                "face": list(key),
                "order_" + side_a: order_a,
                "order_" + side_b: order_b,
                "detail": "两侧接口面方向应相反",
            }
        )
        for side, mapping in ((side_a, map_a), (side_b, map_b)):
            defect_nodes[side].update(mapping[g] for g in key if g in mapping)

    if fields is None:
        fields = [
            key
            for key in meshes[side_a].point_data
            if key != id_keys[side_a]
            and not key.startswith("gmsh:")
            and not key.startswith(INTERFACE_SET_PREFIX)
        ]
    for field in fields:
        values_a = meshes[side_a].point_data.get(field)
        values_b = meshes[side_b].point_data.get(field)
        if values_a is None or values_b is None:
            result["missing"].append(
                {
                    "kind": "missing_field",
                    "field": field,
                    "missing_on": side_a if values_a is None else side_b,
                }
            )
            continue
        for gid in common:
            ia, ib = map_a[gid], map_b[gid]
            diff = float(
                np.max(np.abs(np.asarray(values_a[ia]) - np.asarray(values_b[ib])))
            )
            if diff > field_tol:
                result["field_discontinuities"].append(
                    {
                        "field": field,
                        "global_id": int(gid),
                        "difference": diff,
                        "value_" + side_a: np.asarray(values_a[ia]).tolist(),
                        "value_" + side_b: np.asarray(values_b[ib]).tolist(),
                    }
                )
                defect_nodes[side_a].add(ia)
                defect_nodes[side_b].add(ib)

    if (
        result["cracks"]
        or result["missing"]
        or result["orientation"]
        or result["field_discontinuities"]
    ):
        result["status"] = "defective"
    return result


def check_delivery(manifest: dict, base_dir=".") -> dict:
    """按清单检查整批分区交付, 返回验收报告."""
    tolerances = manifest.get("tolerances", {})
    coord_tol = float(tolerances.get("coordinates", 1e-8))
    field_tol = float(tolerances.get("field", 1e-8))

    meshes: dict[str, meshio.Mesh] = {}
    id_keys: dict[str, str] = {}
    for name, source in manifest["partitions"].items():
        mesh = (
            source
            if isinstance(source, meshio.Mesh)
            else meshio.read(Path(base_dir) / source)
        )
        meshes[name] = mesh
        id_keys[name] = _resolve_id_key(mesh, manifest.get("global_id_key"))

    defect_nodes: dict[str, set] = {name: set() for name in meshes}
    interfaces = [
        _check_interface(
            iface,
            meshes,
            id_keys,
            coord_tol,
            field_tol,
            manifest.get("fields"),
            defect_nodes,
        )
        for iface in manifest.get("interfaces", [])
    ]

    acceptance = {}
    for name, mesh in meshes.items():
        bad = defect_nodes[name]
        accepted = quarantined = 0
        for block in mesh.cells:
            data = np.asarray(block.data)
            if bad:
                hits = np.isin(data, list(bad)).any(axis=1)
            else:
                hits = np.zeros(len(data), dtype=bool)
            quarantined += int(hits.sum())
            accepted += int((~hits).sum())
        acceptance[name] = {"accepted": accepted, "quarantined": quarantined}

    status = (
        "defective" if any(i["status"] != "ok" for i in interfaces) else "ok"
    )
    return {
        "status": status,
        "tolerances": {"coordinates": coord_tol, "field": field_tol},
        "interfaces": interfaces,
        "acceptance": acceptance,
    }


def _summary(report: dict) -> str:
    lines = []
    for iface in report["interfaces"]:
        sides = "/".join(iface["sides"])
        lines.append(
            f"接口 {iface['name']} ({sides}): 状态 {iface['status']}, "
            f"匹配节点 {iface['matched_nodes']}, "
            f"裂缝 {len(iface['cracks'])}, "
            f"缺件 {len(iface['missing'])}, "
            f"方向不相容 {len(iface['orientation'])}, "
            f"场值不连续 {len(iface['field_discontinuities'])}"
        )
        for crack in iface["cracks"]:
            lines.append(
                f"  裂缝: 全局点 {crack['global_id']} "
                f"类型 {crack['kind']} 间距 {crack['distance']:.3e}"
            )
        for item in iface["missing"]:
            lines.append(f"  缺件: {item}")
        for item in iface["orientation"]:
            lines.append(f"  方向不相容: 面 {item['face']} 类型 {item['kind']}")
        for item in iface["field_discontinuities"]:
            lines.append(
                f"  场值不连续: 场 {item['field']} 全局点 {item['global_id']} "
                f"差值 {item['difference']:.3e}"
            )
    for name, acc in report["acceptance"].items():
        lines.append(
            f"分区 {name}: 入仓 {acc['accepted']} 单元, "
            f"隔离 {acc['quarantined']} 单元"
        )
    lines.append(f"总体状态: {report['status']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="分区交付接口一致性检查")
    parser.add_argument("manifest", help="交付清单 (JSON)")
    parser.add_argument("-o", "--output", help="报告输出路径 (JSON)")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = check_delivery(manifest, base_dir=manifest_path.parent)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(_summary(report))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
