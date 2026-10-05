"""UV střechy z už hotového ortofota terénu. Stačí NumPy, běží ve Blenderu."""

from __future__ import annotations

import numpy as np


def project_roof_uv(vertices, faces, terrain_xy, terrain_faces, terrain_uv, terrain_slot):
    """UV střechy ze stejného ortofota jako terén. Stěny vrací slot -1."""
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int32)
    uv_out = np.zeros((len(faces), 3, 2), dtype=np.float32)
    slot_out = np.full(len(faces), -1, dtype=np.int32)
    if len(faces) == 0 or len(terrain_faces) == 0:
        return uv_out, slot_out
    normals = _unit_normals(vertices, faces)
    roof = np.flatnonzero(normals[:, 2] > 0.55)
    if len(roof) == 0:
        return uv_out, slot_out
    point_uv, point_slot = _sample_terrain_uv(
        vertices[:, :2],
        np.asarray(terrain_xy, dtype=np.float64),
        np.asarray(terrain_faces, dtype=np.int32),
        np.asarray(terrain_uv, dtype=np.float32),
        np.asarray(terrain_slot, dtype=np.int32),
    )
    for face_index in roof:
        corners = faces[face_index]
        slots = point_slot[corners]
        valid = slots >= 0
        if not valid.any():
            continue
        chosen = int(np.bincount(slots[valid]).argmax())
        corner_uv = point_uv[corners].copy()
        if np.any(slots != chosen):
            replacement = corner_uv[slots == chosen][0]
            corner_uv[slots != chosen] = replacement
        uv_out[face_index] = corner_uv
        slot_out[face_index] = chosen
    return uv_out, slot_out


def _unit_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    a = vertices[faces[:, 0]]
    b = vertices[faces[:, 1]]
    c = vertices[faces[:, 2]]
    normal = np.cross(b - a, c - a)
    length = np.linalg.norm(normal, axis=1)
    length[length < 1e-12] = 1.0
    return normal / length[:, None]


def _sample_terrain_uv(points, terrain_xy, terrain_faces, terrain_uv, terrain_slot):
    usable = terrain_slot >= 0
    terrain_faces = terrain_faces[usable]
    terrain_uv = terrain_uv[usable]
    terrain_slot = terrain_slot[usable]
    point_uv = np.zeros((len(points), 2), dtype=np.float32)
    point_slot = np.full(len(points), -1, dtype=np.int32)
    if len(terrain_faces) == 0 or len(points) == 0:
        return point_uv, point_slot
    triangles = terrain_xy[terrain_faces]
    cell = 40.0
    buckets: dict[tuple[int, int], list[int]] = {}
    min_xy = np.floor(triangles.min(axis=1) / cell).astype(np.int32)
    max_xy = np.floor(triangles.max(axis=1) / cell).astype(np.int32)
    for index in range(len(triangles)):
        for ix in range(int(min_xy[index, 0]), int(max_xy[index, 0]) + 1):
            for iy in range(int(min_xy[index, 1]), int(max_xy[index, 1]) + 1):
                buckets.setdefault((ix, iy), []).append(index)
    point_cell = np.floor(points / cell).astype(np.int32)
    grouped: dict[tuple[int, int], list[int]] = {}
    for index, key in enumerate(point_cell):
        grouped.setdefault((int(key[0]), int(key[1])), []).append(index)
    for key, members in grouped.items():
        chosen = buckets.get(key)
        if not chosen:
            continue
        ids = np.asarray(members, dtype=np.int32)
        weights = _barycentric(triangles[np.asarray(chosen, dtype=np.int32)], points[ids])
        inside = np.all(weights >= -1e-4, axis=2)
        hit = inside.any(axis=1)
        if not hit.any():
            continue
        first = np.argmax(inside, axis=1)
        hit_rows = np.flatnonzero(hit)
        picked = np.asarray(chosen, dtype=np.int32)[first[hit]]
        bary = weights[hit_rows, first[hit]]
        point_uv[ids[hit]] = np.sum(terrain_uv[picked] * bary[:, :, None], axis=1)
        point_slot[ids[hit]] = terrain_slot[picked]
    return point_uv, point_slot


def _barycentric(triangles: np.ndarray, points: np.ndarray) -> np.ndarray:
    a = triangles[:, 0]
    b = triangles[:, 1]
    c = triangles[:, 2]
    v0 = b - a
    v1 = c - a
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    den = np.where(np.abs(den) < 1e-12, np.nan, den)
    rel = points[:, None, :] - a[None, :, :]
    w_b = (rel[:, :, 0] * v1[:, 1] - v1[:, 0] * rel[:, :, 1]) / den
    w_c = (v0[:, 0] * rel[:, :, 1] - rel[:, :, 0] * v0[:, 1]) / den
    w_a = 1.0 - w_b - w_c
    return np.stack([w_a, w_b, w_c], axis=2)
