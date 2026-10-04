from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import gpxpy
import gpxpy.gpx


def write_gpx(path: Path, segments: list[list[dict]], name: str, elevations: list[list[float | None]] | None = None) -> None:
    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack(name=name)
    gpx.tracks.append(track)
    for index, segment in enumerate(segments):
        part = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(part)
        heights = elevations[index] if elevations and index < len(elevations) else None
        for point_index, point in enumerate(segment):
            elevation = None
            if heights and point_index < len(heights):
                elevation = heights[point_index]
            part.points.append(
                gpxpy.gpx.GPXTrackPoint(point["lat"], point["lon"], elevation=elevation)
            )
    path.write_text(gpx.to_xml(), encoding="utf-8")


def parse_track_file(filename: str, raw: bytes) -> list[list[dict]]:
    lower = filename.lower()
    if lower.endswith(".kmz"):
        return _parse_kmz(raw)
    text = raw.decode("utf-8", errors="replace")
    if lower.endswith(".kml") or "<kml" in text[:500].lower():
        return _parse_kml(text)
    return parse_gpx(text)


def parse_gpx(text: str) -> list[list[dict]]:
    gpx = gpxpy.parse(text)
    segments: list[list[dict]] = []
    sources = list(gpx.tracks)
    for track in sources:
        for segment in track.segments:
            points = [{"lat": point.latitude, "lon": point.longitude} for point in segment.points]
            if len(points) >= 2:
                segments.append(points)
    for route in gpx.routes:
        points = [{"lat": point.latitude, "lon": point.longitude} for point in route.points]
        if len(points) >= 2:
            segments.append(points)
    if not segments:
        raise ValueError("V souboru není žádná linie trati.")
    return segments


def _parse_kmz(raw: bytes) -> list[list[dict]]:
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
        if not names:
            raise ValueError("KMZ neobsahuje KML.")
        text = archive.read(names[0]).decode("utf-8", errors="replace")
    return _parse_kml(text)


def _parse_kml(text: str) -> list[list[dict]]:
    cleaned = text
    for token in ("xmlns", "xsi"):
        cleaned = cleaned.replace(token, token)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("KML se nepodařilo přečíst.") from exc
    segments = []
    for node in root.iter():
        if _local(node.tag) != "coordinates" or not node.text:
            continue
        points = []
        for token in node.text.replace("\n", " ").split():
            parts = token.split(",")
            if len(parts) < 2:
                continue
            lon, lat = float(parts[0]), float(parts[1])
            points.append({"lat": lat, "lon": lon})
        if len(points) >= 2:
            segments.append(points)
    if not segments:
        raise ValueError("KML neobsahuje souřadnice trati.")
    return segments


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
