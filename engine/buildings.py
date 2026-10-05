"""Půdorysy ZABAGED vytažené podle DMP OK a posazené na DMR 5G."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import numpy as np
from scipy.spatial import Delaunay, QhullError, cKDTree
from shapely import contains_xy
from shapely.geometry import Polygon

from blender_lidartool.engine.crsutil import segments_to_xy, to_5514
from blender_lidartool.engine.cuzk import DMPOK_URL, DMR_URL, SERVICE_BOUNDS
from blender_lidartool.engine.geometry import densify_xy, iter_tiles, lines_from_xy, snap_bounds

SAMPLE_STEP_M = 1.0
DMR_PIXEL_M = 2.0
DMPOK_MAX_PX = 2048
PAD_M = 20.0
MIN_CLEARANCE_M = 1.5
MIN_AREA_M2 = 4.0
MAX_PIXELS = 20_000_000


def build_buildings(cache: Path, segments: list, features: list, progress) -> str:
    if not features:
        raise ValueError("Vyberte aspoň jednu budovu.")
    if len(features) > 200:
        raise ValueError("Najednou jde vložit nejvýš 200 budov.")
    prepared = []
    for feature in features:
        polygons = _polygons(feature.get("geometry") or {})
        if not polygons:
            continue
        prepared.append((str(feature.get("id") or ""), str(feature.get("name") or "").strip(), polygons))
    if not prepared:
        raise ValueError("Vybrané budovy nemají platný půdorys.")
    bounds = _padded_bounds([polygon for _fid, _name, polygons in prepared for polygon in polygons])
    progress(4, "Stahuji DMP OK")
    surface_path = _download_raster(
        DMPOK_URL,
        bounds,
        SAMPLE_STEP_M,
        cache / "TEMP" / "buildings_dmpok_tiles",
        cache / "TEMP" / "buildings_dmpok.tif",
        lambda pct, message, event=None: progress(4 + 0.36 * pct, message, event),
        DMPOK_MAX_PX,
    )
    progress(42, "Stahuji DMR 5G")
    ground_path = _download_raster(
        DMR_URL,
        bounds,
        DMR_PIXEL_M,
        cache / "TEMP" / "buildings_dmr_tiles",
        cache / "TEMP" / "buildings_dmr.tif",
        lambda pct, message, event=None: progress(42 + 0.28 * pct, message, event),
        4000,
    )
    from blender_lidartool.engine.rasterutil import ElevationSampler

    surface = ElevationSampler([surface_path])
    ground = ElevationSampler([ground_path])
    origin, origin_note = _origin(cache, segments, ground, prepared)
    progress(74, "Sestavuji budovy")
    built = []
    skipped = []
    for fid, name, polygons in prepared:
        label = name or fid or "budova"
        try:
            mesh = building_mesh(polygons, surface, ground, origin)
        except QhullError:
            mesh = None
        if mesh is None:
            skipped.append(f"{label}: nízký objekt nebo chybí výška")
            continue
        vertices, faces = mesh
        built.append({"id": fid, "name": name, "vertices": vertices, "faces": faces})
    if not built:
        detail = "; ".join(skipped[:3])
        raise ValueError(detail or "Z vybraných půdorysů nevznikla žádná budova.")
    _write(cache / "OUTPUT", built, origin, skipped)
    progress(95, "Budovy jsou uložené")
    message = f"Vloženo budov: {len(built)}."
    if skipped:
        if len(skipped) <= 3:
            message += " Přeskočeno: " + "; ".join(skipped) + "."
        else:
            message += f" Přeskočeno {len(skipped)}."
    if origin_note:
        message += " " + origin_note
    return message


def building_mesh(polygons: list, surface, ground, origin: np.ndarray):
    """Vrátí lokální vrcholy a plochy, nebo None když budova nemá výšku."""
    parts = []
    heights = []
    for polygon in polygons:
        part = _mesh_part(polygon, surface, ground)
        if part is None:
            continue
        xy, elevation, faces, clearance = part
        parts.append((xy, elevation, faces))
        heights.append(clearance)
    if not parts or not heights:
        return None
    clearance = np.concatenate(heights)
    finite = clearance[np.isfinite(clearance)]
    if finite.size == 0 or float(np.median(finite)) < MIN_CLEARANCE_M:
        return None
    vertices = []
    faces = []
    offset = 0
    origin = np.asarray(origin, dtype=np.float64).reshape(-1)
    for xy, elevation, part_faces in parts:
        local = np.column_stack(
            [xy[:, 0] - origin[0], xy[:, 1] - origin[1], elevation - origin[2]]
        ).astype(np.float32)
        if len(part_faces) == 0 or len(local) < 3:
            continue
        vertices.append(local)
        faces.append(part_faces + offset)
        offset += len(local)
    if not vertices or not faces:
        return None
    return np.vstack(vertices), np.vstack(faces).astype(np.int32)


def _mesh_part(polygon: Polygon, surface, ground):
    if polygon.is_empty or polygon.area < MIN_AREA_M2:
        return None
    boundary = _clean_ring(densify_xy(np.asarray(polygon.exterior.coords, dtype=np.float64), SAMPLE_STEP_M))
    if len(boundary) < 3:
        return None
    roof_b = np.asarray(surface.sample(boundary[:, 0], boundary[:, 1]), dtype=np.float64)
    ground_b = np.asarray(ground.sample(boundary[:, 0], boundary[:, 1]), dtype=np.float64)
    roof_b = _fill_ring(roof_b)
    ground_b = _fill_ring(ground_b)
    if not (np.isfinite(roof_b).all() and np.isfinite(ground_b).all()):
        return None
    interior = _interior_points(polygon, boundary, SAMPLE_STEP_M)
    if len(interior):
        roof_i = np.asarray(surface.sample(interior[:, 0], interior[:, 1]), dtype=np.float64)
        ground_i = np.asarray(ground.sample(interior[:, 0], interior[:, 1]), dtype=np.float64)
        valid = np.isfinite(roof_i) & np.isfinite(ground_i)
        interior = interior[valid]
        roof_i = roof_i[valid]
        ground_i = ground_i[valid]
    else:
        roof_i = np.zeros(0, dtype=np.float64)
        ground_i = np.zeros(0, dtype=np.float64)
    ground_b = _smooth_ring(ground_b)
    xy = np.vstack([boundary, interior]) if len(interior) else boundary
    base = np.concatenate([ground_b, ground_i])
    clearance = (roof_i - ground_i) if len(roof_i) else (roof_b - ground_b)
    roof = np.maximum(_smooth_roof(boundary, interior, roof_b, roof_i), base)
    try:
        roof_faces = _roof_faces(xy, polygon)
    except QhullError:
        roof_faces = np.zeros((0, 3), dtype=np.int32)
    count = len(boundary)
    xy_all = np.vstack([xy, boundary])
    elevation = np.concatenate([roof, ground_b])
    wall_faces = _wall_faces(count, len(xy))
    chunks = [faces for faces in (roof_faces, wall_faces) if len(faces)]
    if not chunks:
        return None
    return xy_all, elevation, np.vstack(chunks).astype(np.int32), clearance


def _smooth_roof(boundary: np.ndarray, interior: np.ndarray, roof_boundary: np.ndarray, roof_interior: np.ndarray) -> np.ndarray:
    """Výšku střechy vezme z vnitřku půdorysu. Okraj DMP je směs země a střechy a mesh by po obvodu stékal."""
    if len(interior) >= 4:
        source_xy = interior
        source_z = _clip_roof(roof_interior)
    else:
        source_xy = boundary
        source_z = _clip_roof(roof_boundary)
    source_z = _smooth_field(source_xy, source_z, radius=4.0, passes=4)
    eave = _idw(source_xy, source_z, boundary, radius=14.0)
    if len(interior) >= 4:
        roof = np.concatenate([eave, source_z])
        xy = np.vstack([boundary, interior])
    else:
        roof = eave
        xy = boundary
    return _smooth_field(xy, roof, radius=3.0, passes=2)


def _clip_roof(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return values
    center = float(np.median(finite))
    low = center - 2.0
    high = min(float(np.percentile(finite, 90)), center + 2.5)
    if high < low:
        high = low
    return np.clip(values, low, high)


def _smooth_field(xy: np.ndarray, values: np.ndarray, radius: float, passes: int) -> np.ndarray:
    current = np.asarray(values, dtype=np.float64).copy()
    if len(current) < 3:
        return current
    tree = cKDTree(xy)
    sigma = max(radius * 0.5, 0.5)
    for _pass in range(passes):
        groups = tree.query_ball_point(xy, radius)
        updated = current.copy()
        for index, neighbors in enumerate(groups):
            if len(neighbors) < 2:
                continue
            chosen = np.asarray(neighbors, dtype=np.int64)
            distance = np.linalg.norm(xy[chosen] - xy[index], axis=1)
            weight = np.exp(-(distance ** 2) / (2.0 * sigma * sigma))
            updated[index] = float(np.average(current[chosen], weights=weight))
        current = updated
    return current


def _idw(source_xy: np.ndarray, source_z: np.ndarray, target_xy: np.ndarray, radius: float) -> np.ndarray:
    if len(source_xy) == 0:
        return np.full(len(target_xy), np.nan, dtype=np.float64)
    tree = cKDTree(source_xy)
    count = min(8, len(source_xy))
    distance, index = tree.query(target_xy, k=count, distance_upper_bound=radius)
    distance = np.asarray(distance, dtype=np.float64).reshape(len(target_xy), count)
    index = np.asarray(index, dtype=np.int64).reshape(len(target_xy), count)
    output = np.empty(len(target_xy), dtype=np.float64)
    fallback = float(np.median(source_z))
    for row in range(len(target_xy)):
        chosen = index[row]
        dist = distance[row]
        valid = (chosen >= 0) & (chosen < len(source_xy)) & np.isfinite(dist)
        if not valid.any():
            output[row] = fallback
            continue
        dist = np.maximum(dist[valid], 0.05)
        weight = 1.0 / dist ** 2
        output[row] = float(np.average(source_z[chosen[valid]], weights=weight))
    return output


def _smooth_ring(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if len(values) < 5:
        return values.copy()
    window = 3
    current = values
    for _pass in range(2):
        updated = current.copy()
        count = len(current)
        for index in range(count):
            samples = [current[(index + shift) % count] for shift in range(-window, window + 1)]
            updated[index] = float(np.mean(samples))
        current = updated
    return current


def _roof_faces(xy: np.ndarray, polygon: Polygon) -> np.ndarray:
    if len(xy) < 3:
        return np.zeros((0, 3), dtype=np.int32)
    local = xy - xy.mean(axis=0)
    try:
        triangulation = Delaunay(local)
    except QhullError:
        triangulation = Delaunay(local, qhull_options="QJ")
    faces = np.asarray(triangulation.simplices, dtype=np.int32)
    a = xy[faces[:, 0]]
    b = xy[faces[:, 1]]
    c = xy[faces[:, 2]]
    area2 = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    centroids = (a + b + c) / 3.0
    inside = contains_xy(polygon, centroids[:, 0], centroids[:, 1])
    faces = faces[(np.abs(area2) > 1e-4) & inside]
    if len(faces) == 0:
        return faces
    a = xy[faces[:, 0]]
    b = xy[faces[:, 1]]
    c = xy[faces[:, 2]]
    winding = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    flip = winding < 0
    if flip.any():
        swapped = faces[flip, 1].copy()
        faces[flip, 1] = faces[flip, 2]
        faces[flip, 2] = swapped
    return faces


def _wall_faces(boundary_count: int, roof_count: int) -> np.ndarray:
    faces = []
    for index in range(boundary_count):
        nxt = (index + 1) % boundary_count
        ground_index = roof_count + index
        ground_next = roof_count + nxt
        faces.append((index, ground_next, nxt))
        faces.append((index, ground_index, ground_next))
    return np.asarray(faces, dtype=np.int32)


def _interior_points(polygon: Polygon, boundary: np.ndarray, step: float) -> np.ndarray:
    minx, miny, maxx, maxy = polygon.bounds
    xs = np.arange(minx + step * 0.5, maxx, step)
    ys = np.arange(miny + step * 0.5, maxy, step)
    if len(xs) == 0 or len(ys) == 0:
        return np.zeros((0, 2), dtype=np.float64)
    grid_x, grid_y = np.meshgrid(xs, ys)
    points = np.column_stack([grid_x.ravel(), grid_y.ravel()])
    if len(points) > 12000:
        return _interior_points(polygon, boundary, step * int(np.ceil(np.sqrt(len(points) / 8000.0))))
    points = points[contains_xy(polygon, points[:, 0], points[:, 1])]
    if len(points) == 0 or len(boundary) == 0:
        return points
    tree = cKDTree(boundary)
    distance, _index = tree.query(points, distance_upper_bound=0.45)
    near = np.isfinite(distance) & (distance <= 0.45)
    return points[~near]


def _clean_ring(xy: np.ndarray) -> np.ndarray:
    xy = np.asarray(xy, dtype=np.float64)
    if len(xy) > 1 and np.allclose(xy[0], xy[-1]):
        xy = xy[:-1]
    if len(xy) < 2:
        return xy
    keep = [0]
    for index in range(1, len(xy)):
        if float(np.hypot(*(xy[index] - xy[keep[-1]]))) > 0.05:
            keep.append(index)
    if len(keep) > 2 and float(np.hypot(*(xy[keep[0]] - xy[keep[-1]]))) <= 0.05:
        keep.pop()
    return xy[np.asarray(keep, dtype=np.int64)]


def _fill_ring(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64).copy()
    finite = np.isfinite(values)
    if finite.all() or not finite.any():
        return values
    count = len(values)
    doubled = np.concatenate([values, values])
    last = None
    for index in range(2 * count):
        if np.isfinite(doubled[index]):
            last = float(doubled[index])
        elif last is not None:
            doubled[index] = last
    if not np.isfinite(doubled[count:]).all():
        last = None
        for index in range(2 * count - 1, -1, -1):
            if np.isfinite(doubled[index]):
                last = float(doubled[index])
            elif last is not None:
                doubled[index] = last
    return doubled[count:]


def _polygons(geometry: dict) -> list[Polygon]:
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind not in ("Polygon", "MultiPolygon") or not isinstance(coords, list):
        return []
    raw = [coords] if kind == "Polygon" else coords
    polygons = []
    for rings in raw:
        if not isinstance(rings, list) or not rings:
            continue
        shell = _xy_ring(rings[0])
        holes = [_xy_ring(ring) for ring in rings[1:] if len(ring) >= 4]
        if len(shell) < 4:
            continue
        polygon = Polygon(shell, holes)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            continue
        if polygon.geom_type == "Polygon":
            polygons.append(polygon)
        elif polygon.geom_type == "MultiPolygon":
            polygons.extend(part for part in polygon.geoms if not part.is_empty)
    return polygons


def _xy_ring(ring) -> np.ndarray:
    lon = np.array([point[0] for point in ring], dtype=np.float64)
    lat = np.array([point[1] for point in ring], dtype=np.float64)
    xs, ys = to_5514(lon, lat)
    return np.column_stack([np.atleast_1d(xs), np.atleast_1d(ys)])


def _padded_bounds(polygons: list[Polygon]):
    minx = min(polygon.bounds[0] for polygon in polygons) - PAD_M
    miny = min(polygon.bounds[1] for polygon in polygons) - PAD_M
    maxx = max(polygon.bounds[2] for polygon in polygons) + PAD_M
    maxy = max(polygon.bounds[3] for polygon in polygons) + PAD_M
    sx0, sy0, sx1, sy1 = SERVICE_BOUNDS
    minx = max(minx, sx0)
    miny = max(miny, sy0)
    maxx = min(maxx, sx1)
    maxy = min(maxy, sy1)
    if maxx <= minx or maxy <= miny:
        raise ValueError("Budovy leží mimo pokrytí výškových dat ČÚZK.")
    return minx, miny, maxx, maxy


def _download_raster(url: str, bounds, pixel: float, folder: Path, dest: Path, progress, max_px: int) -> Path:
    from blender_lidartool.engine.demops import _download_tiles, _mosaic
    minx, miny, maxx, maxy = snap_bounds(*bounds, pixel)
    tiles = [(*tile, "fine") for tile in iter_tiles(minx, miny, maxx, maxy, pixel, max_px=max_px)]
    pixels = sum(tile[4] * tile[5] for tile in tiles)
    if pixels > MAX_PIXELS:
        raise ValueError("Vybrané budovy pokrývají moc velkou plochu. Vložte je po menších skupinách.")
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    saved = _download_tiles(url, tiles, folder, progress)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _mosaic([path for path, _kind in saved], dest)
    shutil.rmtree(folder, ignore_errors=True)
    return dest


def _origin(cache: Path, segments: list, ground, prepared) -> tuple[np.ndarray, str]:
    stored = cache / "OUTPUT" / "mesh.npz"
    if stored.is_file():
        origin = np.asarray(np.load(stored)["origin"], dtype=np.float64).reshape(-1)
        if origin.size >= 3 and np.isfinite(origin[:3]).all():
            return origin[:3].copy(), ""
    if segments and isinstance(segments, list) and segments and len(segments[0]) >= 2:
        try:
            main = lines_from_xy(segments_to_xy(segments))[0]
            midpoint = main.interpolate(main.length / 2.0)
            height = float(np.asarray(ground.sample([midpoint.x], [midpoint.y])).reshape(-1)[0])
            if np.isfinite(height):
                return np.array([midpoint.x, midpoint.y, height], dtype=np.float64), "Počátek je ze středu tratě."
        except (ValueError, IndexError):
            pass
    xs = []
    ys = []
    for _fid, _name, polygons in prepared:
        for polygon in polygons:
            xs.append(polygon.centroid.x)
            ys.append(polygon.centroid.y)
    cx = float(np.mean(xs))
    cy = float(np.mean(ys))
    height = float(np.asarray(ground.sample([cx], [cy])).reshape(-1)[0])
    if not np.isfinite(height):
        height = 0.0
    return (
        np.array([cx, cy, height], dtype=np.float64),
        "Bez terénu a tratě budovy nesedí na pozdější terén, dokud je nevložíte znovu.",
    )


def _write(output: Path, built: list, origin: np.ndarray, skipped: list) -> None:
    folder = output / "buildings"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"origin": [float(value) for value in origin], "buildings": [], "skipped": skipped}
    for item in built:
        filename = f"{_file_id(item['id'])}.npz"
        np.savez_compressed(folder / filename, vertices=item["vertices"], faces=item["faces"])
        manifest["buildings"].append(
            {"id": item["id"], "name": item["name"], "file": f"buildings/{filename}"}
        )
    text = json.dumps(manifest, ensure_ascii=False)
    (output / "buildings.json").write_text(text, encoding="utf-8")


def _file_id(building_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", building_id).strip("_")
    return (cleaned or "budova")[:80]
