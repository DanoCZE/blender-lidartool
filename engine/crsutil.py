from __future__ import annotations

import numpy as np
from pyproj import Transformer

_TO_5514 = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
_TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
_TO_3857 = Transformer.from_crs("EPSG:5514", "EPSG:3857", always_xy=True)


def to_5514(lon, lat):
    return _TO_5514.transform(lon, lat)


def to_wgs(x, y):
    return _TO_WGS.transform(x, y)


def to_3857(x, y):
    return _TO_3857.transform(x, y)


def segments_to_xy(segments: list[list[dict]]) -> list[np.ndarray]:
    lines = []
    for segment in segments:
        if len(segment) < 2:
            continue
        lons = np.array([point["lon"] for point in segment], dtype=np.float64)
        lats = np.array([point["lat"] for point in segment], dtype=np.float64)
        if not np.isfinite(lons).all() or not np.isfinite(lats).all():
            raise ValueError("Souřadnice trati obsahují neplatné hodnoty.")
        xs, ys = to_5514(lons, lats)
        lines.append(np.column_stack([np.asarray(xs), np.asarray(ys)]))
    if not lines:
        raise ValueError("Trať musí mít alespoň dva body.")
    return lines


def xy_to_segment(xy: np.ndarray) -> list[dict]:
    lons, lats = to_wgs(xy[:, 0], xy[:, 1])
    lons = np.atleast_1d(lons)
    lats = np.atleast_1d(lats)
    return [{"lat": float(lat), "lon": float(lon)} for lat, lon in zip(lats, lons)]
