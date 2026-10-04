from __future__ import annotations

import re

import numpy as np

PAIR_ARRAY = re.compile(
    r"\[(?:\s*\[\s*-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+\s*\]\s*,?){4,}\s*\]",
    re.S,
)
PAIR = re.compile(r"\[\s*(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*\]")
TRKPT = re.compile(
    r"lat=[\"'](-?\d{1,3}\.\d+)[\"'][^>]*lon=[\"'](-?\d{1,3}\.\d+)[\"']",
    re.I,
)


def extract_track(html: str) -> list[dict]:
    candidates: list[list[tuple[float, float]]] = []
    trk = [(float(lat), float(lon)) for lat, lon in TRKPT.findall(html)]
    if len(trk) >= 4:
        candidates.append(trk)
    for match in PAIR_ARRAY.finditer(html):
        pairs = [(float(a), float(b)) for a, b in PAIR.findall(match.group(0))]
        classified = _classify(pairs)
        if classified:
            candidates.append(classified)
    if not candidates:
        raise ValueError(
            "Z stránky Rally-Maps se nepodařilo přečíst trať. Stáhněte GPX a naimportujte soubor."
        )
    best = max(candidates, key=len)
    return [{"lat": lat, "lon": lon} for lat, lon in best]


def _classify(pairs: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    if len(pairs) < 4:
        return None
    first = np.array([item[0] for item in pairs])
    second = np.array([item[1] for item in pairs])
    if _is_lat(first) and _is_lon(second):
        return pairs
    if _is_lon(first) and _is_lat(second):
        return [(b, a) for a, b in pairs]
    return None


def _is_lat(values: np.ndarray) -> bool:
    return bool(np.all((values >= 34) & (values <= 72)))


def _is_lon(values: np.ndarray) -> bool:
    return bool(np.all((values >= -15) & (values <= 42)))
