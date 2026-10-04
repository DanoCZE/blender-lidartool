from __future__ import annotations

import numpy as np
from shapely import distance, points
from shapely.geometry import LineString, Point

from blender_lidartool.engine.crsutil import segments_to_xy, to_5514, to_wgs, xy_to_segment
from blender_lidartool.engine.http import session

HIGHWAYS = {
    "driving": {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "service",
        "living_street",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    },
    "cycling": set(),
    "walking": set(),
}
HIGHWAYS["cycling"] = HIGHWAYS["driving"] | {"cycleway", "track", "path"}
HIGHWAYS["walking"] = HIGHWAYS["cycling"] | {"footway", "pedestrian", "steps"}


def fetch_ways(bboxes: list[tuple[float, float, float, float]], profile: str) -> list[list[dict]]:
    if profile not in HIGHWAYS:
        raise ValueError("Neznámý profil křižovatek.")
    pattern = "|".join(sorted(HIGHWAYS[profile]))
    client = session()
    ways = []
    seen = set()
    for south, west, north, east in bboxes:
        query = (
            f'[out:json][timeout:25];'
            f'way["highway"~"^({pattern})$"]({south},{west},{north},{east});'
            "out geom;"
        )
        response = client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=40,
        )
        response.raise_for_status()
        for element in response.json().get("elements", []):
            if element.get("type") != "way" or "geometry" not in element:
                continue
            if element.get("id") in seen:
                continue
            seen.add(element.get("id"))
            ways.append(element["geometry"])
    return ways


def stubs_from_ways(track_xy: np.ndarray, ways_xy: list[np.ndarray], length_m: float, meet_m: float = 28.0):
    if length_m <= 0:
        raise ValueError("Délka odbočky musí být větší než nula.")
    track = LineString(track_xy)
    stubs = []
    used = []
    for way in ways_xy:
        if len(way) < 2:
            continue
        for stub in _stubs_for_way(track, way, length_m, meet_m):
            start = stub[0]
            if any(np.hypot(start[0] - other[0], start[1] - other[1]) < 20 for other in used):
                continue
            used.append(start)
            stubs.append(stub)
            if len(stubs) >= 30:
                return stubs
    return stubs


def _stubs_for_way(track: LineString, way: np.ndarray, length_m: float, meet_m: float):
    sampled = distance(points(way[:, 0], way[:, 1]), track)
    distances = np.asarray(sampled, dtype=np.float64)
    if float(distances.min()) > meet_m or float(np.median(distances)) < 10:
        return []
    index = int(np.argmin(distances))
    results = []
    for step in (1, -1):
        stub = [way[index]]
        travelled = 0.0
        cursor = index
        while 0 <= cursor + step < len(way) and travelled < length_m:
            nxt = way[cursor + step]
            travelled += float(np.hypot(*(nxt - way[cursor])))
            stub.append(nxt)
            cursor += step
        if travelled < 8:
            continue
        end = Point(stub[-1])
        if track.distance(end) < meet_m * 0.75:
            continue
        nearest = track.interpolate(track.project(Point(stub[0])))
        results.append(np.vstack([np.array(nearest.coords[0]), np.array(stub)]))
    return results


def chunk_bboxes(xy: np.ndarray, chunk_m: float, pad_m: float) -> list[tuple[float, float, float, float]]:
    line = LineString(xy)
    bboxes = []
    start = 0.0
    while start < line.length:
        end = min(line.length, start + chunk_m)
        piece = LineString([line.interpolate(distance).coords[0] for distance in np.linspace(start, end, 8)])
        minx, miny, maxx, maxy = piece.bounds
        west_x, south_y = _to_wgs_pair(minx - pad_m, miny - pad_m)
        east_x, north_y = _to_wgs_pair(maxx + pad_m, maxy + pad_m)
        south = min(south_y, north_y)
        north = max(south_y, north_y)
        west = min(west_x, east_x)
        east = max(west_x, east_x)
        bboxes.append((south, west, north, east))
        if end >= line.length:
            break
        start += chunk_m
    return bboxes


def append_junctions(segments: list, length_m: float, profile: str) -> tuple[list, int]:
    lines = segments_to_xy(segments)
    main = lines[0]
    try:
        ways = fetch_ways(chunk_bboxes(main, 4000, 80), profile)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Seznam křižovatek se nepodařilo stáhnout: {exc}") from exc
    ways_xy = []
    for way in ways:
        lons = [point["lon"] for point in way]
        lats = [point["lat"] for point in way]
        xs, ys = to_5514(lons, lats)
        ways_xy.append(np.column_stack([xs, ys]))
    stubs = stubs_from_ways(main, ways_xy, length_m)
    extra = [xy_to_segment(stub) for stub in stubs if len(stub) >= 2]
    return [segments[0], *extra], len(extra)


def _to_wgs_pair(x_coord: float, y_coord: float) -> tuple[float, float]:
    lon, lat = to_wgs([x_coord], [y_coord])
    return float(lon[0]), float(lat[0])
