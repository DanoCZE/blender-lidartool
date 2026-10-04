from __future__ import annotations

import numpy as np
import requests
from pyproj import Geod

from blender_lidartool.engine.crsutil import segments_to_xy, xy_to_segment
from blender_lidartool.engine.geometry import densify_xy
from blender_lidartool.engine.http import session
from blender_lidartool.engine.trackedit import simplify_segments

GEOD = Geod(ellps="WGS84")
PROFILES = ("driving", "cycling", "walking")
FOSSGIS_ROUTE = {
    "driving": "https://routing.openstreetmap.de/routed-car/route/v1/driving",
    "cycling": "https://routing.openstreetmap.de/routed-bike/route/v1/cycling",
    "walking": "https://routing.openstreetmap.de/routed-foot/route/v1/walking",
}
CHUNK_SIZE = 2
MAX_ANCHORS = 24
SNAP_STEP_M = 8.0
TIMEOUT = (6, 12)
DETOUR_RATIO = 2.8
DETOUR_EXTRA_M = 250.0


def snap_track(points: list[dict], base_url: str, profile: str) -> list[dict]:
    if profile not in PROFILES:
        raise ValueError("Neznámý profil přichycení.")
    if len(points) < 2:
        raise ValueError("Na přichycení jsou potřeba alespoň dva body.")
    endpoints = _route_endpoints(base_url, profile)
    anchors = _anchors(points)
    client = session()
    snapped: list[dict] = []
    try:
        for start, end in zip(anchors, anchors[1:]):
            geometry = _route_chunk_any(client, endpoints, [start, end])
            if _is_detour(start, end, geometry):
                geometry = [start, end]
            if snapped and geometry:
                geometry = _drop_overlap(snapped[-1], geometry)
            snapped.extend(geometry)
    except requests.RequestException as exc:
        raise ValueError("Server pro přichycení na silnice neodpověděl. Zkuste to za chvíli.") from exc
    if len(snapped) < 2:
        raise ValueError("Přichycení nenašlo silnici. Zkuste profil chůze, nebo trať upravte ručně.")
    return _refine_snapped(snapped)


def _route_endpoint(base_url: str, profile: str) -> str:
    return _route_endpoints(base_url, profile)[0]


def _route_endpoints(base_url: str, profile: str) -> list[str]:
    base = (base_url or "").rstrip("/")
    if base and "project-osrm.org" not in base:
        return [f"{base}/route/v1/{profile}"]
    return [
        FOSSGIS_ROUTE[profile],
        f"https://router.project-osrm.org/route/v1/{profile}",
    ]


def _anchors(points: list[dict], limit: int = MAX_ANCHORS) -> list[dict]:
    if len(points) <= limit:
        return points
    simplified = simplify_segments([points], tolerance_m=16.0)[0]
    if len(simplified) <= limit:
        simplified[0] = points[0]
        simplified[-1] = points[-1]
        return simplified
    step = (len(simplified) - 1) / (limit - 1)
    picked = []
    for index in range(limit):
        point = simplified[min(len(simplified) - 1, int(round(index * step)))]
        if not picked or point != picked[-1]:
            picked.append(point)
    picked[0] = points[0]
    picked[-1] = points[-1]
    return picked


def _refine_snapped(points: list[dict], step_m: float = SNAP_STEP_M) -> list[dict]:
    cleaned = [points[0]]
    for point in points[1:]:
        if _meters(cleaned[-1], point) >= 0.4:
            cleaned.append(point)
    if len(cleaned) < 2:
        return points
    xy = segments_to_xy([cleaned])[0]
    return xy_to_segment(densify_xy(xy, step_m))


def _route_chunk_any(client, endpoints: list[str], points: list[dict]) -> list[dict]:
    last_error: Exception | None = None
    for endpoint in endpoints:
        try:
            return _route_chunk(client, endpoint, points)
        except requests.RequestException as exc:
            last_error = exc
    raise requests.RequestException("Žádný router na silnice neodpověděl.") from last_error


def _route_chunk(client, endpoint: str, points: list[dict]) -> list[dict]:
    coords = ";".join(f"{point['lon']},{point['lat']}" for point in points)
    response = client.get(
        f"{endpoint}/{coords}",
        params={
            "overview": "full",
            "geometries": "geojson",
            "alternatives": "false",
            "steps": "false",
            "continue_straight": "true",
            "generate_hints": "false",
        },
        timeout=TIMEOUT,
    )
    if response.status_code >= 500:
        response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise requests.RequestException("Router vrátil nečitelnou odpověď.") from exc
    if payload.get("code") != "Ok" or not payload.get("routes"):
        code = str(payload.get("code") or "")
        if code == "NoSegment":
            raise ValueError("U některého bodu router nenašel silnici. Posuňte ho blíž k cestě, nebo zkuste profil chůze.")
        raise ValueError("Přichycení nenašlo silnici. Zkuste profil chůze, nebo trať upravte ručně.")
    geometry = [
        {"lat": float(lat), "lon": float(lon)}
        for lon, lat in payload["routes"][0]["geometry"]["coordinates"]
    ]
    if len(geometry) < 2:
        raise ValueError("Přichycení nenašlo silnici. Zkuste profil chůze, nebo trať upravte ručně.")
    return geometry


def extend_track(points: list[dict], base_url: str, profile: str, distance_m: float) -> list[dict]:
    if len(points) < 2:
        raise ValueError("Trať musí mít alespoň dva body.")
    if distance_m <= 0:
        raise ValueError("Délka prodloužení musí být větší než nula.")
    endpoints = _route_endpoints(base_url, profile)
    client = session()
    start = _offset(points[0], points[1], distance_m, backward=True)
    end = _offset(points[-1], points[-2], distance_m, backward=True)
    prefix = _route_chunk_any(client, endpoints, [start, points[0]])
    suffix = _route_chunk_any(client, endpoints, [points[-1], end])
    return _trim_join(_trim_join(prefix, points), suffix)


def _trim_join(left: list[dict], right: list[dict]) -> list[dict]:
    if not left:
        return right
    if not right:
        return left
    if _meters(left[-1], right[0]) < 8:
        return left[:-1] + right
    return left + right


def _chunks(points: list[dict], size: int, overlap: int) -> list[list[dict]]:
    if len(points) <= size:
        return [points]
    chunks = []
    start = 0
    while start < len(points) - 1:
        end = min(len(points), start + size)
        chunks.append(points[start:end])
        if end == len(points):
            break
        start = end - overlap
    return chunks


def _offset(origin: dict, other: dict, distance_m: float, backward: bool) -> dict:
    azimuth, _, _ = GEOD.inv(origin["lon"], origin["lat"], other["lon"], other["lat"])
    if backward:
        azimuth = (azimuth + 180.0) % 360.0
    lon, lat, _ = GEOD.fwd(origin["lon"], origin["lat"], azimuth, distance_m)
    return {"lat": float(lat), "lon": float(lon)}


def _drop_overlap(anchor: dict, points: list[dict]) -> list[dict]:
    if not points:
        return points
    distances = [_meters(anchor, point) for point in points[:12]]
    cut = int(np.argmin(distances))
    return points[cut + 1 :]


def _is_detour(start: dict, end: dict, geometry: list[dict]) -> bool:
    if len(geometry) < 2:
        return True
    direct = _meters(start, end)
    routed = sum(_meters(geometry[index], geometry[index + 1]) for index in range(len(geometry) - 1))
    return routed > direct * DETOUR_RATIO + DETOUR_EXTRA_M


def _meters(a: dict, b: dict) -> float:
    _, _, dist = GEOD.inv(a["lon"], a["lat"], b["lon"], b["lat"])
    return float(dist)
