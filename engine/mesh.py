from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.spatial import Delaunay, QhullError
from shapely import contains_xy
from shapely.geometry import LineString, Polygon

from blender_lidartool.engine.geometry import Zone, generate_zone_points, lines_from_xy


def build_mesh(lines_xy: list[np.ndarray], zones: list[Zone], terrain_buffer_m: float, sampler, zone1_extra=None, extras=None):
    xy, steps, zone_ids, rect, _network = generate_zone_points(
        lines_xy, zones, terrain_buffer_m, zone1_extra, extras
    )
    elevations = np.asarray(sampler.sample(xy[:, 0], xy[:, 1]), dtype=np.float64)
    elevations = _fill_small_gaps(xy, elevations, steps)
    valid = np.isfinite(elevations)
    if valid.sum() < 3:
        raise ValueError("Ve výškovém rastru není dost bodů pro mesh. Zkontrolujte stažená data a rozsah trati.")
    xy = xy[valid]
    steps = steps[valid]
    zone_ids = zone_ids[valid]
    elevations = elevations[valid]
    local_origin = xy.mean(axis=0)
    try:
        triangulation = Delaunay(xy - local_origin)
    except QhullError:
        try:
            triangulation = Delaunay(xy - local_origin, qhull_options="QJ")
        except QhullError as exc:
            raise ValueError("Body meshe jsou degenerované, mesh nelze sestavit.") from exc
    faces = triangulation.simplices
    a = xy[faces[:, 0]]
    b = xy[faces[:, 1]]
    c = xy[faces[:, 2]]
    area2 = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    centroids = (a + b + c) / 3.0
    inside = contains_xy(rect, centroids[:, 0], centroids[:, 1])
    keep = (np.abs(area2) > 1e-4) & inside
    faces = faces[keep]
    faces = _fill_interior_holes(faces, xy, rect)
    if len(faces) == 0:
        raise ValueError("Po ořezu nezůstaly žádné trojúhelníky. Zvětšete zóny nebo stáhněte podrobnější výšky.")
    a = xy[faces[:, 0]]
    b = xy[faces[:, 1]]
    c = xy[faces[:, 2]]
    winding = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    flip = winding < 0
    if flip.any():
        swapped = faces[flip, 1].copy()
        faces[flip, 1] = faces[flip, 2]
        faces[flip, 2] = swapped
    main = lines_from_xy(lines_xy)[0]
    midpoint = main.interpolate(main.length / 2.0)
    origin_z = float(sampler.sample([midpoint.x], [midpoint.y])[0])
    if not np.isfinite(origin_z):
        origin_z = float(np.nanmean(elevations))
    origin = np.array([midpoint.x, midpoint.y, origin_z], dtype=np.float64)
    vertices = np.column_stack([xy[:, 0] - origin[0], xy[:, 1] - origin[1], elevations - origin[2]])
    return {
        "vertices": vertices.astype(np.float32),
        "vertices_abs": xy.astype(np.float64),
        "elevations": elevations.astype(np.float64),
        "faces": faces.astype(np.int32),
        "zones": zone_ids.astype(np.int32),
        "origin": origin,
    }


def write_mesh_files(folder: Path, mesh: dict) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        folder / "mesh.npz",
        vertices=mesh["vertices"],
        vertices_abs=mesh["vertices_abs"],
        elevations=mesh["elevations"],
        faces=mesh["faces"],
        zones=mesh["zones"],
        origin=mesh["origin"],
    )
    export_vertices = y_up_vertices(mesh["vertices"])
    colored = _colored_mesh(export_vertices, mesh["faces"])
    glb_path = folder / "terrain.glb"
    obj_path = folder / "terrain.obj"
    colored.export(glb_path)
    _write_obj(obj_path, export_vertices, mesh["faces"])
    return {
        "message": (
            f"Mesh má {len(mesh['faces']):,} trojúhelníků. "
            "V Blenderu je výška nahoru. OBJ nechte na výchozím importu, Y nahoru a -Z dopředu."
        ).replace(",", " "),
        "triangles": int(len(mesh["faces"])),
        "vertices": int(len(mesh["vertices"])),
        "files": ["terrain.glb", "terrain.obj"],
    }


def load_mesh(folder: Path) -> dict:
    path = folder / "mesh.npz"
    if not path.exists():
        raise ValueError("Nejdřív vygenerujte mesh.")
    data = np.load(path)
    return {
        "vertices": data["vertices"],
        "vertices_abs": data["vertices_abs"],
        "elevations": data["elevations"],
        "faces": data["faces"],
        "zones": data["zones"],
        "origin": data["origin"],
    }


def _colored_mesh(vertices: np.ndarray, faces: np.ndarray):
    import trimesh

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    heights = vertices[:, 2].astype(np.float64)
    span = float(heights.max() - heights.min()) or 1.0
    blend = (heights - heights.min()) / span
    colors = np.zeros((len(vertices), 4), dtype=np.uint8)
    colors[:, 0] = 90 + (120 * blend)
    colors[:, 1] = 120 + (50 * (1 - blend))
    colors[:, 2] = 70
    colors[:, 3] = 255
    mesh.visual.vertex_colors = colors
    return mesh


def y_up_vertices(vertices: np.ndarray) -> np.ndarray:
    """Z-up (východ, sever, výška) na glTF a výchozí Blender OBJ (východ, výška, -sever)."""
    vertices = np.asarray(vertices, dtype=np.float64)
    return np.column_stack([vertices[:, 0], vertices[:, 2], -vertices[:, 1]]).astype(np.float32)


def _fill_small_gaps(xy: np.ndarray, elevations: np.ndarray, steps: np.ndarray) -> np.ndarray:
    invalid = ~np.isfinite(elevations)
    if not invalid.any() or (~invalid).sum() < 3:
        return elevations
    from scipy.spatial import cKDTree

    source = elevations[~invalid]
    tree = cKDTree(xy[~invalid])
    distance, index = tree.query(xy[invalid], k=1)
    distance = np.atleast_1d(np.asarray(distance, dtype=np.float64))
    index = np.atleast_1d(np.asarray(index, dtype=np.int64))
    limit = np.maximum(steps[invalid] * 3.0, 8.0)
    filled = elevations.copy()
    targets = np.flatnonzero(invalid)
    accept = distance <= limit
    filled[targets[accept]] = source[index[accept]]
    leftover = ~np.isfinite(filled)
    if leftover.any() and (~invalid).sum() >= 1:
        distance, index = tree.query(xy[leftover], k=1)
        index = np.atleast_1d(np.asarray(index, dtype=np.int64))
        filled[np.flatnonzero(leftover)] = source[index]
    return filled


def interior_hole_edge_count(faces: np.ndarray, xy: np.ndarray, rect) -> int:
    loops = _edge_loops(_interior_boundary_edges(np.asarray(faces, dtype=np.int32), np.asarray(xy, dtype=np.float64), rect))
    return sum(len(loop) for loop in loops if len(loop) >= 3)


def _undirected_edges(faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(faces) == 0:
        empty = np.zeros((0, 2), dtype=np.int32)
        return empty, np.zeros((0,), dtype=np.int32)
    raw = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=0)
    raw.sort(axis=1)
    unique, counts = np.unique(raw, axis=0, return_counts=True)
    return unique.astype(np.int32), counts.astype(np.int32)


def _border_margin(xy: np.ndarray, rect) -> float:
    minx, miny, maxx, maxy = rect.bounds
    span = max(maxx - minx, maxy - miny)
    if len(xy) < 8:
        return max(2.0, 0.04 * span)
    diffs = np.diff(np.sort(xy[:, 0]))
    diffs = diffs[diffs > 1e-6]
    typical = float(np.percentile(diffs, 75)) if len(diffs) else 2.0
    return max(typical * 1.6, 0.015 * span, 1.0)


def _distance_to_rect_border(points: np.ndarray, rect) -> np.ndarray:
    minx, miny, maxx, maxy = rect.bounds
    x = points[:, 0]
    y = points[:, 1]
    return np.minimum(np.minimum(x - minx, maxx - x), np.minimum(y - miny, maxy - y))


def _interior_boundary_edges(faces: np.ndarray, xy: np.ndarray, rect) -> np.ndarray:
    edges, counts = _undirected_edges(faces)
    border = edges[counts == 1]
    if len(border) == 0:
        return border
    mid = (xy[border[:, 0]] + xy[border[:, 1]]) * 0.5
    lengths = np.linalg.norm(xy[border[:, 0]] - xy[border[:, 1]], axis=1)
    dist = _distance_to_rect_border(mid, rect)
    margin = _border_margin(xy, rect)
    interior = (dist > np.maximum(margin, lengths * 0.6)) & contains_xy(rect, mid[:, 0], mid[:, 1])
    return border[interior]


def _edge_loops(edges: np.ndarray) -> list[list[int]]:
    if len(edges) == 0:
        return []
    adj: dict[int, list[int]] = {}
    unused: set[tuple[int, int]] = set()
    for a_raw, b_raw in edges:
        a = int(a_raw)
        b = int(b_raw)
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
        unused.add((min(a, b), max(a, b)))
    loops: list[list[int]] = []
    while unused:
        start_a, start_b = next(iter(unused))
        unused.remove((start_a, start_b))
        loop = [start_a, start_b]
        prev, cur = start_a, start_b
        closed = False
        for _ in range(len(edges) + 2):
            options = [
                node
                for node in adj.get(cur, [])
                if node != prev and (min(cur, node), max(cur, node)) in unused
            ]
            if not options:
                break
            nxt = options[0]
            unused.discard((min(cur, nxt), max(cur, nxt)))
            if nxt == loop[0]:
                closed = True
                break
            loop.append(nxt)
            prev, cur = cur, nxt
        if closed and len(loop) >= 3:
            loops.append(loop)
    return loops


def _triangulate_loop(loop: list[int], xy: np.ndarray) -> list[list[int]]:
    if len(loop) < 3:
        return []
    if len(loop) == 3:
        return [loop]
    pts = xy[np.asarray(loop, dtype=np.int32)]
    try:
        hole = Polygon(pts)
    except (ValueError, TypeError):
        return []
    if hole.is_empty:
        return []
    if not hole.is_valid:
        hole = hole.buffer(0)
        if hole.is_empty:
            return []
    try:
        triangulation = Delaunay(pts)
    except QhullError:
        try:
            triangulation = Delaunay(pts, qhull_options="QJ")
        except QhullError:
            return []
    centroids = pts[triangulation.simplices].mean(axis=1)
    inside = contains_xy(hole, centroids[:, 0], centroids[:, 1])
    faces = []
    for simplex, keep in zip(triangulation.simplices, inside):
        if keep:
            faces.append([loop[int(simplex[0])], loop[int(simplex[1])], loop[int(simplex[2])]])
    return faces


def _fill_interior_holes(faces: np.ndarray, xy: np.ndarray, rect) -> np.ndarray:
    faces = np.asarray(faces, dtype=np.int32)
    xy = np.asarray(xy, dtype=np.float64)
    for _ in range(24):
        hole_edges = _interior_boundary_edges(faces, xy, rect)
        if len(hole_edges) == 0:
            return faces
        existing = {tuple(sorted(int(v) for v in face)) for face in faces}
        added: list[list[int]] = []
        seen: set[tuple[int, int, int]] = set()
        for loop in _edge_loops(hole_edges):
            for tri in _triangulate_loop(loop, xy):
                key = tuple(sorted(tri))
                if len(key) != 3 or key in existing or key in seen:
                    continue
                seen.add(key)
                added.append(tri)
        if not added:
            break
        faces = np.vstack([faces, np.asarray(added, dtype=np.int32)])
    return faces


def _write_obj(path: Path, vertices: np.ndarray, faces: np.ndarray) -> None:
    lines = [
        "# Blender: výchozí import, Y nahoru, -Z dopředu",
        "# jednotky: metry, počátek ve středu tratě, výška je osa Y",
    ]
    for x_coord, y_coord, z_coord in vertices:
        lines.append(f"v {x_coord:.4f} {y_coord:.4f} {z_coord:.4f}")
    for a, b, c in faces:
        lines.append(f"f {a + 1} {b + 1} {c + 1}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def length_of(lines_xy: list[np.ndarray]) -> float:
    return float(sum(LineString(xy).length for xy in lines_xy if len(xy) >= 2))
