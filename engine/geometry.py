from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from shapely import contains_xy
from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import unary_union

from blender_lidartool.engine.crsutil import segments_to_xy, to_5514


@dataclass
class Zone:
    id: int
    radius_m: float | None
    step_m: float


def parse_zones(raw: list[dict]) -> list[Zone]:
    zones: list[Zone] = []
    last = 0.0
    for index, item in enumerate(raw, start=1):
        radius = item.get("radius_m")
        step = float(item.get("step_m") or 0)
        if step <= 0:
            raise ValueError("Krok zóny musí být větší než nula.")
        radius_m = None if radius in (None, "", 0) else float(radius)
        if radius_m is not None:
            if radius_m <= 0:
                raise ValueError("Poloměr zóny musí být větší než nula.")
            if radius_m < last:
                raise ValueError("Poloměry zón musí růst od silnice ven.")
            last = radius_m
        zones.append(Zone(index, radius_m, step))
    if not zones:
        raise ValueError("Chybí zóny meshe.")
    return zones


def lines_from_xy(lines_xy: list[np.ndarray]) -> list[LineString]:
    lines = []
    for xy in lines_xy:
        if len(xy) >= 2:
            lines.append(LineString(np.asarray(xy, dtype=np.float64)))
    if not lines:
        raise ValueError("Trať musí mít alespoň dva body.")
    return lines


def united_line(lines: list[LineString]):
    if len(lines) == 1:
        return lines[0]
    return unary_union(lines)


def terrain_rectangle(lines: list[LineString], buffer_m: float):
    if buffer_m <= 0:
        raise ValueError("Hranice terénu musí být větší než nula.")
    minx = min(line.bounds[0] for line in lines)
    miny = min(line.bounds[1] for line in lines)
    maxx = max(line.bounds[2] for line in lines)
    maxy = max(line.bounds[3] for line in lines)
    return box(minx - buffer_m, miny - buffer_m, maxx + buffer_m, maxy + buffer_m)


def road_corridor(line, buffer_m: float):
    if buffer_m <= 0:
        raise ValueError("Koridor silnice musí být větší než nula.")
    return line.buffer(buffer_m, cap_style="round", join_style="round")


def snap_bounds(minx: float, miny: float, maxx: float, maxy: float, pixel: float):
    if pixel <= 0:
        raise ValueError("Krok rastru musí být větší než nula.")
    minx = float(np.floor(minx / pixel) * pixel)
    miny = float(np.floor(miny / pixel) * pixel)
    maxx = float(np.ceil(maxx / pixel) * pixel)
    maxy = float(np.ceil(maxy / pixel) * pixel)
    if maxx <= minx:
        maxx = minx + pixel
    if maxy <= miny:
        maxy = miny + pixel
    return minx, miny, maxx, maxy


def iter_tiles(minx: float, miny: float, maxx: float, maxy: float, pixel: float, max_px: int = 4000):
    minx, miny, maxx, maxy = snap_bounds(minx, miny, maxx, maxy, pixel)
    span = pixel * max_px
    tiles = []
    x = minx
    while x < maxx - pixel * 0.1:
        remaining_w = int(round((min(x + span, maxx) - x) / pixel))
        width = max(1, min(max_px, remaining_w))
        x2 = x + width * pixel
        y = miny
        while y < maxy - pixel * 0.1:
            remaining_h = int(round((min(y + span, maxy) - y) / pixel))
            height = max(1, min(max_px, remaining_h))
            y2 = y + height * pixel
            tiles.append((x, y, x2, y2, width, height))
            y = y2
        x = x2
    return tiles


def densify_xy(xy: np.ndarray, step: float) -> np.ndarray:
    xy = np.asarray(xy, dtype=np.float64)
    if len(xy) < 2:
        return xy
    pieces = []
    for start, end in zip(xy[:-1], xy[1:]):
        dist = float(np.hypot(*(end - start)))
        count = max(1, int(round(dist / step))) if step > 0 else 1
        samples = np.linspace(0.0, 1.0, count, endpoint=False)
        pieces.append(start + (end - start) * samples[:, None])
    pieces.append(xy[-1:])
    return np.vstack(pieces)


def _parts(geom) -> list[LineString]:
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return [part for part in geom.geoms if part.length > 0]
    return []


def _ribbon(line: LineString, radius: float, step: float, rect) -> np.ndarray:
    xy = densify_xy(np.asarray(line.coords), step)
    if len(xy) < 2 or radius <= 0:
        return np.zeros((0, 2))
    tangent = np.gradient(xy, axis=0)
    length = np.hypot(tangent[:, 0], tangent[:, 1])
    length[length == 0] = 1.0
    nx = -tangent[:, 1] / length
    ny = tangent[:, 0] / length
    offsets = np.linspace(-radius, radius, int(round(2 * radius / step)) + 1)
    clouds = [
        np.column_stack([xy[:, 0] + nx * offset, xy[:, 1] + ny * offset])
        for offset in offsets
    ]
    points = np.vstack(clouds)
    mask = contains_xy(rect.buffer(0.5), points[:, 0], points[:, 1])
    return points[mask]


def _grid(geom, step: float, rect) -> np.ndarray:
    if geom.is_empty:
        return np.zeros((0, 2))
    area = geom.intersection(rect)
    if area.is_empty:
        return np.zeros((0, 2))
    minx, miny, maxx, maxy = area.bounds
    xs = np.arange(minx, maxx + step * 0.5, step)
    ys = np.arange(miny, maxy + step * 0.5, step)
    if xs.size == 0 or ys.size == 0:
        return np.zeros((0, 2))
    grid_x, grid_y = np.meshgrid(xs, ys)
    flat_x = grid_x.ravel()
    flat_y = grid_y.ravel()
    mask = contains_xy(area, flat_x, flat_y)
    return np.column_stack([flat_x[mask], flat_y[mask]])


def _boundary_samples(geom, step: float, rect) -> np.ndarray:
    if geom.is_empty:
        return np.zeros((0, 2))
    boundary = geom.boundary
    if boundary.is_empty or boundary.length == 0:
        return np.zeros((0, 2))
    count = max(8, int(boundary.length / max(step, 0.5)))
    distances = np.linspace(0, boundary.length, count, endpoint=False)
    coords = np.array([boundary.interpolate(distance).coords[0] for distance in distances])
    mask = contains_xy(rect.buffer(0.5), coords[:, 0], coords[:, 1])
    return coords[mask]


def reference_local(segments: list, reference: dict, origin=None) -> dict:
    lines = lines_from_xy(segments_to_xy(segments))
    main = lines[0]
    if origin is None:
        midpoint = main.interpolate(main.length / 2.0)
        origin_x, origin_y = float(midpoint.x), float(midpoint.y)
    else:
        origin_x, origin_y = float(origin[0]), float(origin[1])
    xs, ys = to_5514([float(reference["lon"])], [float(reference["lat"])])
    xs = np.atleast_1d(np.asarray(xs, dtype=np.float64))
    ys = np.atleast_1d(np.asarray(ys, dtype=np.float64))
    return {
        "x": float(xs[0] - origin_x),
        "y": float(ys[0] - origin_y),
        "z": 0.2,
        "rotation": -math.radians(float(reference.get("rotation") or 0)),
        "width": float(reference["width_m"]) * float(reference.get("scale") or 1),
        "opacity": float(0.6 if reference.get("opacity") is None else reference.get("opacity")),
    }


BRUSH_TARGETS = ("1", "2", "3", "4", "corridor")


def brush_target(stamp: dict) -> str:
    value = stamp.get("target")
    if value in (None, "", 1, "1"):
        return "1"
    text = str(value).strip()
    return text if text in BRUSH_TARGETS else "1"


def _empty(geom) -> bool:
    return geom is None or geom.is_empty


def _stamps_geometry(stamps: list[dict]):
    if not stamps:
        return None
    lons = [float(stamp["lon"]) for stamp in stamps]
    lats = [float(stamp["lat"]) for stamp in stamps]
    xs, ys = to_5514(lons, lats)
    xs = np.atleast_1d(np.asarray(xs, dtype=np.float64))
    ys = np.atleast_1d(np.asarray(ys, dtype=np.float64))
    area = None
    for x_coord, y_coord, stamp in zip(xs, ys, stamps):
        radius_m = float(stamp["radius_m"])
        if radius_m <= 0:
            continue
        circle = Point(float(x_coord), float(y_coord)).buffer(radius_m)
        if stamp.get("erase"):
            if area is not None:
                area = area.difference(circle)
                if area.is_empty:
                    area = None
        else:
            area = circle if area is None else area.union(circle)
    return None if _empty(area) else area


def brush_geometries(stamps: list[dict]) -> dict:
    grouped = {key: [] for key in BRUSH_TARGETS}
    for stamp in stamps or []:
        grouped[brush_target(stamp)].append(stamp)
    result = {}
    for key, items in grouped.items():
        geom = _stamps_geometry(items)
        if geom is not None:
            result[key] = geom
    return result


def zone1_brush_geometry(stamps: list[dict]):
    return brush_geometries(stamps).get("1")


def zone_brush_extras(stamps: list[dict]) -> dict[int, object]:
    return {int(key): geom for key, geom in brush_geometries(stamps).items() if key in {"1", "2", "3", "4"}}


def corridor_brush_geometry(stamps: list[dict]):
    return brush_geometries(stamps).get("corridor")


def brush_detail_geometry(stamps: list[dict]):
    geoms = list(brush_geometries(stamps).values())
    if not geoms:
        return None
    area = unary_union(geoms)
    return None if _empty(area) else area


def _claimed_extras(zones: list[Zone], extras: dict):
    claimed = None
    exclusive: dict[int, object] = {}
    for zone in zones:
        extra = extras.get(zone.id)
        if _empty(extra):
            exclusive[zone.id] = None
            continue
        piece = extra if claimed is None else extra.difference(claimed)
        exclusive[zone.id] = None if _empty(piece) else piece
        claimed = extra if claimed is None else claimed.union(extra)
    return (None if _empty(claimed) else claimed), exclusive


def _outside(points: np.ndarray, geom) -> np.ndarray:
    if _empty(geom) or len(points) == 0:
        return points
    return points[~contains_xy(geom, points[:, 0], points[:, 1])]


def _extra_points(geom, step: float, rect) -> np.ndarray:
    if _empty(geom):
        return np.zeros((0, 2))
    clipped = geom.intersection(rect)
    if clipped.is_empty:
        return np.zeros((0, 2))
    parts = [_grid(clipped, step, rect), _boundary_samples(clipped, step, rect)]
    parts = [part for part in parts if len(part)]
    points = np.vstack(parts) if parts else np.zeros((0, 2))
    rep = clipped.representative_point()
    seed = np.array([[float(rep.x), float(rep.y)]])
    if len(points) == 0:
        return seed
    return np.vstack([points, seed])


def _others(claimed, own):
    if _empty(claimed):
        return None
    if _empty(own):
        return claimed
    leftover = claimed.difference(own)
    return None if leftover.is_empty else leftover


def generate_zone_points(lines_xy: list[np.ndarray], zones: list[Zone], terrain_buffer_m: float, zone1_extra=None, extras=None):
    lines = lines_from_xy(lines_xy)
    network = united_line(lines)
    rect = terrain_rectangle(lines, terrain_buffer_m)
    extras_by_zone: dict[int, object] = {}
    if extras:
        for key, geom in extras.items():
            try:
                zone_id = int(key)
            except (TypeError, ValueError):
                continue
            if not _empty(geom):
                extras_by_zone[zone_id] = geom
    if 1 not in extras_by_zone and not _empty(zone1_extra):
        extras_by_zone[1] = zone1_extra
    claimed, exclusive = _claimed_extras(zones, extras_by_zone)
    chunks: list[np.ndarray] = []
    steps: list[np.ndarray] = []
    zone_ids: list[np.ndarray] = []
    previous = 0.0
    for index, zone in enumerate(zones):
        own = exclusive.get(zone.id)
        others = _others(claimed, own)
        if index == 0 and zone.radius_m:
            area = rect.intersection(network.buffer(zone.radius_m))
            if others is not None:
                area = area.difference(others)
            if own is not None:
                area = rect.intersection(area.union(own))
            parts = [_grid(area, zone.step_m, rect)]
            for line in _parts(network):
                parts.append(_outside(_ribbon(line, zone.radius_m, zone.step_m, rect), others))
                parts.append(_outside(_boundary_samples(line.buffer(zone.radius_m), zone.step_m, rect), others))
            if own is not None:
                parts.append(_extra_points(own, zone.step_m, rect))
            parts = [part for part in parts if len(part)]
            points = np.vstack(parts) if parts else np.zeros((0, 2))
        else:
            outer = rect if zone.radius_m is None else rect.intersection(network.buffer(zone.radius_m))
            inner = network.buffer(previous) if previous > 0 else None
            ring = outer.difference(inner) if inner is not None else outer
            if others is not None:
                ring = ring.difference(others)
            if own is not None:
                ring = ring.union(own.intersection(rect))
            points = _grid(ring, zone.step_m, rect)
            edge_geom = rect if zone.radius_m is None else network.buffer(zone.radius_m)
            edge = _outside(_boundary_samples(edge_geom.intersection(rect), zone.step_m, rect), others)
            if own is not None:
                extra_pts = _extra_points(own, zone.step_m, rect)
                points = np.vstack([points, extra_pts]) if len(extra_pts) else points
            points = np.vstack([points, edge]) if len(edge) else points
        if len(points):
            chunks.append(points)
            steps.append(np.full(len(points), zone.step_m))
            zone_ids.append(np.full(len(points), zone.id))
        if zone.radius_m is not None:
            previous = zone.radius_m
    if not chunks:
        raise ValueError("V zónách nevznikl žádný bod. Zvětšete poloměr nebo hranici terénu.")
    xy = np.vstack(chunks)
    step_arr = np.concatenate(steps)
    zone_arr = np.concatenate(zone_ids).astype(np.int32)
    xy, step_arr, zone_arr = _dedupe(xy, step_arr, zone_arr)
    return xy, step_arr, zone_arr, rect, network


def _dedupe(xy: np.ndarray, steps: np.ndarray, zones: np.ndarray, tol: float = 0.05):
    best: dict[tuple[int, int], tuple[float, float, float, int]] = {}
    for (x_coord, y_coord), step, zone in zip(xy, steps, zones):
        key = (int(round(x_coord / tol)), int(round(y_coord / tol)))
        current = best.get(key)
        if current is None or step < current[2]:
            best[key] = (float(x_coord), float(y_coord), float(step), int(zone))
    values = np.array(list(best.values()), dtype=np.float64)
    return values[:, :2], values[:, 2], values[:, 3].astype(np.int32)


def point_along(line: LineString, distance: float) -> Point:
    return line.interpolate(float(np.clip(distance, 0, line.length)))
