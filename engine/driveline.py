from __future__ import annotations

import numpy as np

from blender_lidartool.engine.crsutil import segments_to_xy, xy_to_segment
from blender_lidartool.engine.geometry import densify_xy


def sample_driveline(segments: list, step_m: float, sampler, origin=None) -> dict:
    if step_m <= 0:
        raise ValueError("Krok osy musí být větší než nula.")
    lines = segments_to_xy(segments)
    rows = []
    gpx_segments = []
    elevations = []
    local_segments = []
    for index, xy in enumerate(lines):
        dense = densify_xy(xy, step_m)
        heights = np.asarray(sampler.sample(dense[:, 0], dense[:, 1]), dtype=np.float64)
        segment = xy_to_segment(dense)
        gpx_segments.append(segment)
        elevations.append([None if not np.isfinite(value) else float(value) for value in heights])
        local = np.full((len(dense), 3), np.nan, dtype=np.float64)
        travelled = 0.0
        previous = dense[0]
        for point_index, (point, height, latlon) in enumerate(zip(dense, heights, segment)):
            travelled += float(np.hypot(*(point - previous)))
            previous = point
            z_value = "" if not np.isfinite(height) else f"{height:.3f}"
            if origin is not None and np.isfinite(height):
                local[point_index] = (
                    point[0] - origin[0],
                    point[1] - origin[1],
                    height - origin[2],
                )
                local_text = (
                    f"{local[point_index, 0]:.3f}",
                    f"{local[point_index, 1]:.3f}",
                    f"{local[point_index, 2]:.3f}",
                )
            else:
                local_text = ("", "", "")
            rows.append(
                f"{travelled:.3f},{index},{point[0]:.3f},{point[1]:.3f},{z_value},{local_text[0]},{local_text[1]},{local_text[2]},{latlon['lat']:.7f},{latlon['lon']:.7f}"
            )
        local_segments.append(local)
    return {
        "rows": rows,
        "gpx_segments": gpx_segments,
        "elevations": elevations,
        "local": local_segments,
    }


def finite_lines(local_segments: list[np.ndarray]) -> list[np.ndarray]:
    lines = []
    for segment in local_segments:
        segment = np.asarray(segment, dtype=np.float64)
        if segment.ndim != 2 or segment.shape[1] != 3:
            continue
        current = []
        finite = np.isfinite(segment).all(axis=1)
        for point, ok in zip(segment, finite):
            if ok:
                current.append(point)
            elif len(current) >= 2:
                lines.append(np.asarray(current, dtype=np.float32))
                current = []
            else:
                current = []
        if len(current) >= 2:
            lines.append(np.asarray(current, dtype=np.float32))
    return lines
