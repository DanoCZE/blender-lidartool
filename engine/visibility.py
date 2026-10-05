from __future__ import annotations

import numpy as np


def viewshed_mask(
    elev: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    eyes: np.ndarray,
    n_bins: int = 360,
    radius_m: float | None = None,
) -> np.ndarray:
    elev = np.asarray(elev, dtype=np.float64)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    height, width = elev.shape
    visible = np.zeros((height, width), dtype=bool)
    finite = np.isfinite(elev)
    x_step = float(xs[0, 1] - xs[0, 0]) if width > 1 else 1.0
    y_step = float(ys[1, 0] - ys[0, 0]) if height > 1 else 1.0
    if x_step == 0 or y_step == 0:
        return visible
    pixel = max(min(abs(x_step), abs(y_step)), 1e-6)
    x0 = float(xs[0, 0])
    y0 = float(ys[0, 0])
    span = float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min()))
    max_radius = span if not radius_m or radius_m <= 0 else min(span, float(radius_m))
    ray_count = int(np.clip(max(n_bins, 2 * np.pi * max_radius / pixel), 180, 1440))
    angles = np.linspace(-np.pi, np.pi, ray_count, endpoint=False)
    steps = np.arange(pixel, max_radius + pixel, pixel)
    if steps.size == 0:
        return visible
    for eye_x, eye_y, eye_z in np.asarray(eyes, dtype=np.float64):
        ray_x = eye_x + np.cos(angles)[:, None] * steps[None, :]
        ray_y = eye_y + np.sin(angles)[:, None] * steps[None, :]
        cols = np.rint((ray_x - x0) / x_step).astype(np.int64)
        rows = np.rint((ray_y - y0) / y_step).astype(np.int64)
        inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
        ground = np.full(ray_x.shape, np.nan, dtype=np.float64)
        ground[inside] = elev[rows[inside], cols[inside]]
        slope = np.full(ground.shape, -np.inf, dtype=np.float64)
        valid = inside & np.isfinite(ground)
        slope[valid] = (ground[valid] - eye_z) / np.broadcast_to(steps, slope.shape)[valid]
        running = np.maximum.accumulate(np.where(np.isfinite(slope), slope, -np.inf), axis=1)
        previous = np.full_like(slope, -np.inf)
        previous[:, 1:] = running[:, :-1]
        hit = valid & (slope + 1e-6 >= previous)
        visible[rows[hit], cols[hit]] = True
        eye_col = int(np.rint((eye_x - x0) / x_step))
        eye_row = int(np.rint((eye_y - y0) / y_step))
        if 0 <= eye_row < height and 0 <= eye_col < width and finite[eye_row, eye_col]:
            visible[eye_row, eye_col] = True
    visible &= finite
    return visible


def bake_buildings(elev: np.ndarray, xs: np.ndarray, ys: np.ndarray, footprints: list, clearance_m: float = 1.5) -> int:
    """Zvedne buňky půdorysu na výšku střechy. footprints jsou (polygon, výška)."""
    from shapely import contains_xy

    baked = 0
    for polygon, roof in footprints:
        if polygon is None or polygon.is_empty or not np.isfinite(roof):
            continue
        minx, miny, maxx, maxy = polygon.bounds
        candidate = (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
        if not candidate.any():
            continue
        inside = contains_xy(polygon, xs[candidate], ys[candidate])
        if not inside.any():
            continue
        cells = np.zeros(xs.shape, dtype=bool)
        chosen = np.flatnonzero(candidate)
        cells.ravel()[chosen[inside]] = True
        ground = elev[cells]
        blocking = np.isfinite(ground) & (float(roof) - ground >= clearance_m)
        if not blocking.any():
            continue
        elev[cells] = np.where(blocking, np.maximum(ground, float(roof)), ground)
        baked += 1
    return baked


def downsample(array: np.ndarray, transform, max_cells: int = 350_000):
    height, width = array.shape
    if height * width <= max_cells:
        return array, transform
    from rasterio.transform import Affine

    scale = int(np.ceil(np.sqrt((height * width) / max_cells)))
    reduced = array[::scale, ::scale]
    return reduced, transform * Affine.scale(scale, scale)
