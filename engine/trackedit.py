from __future__ import annotations

import numpy as np
from shapely.geometry import LineString

from blender_lidartool.engine.crsutil import segments_to_xy, xy_to_segment
from blender_lidartool.engine.mesh import length_of


def simplify_segments(segments: list, tolerance_m: float = 8.0) -> list:
    simplified = []
    for xy in segments_to_xy(segments):
        line = LineString(xy).simplify(tolerance_m, preserve_topology=True)
        simplified.append(xy_to_segment(np.asarray(line.coords)))
    if not simplified:
        raise ValueError("Trať musí mít alespoň dva body.")
    return simplified


def track_stats(segments: list) -> dict:
    points = sum(len(segment) for segment in segments)
    length = length_of(segments_to_xy(segments)) if segments else 0.0
    return {"points": int(points), "length_m": float(length)}
