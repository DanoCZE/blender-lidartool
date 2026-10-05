from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling as WarpResampling
from rasterio.warp import calculate_default_transform, reproject
from shapely.geometry import box

from blender_lidartool.engine.crsutil import segments_to_xy, to_5514, to_wgs
from blender_lidartool.engine.cuzk import DMR_URL, ELEVATION_URLS, SERVICE_BOUNDS, export_image_params
from blender_lidartool.engine.geometry import (
    brush_detail_geometry,
    densify_xy,
    iter_tiles,
    lines_from_xy,
    road_corridor,
    snap_bounds,
    terrain_rectangle,
    united_line,
)
from blender_lidartool.engine.http import session
from blender_lidartool.engine.rasterutil import bounds_from_transform, grid_centers, hillshade
from blender_lidartool.engine.visibility import downsample, viewshed_mask


def download_elevation(project_dir: Path, source: str, segments: list, project: dict, progress) -> dict:
    if source not in ELEVATION_URLS:
        raise ValueError("Neznámý zdroj výšek.")
    lines_xy = segments_to_xy(segments)
    lines = lines_from_xy(lines_xy)
    network = united_line(lines)
    if not _intersects_service(network):
        raise ValueError("Trať leží mimo pokrytí výškových dat ČÚZK (DMR 5G / DMP 1G).")
    rect = terrain_rectangle(lines, float(project["terrain_buffer_m"])).intersection(_service_box())
    native = float(project.get("native_pixel_m") or 2)
    coarse = float(project.get("coarse_pixel_m") or 20)
    road_only = bool(project.get("road_only", True))
    dem = project_dir / "DEM"
    dem.mkdir(parents=True, exist_ok=True)
    for name in (f"{source}_fine.tif", f"{source}_coarse.tif"):
        target = dem / name
        if target.exists():
            target.unlink()
    tiles = []
    extra = brush_detail_geometry(project.get("zone1_brush") or [])
    if road_only:
        tiles.extend(_tiles_along(network, float(project["road_buffer_m"]), native, rect))
        if extra is not None:
            painted = extra.buffer(native).intersection(rect)
            if not painted.is_empty:
                tiles.extend((tile + ("fine",)) for tile in _tiles_for_geom(painted, native))
        tiles.extend((tile + ("coarse",)) for tile in _tiles_for_geom(rect, coarse))
    else:
        tiles.extend((tile + ("fine",)) for tile in _tiles_for_geom(rect, native))
    if road_only:
        fine_tiles = [tile for tile in tiles if tile[-1] == "fine"]
        coarse_tiles = [tile for tile in tiles if tile[-1] == "coarse"]
    else:
        fine_tiles = tiles
        coarse_tiles = []
    _guard_size(fine_tiles, "podrobné")
    _guard_size(coarse_tiles, "hrubé")
    temp = project_dir / "TEMP" / f"{source}_tiles"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True, exist_ok=True)
    progress(2, f"Stahuji {source.upper()}: {len(tiles)} dlaždic")
    paths = _download_tiles(ELEVATION_URLS[source], tiles, temp, progress)
    fine_paths = [path for path, kind in paths if kind == "fine"]
    coarse_paths = [path for path, kind in paths if kind == "coarse"]
    if fine_paths:
        _mosaic(fine_paths, dem / f"{source}_fine.tif")
    if coarse_paths:
        _mosaic(coarse_paths, dem / f"{source}_coarse.tif")
    shutil.rmtree(temp, ignore_errors=True)
    label = "DMR 5G" if source == "dmr5g" else "DMP 1G"
    return {"message": f"{label} je stažený. Podrobných dlaždic: {len(fine_paths)}, hrubých: {len(coarse_paths)}."}


def clip_road(project_dir: Path, segments: list, project: dict) -> dict:
    source = project.get("mesh_source") or "dmr5g"
    path = _best_raster(project_dir, source)
    lines = lines_from_xy(segments_to_xy(segments))
    corridor = road_corridor(united_line(lines), float(project["road_buffer_m"]))
    extra = brush_detail_geometry(project.get("zone1_brush") or [])
    if extra is not None:
        corridor = corridor.union(extra)
    dest = project_dir / "DEM" / "road_zone.tif"
    from rasterio.mask import mask

    with rasterio.open(path) as dataset:
        clipped, transform = mask(dataset, [corridor], crop=True, nodata=-9999, filled=True)
        profile = dataset.profile.copy()
        profile.update(
            height=clipped.shape[1],
            width=clipped.shape[2],
            transform=transform,
            nodata=-9999,
            compress="lzw",
            dtype="float32",
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(dest, "w", **profile) as target:
            target.write(clipped.astype(np.float32))
    return {"message": "Koridor silnice je oříznutý do DEM/road_zone.tif.", "files": ["road_zone.tif"]}


def analyze(project_dir: Path) -> dict:
    dem = project_dir / "DEM"
    files = sorted(dem.glob("*.tif")) if dem.exists() else []
    if not files:
        raise ValueError("Není co analyzovat. Nejdřív stáhněte nebo importujte výšky.")
    reports = [_raster_stats(path) for path in files]
    return {"message": "Analýza hotová.", "rasters": reports}


def import_elevation(project_dir: Path, filename: str, raw: bytes, progress) -> dict:
    temp = project_dir / "TEMP" / "import" / filename
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(raw)
    dest = project_dir / "DEM" / "imported.tif"
    lower = filename.lower()
    progress(10, "Importuji výšky")
    if lower.endswith((".laz", ".las")):
        _import_pointcloud(temp, dest)
    elif lower.endswith((".tif", ".tiff")):
        _import_geotiff(temp, dest)
    else:
        raise ValueError("Podporovaný import je GeoTIFF, LAS nebo LAZ.")
    progress(100, "Import dokončen")
    return {"message": f"Soubor {filename} je převzatý do DEM/imported.tif."}


def render_visibility(project_dir: Path, segments: list, project: dict, progress) -> dict:
    source = project.get("mesh_source") or "dmr5g"
    path = _coarse_or_fine(project_dir, source)
    progress(10, "Připravuji hrubý reliéf")
    with rasterio.open(path) as dataset:
        array = dataset.read(1).astype(np.float64)
        nodata = dataset.nodata if dataset.nodata is not None else -9999
        array[array == nodata] = np.nan
        array[(array < -500) | (array > 5000)] = np.nan
        array, transform = downsample(array, dataset.transform)
    xs, ys = grid_centers(transform, array.shape[0], array.shape[1])
    lines_xy = segments_to_xy(segments)
    eyes = _eyes(
        lines_xy,
        float(project.get("visibility_sample_m") or 25),
        float(project.get("visibility_eye_m") or 15),
        array,
        transform,
    )
    progress(30, "Počítám viditelnost")
    mask = viewshed_mask(array, xs, ys, eyes)
    image, image_transform = _warp_mask(mask, transform)
    output = project_dir / "OUTPUT"
    output.mkdir(parents=True, exist_ok=True)
    rgba = np.zeros((image.shape[0], image.shape[1], 4), dtype=np.uint8)
    rgba[image > 0, 1] = 190
    rgba[image > 0, 3] = 110
    Image.fromarray(rgba, "RGBA").save(output / "visibility.png")
    left, bottom, right, top = bounds_from_transform(image_transform, image.shape[0], image.shape[1])
    meta = {
        "south": bottom,
        "west": left,
        "north": top,
        "east": right,
    }
    (output / "visibility.json").write_text(__import__("json").dumps(meta), encoding="utf-8")
    progress(100, "Viditelnost je na mapě")
    return {"message": "Analýza viditelnosti je hotová. Zelená plocha ukazuje, co je z tratě vidět."}


VISIBILITY_RADIUS_M = 400.0
VISIBILITY_EYE_M = 1.5
VISIBILITY_SAMPLE_M = 25.0
VISIBILITY_CLEARANCE_M = 1.5


def render_track_visibility(cache: Path, segments: list, features: list, progress) -> str:
    """Zelená plocha kolem tratě. DMR 5G a do něj zapečené vybrané budovy."""
    import json

    from blender_lidartool.engine.buildings import _polygons
    from blender_lidartool.engine.visibility import bake_buildings

    lines_xy = segments_to_xy(segments)
    lines = lines_from_xy(lines_xy)
    network = united_line(lines)
    if network.is_empty or network.length < 1:
        raise ValueError("Viditelnost potřebuje trať aspoň se dvěma body.")
    if not _intersects_service(network):
        raise ValueError("Trať leží mimo pokrytí DMR 5G.")
    corridor = network.buffer(VISIBILITY_RADIUS_M).intersection(_service_box())
    if corridor.is_empty:
        raise ValueError("Kolem tratě není pokrytí DMR 5G.")
    output = cache / "OUTPUT"
    output.mkdir(parents=True, exist_ok=True)
    key = _visibility_key(segments, features)
    cached = _read_visibility_meta(output)
    if cached and cached.get("key") == key and (output / "visibility.png").is_file():
        progress(100, "Viditelnost je na mapě")
        return str(cached.get("message") or "Viditelnost je na mapě.")

    pixel = _visibility_pixel(network.length)
    progress(4, "Stahuji DMR 5G kolem tratě")
    dmr_path = cache / "TEMP" / "visibility_dmr.tif"
    _download_corridor(DMR_URL, network, corridor, pixel, 4000, cache / "TEMP" / "visibility_dmr_tiles", dmr_path, progress, 4, 28)
    with rasterio.open(dmr_path) as dataset:
        array = dataset.read(1).astype(np.float64)
        nodata = dataset.nodata if dataset.nodata is not None else -9999
        array[array == nodata] = np.nan
        array[(array < -500) | (array > 5000)] = np.nan
        ground, transform = downsample(array, dataset.transform, max_cells=300_000)
    xs, ys = grid_centers(transform, ground.shape[0], ground.shape[1])
    footprints = _visibility_footprints(features, corridor, _polygons)
    baked = np.array(ground, copy=True)
    baked_count = 0
    if footprints:
        progress(34, "Stahuji střechy budov")
        surface = _building_surface(cache, footprints, progress)
        roofs = _roof_heights(footprints, surface)
        progress(68, "Peču budovy do DMR 5G")
        baked_count = bake_buildings(baked, xs, ys, roofs, VISIBILITY_CLEARANCE_M)
    progress(74, "Počítám viditelnost")
    eyes = _eyes(lines_xy, VISIBILITY_SAMPLE_M, VISIBILITY_EYE_M, ground, transform)
    mask = viewshed_mask(baked, xs, ys, eyes, radius_m=VISIBILITY_RADIUS_M)
    image, image_transform = _warp_mask(mask, transform)
    rgba = np.zeros((image.shape[0], image.shape[1], 4), dtype=np.uint8)
    rgba[image > 0, 1] = 190
    rgba[image > 0, 3] = 140
    Image.fromarray(rgba, "RGBA").save(output / "visibility.png")
    left, bottom, right, top = bounds_from_transform(image_transform, image.shape[0], image.shape[1])
    if baked_count:
        message = (
            f"Zeleně je z tratě vidět, do {VISIBILITY_RADIUS_M:.0f} m. "
            f"Do DMR 5G je zapečených {baked_count} budov, ty výhled zakrývají."
        )
    else:
        message = (
            f"Zeleně je z tratě vidět, do {VISIBILITY_RADIUS_M:.0f} m. "
            "Žádná vybraná budova v pásu není, výhled zakrývá jen terén."
        )
    meta = {
        "south": float(bottom),
        "west": float(left),
        "north": float(top),
        "east": float(right),
        "key": key,
        "message": message,
        "buildings": baked_count,
    }
    (output / "visibility.json").write_text(json.dumps(meta), encoding="utf-8")
    progress(100, "Viditelnost je na mapě")
    return message


def render_hillshade(project_dir: Path, source: str) -> tuple[Image.Image, dict]:
    path = _coarse_or_fine(project_dir, source)
    with rasterio.open(path) as dataset:
        scale = max(1, int(max(dataset.width, dataset.height) / 800))
        array = dataset.read(1, out_shape=(dataset.height // scale, dataset.width // scale), resampling=Resampling.average)
        transform = dataset.transform * Affine.scale(scale, scale)
        nodata = dataset.nodata if dataset.nodata is not None else -9999
    data = array.astype(np.float64)
    data[data == nodata] = np.nan
    shade = hillshade(data)
    warped, transform = _warp_byte(shade, transform)
    image = Image.fromarray(warped, "L")
    left, bottom, right, top = bounds_from_transform(transform, warped.shape[0], warped.shape[1])
    return image, {"south": bottom, "west": left, "north": top, "east": right}


def source_ready(project_dir: Path, source: str) -> bool:
    names = {path.name for path in elevation_paths(project_dir, source)}
    return f"{source}_fine.tif" in names or f"{source}_coarse.tif" in names or "imported.tif" in names


def fine_covers_brushes(project_dir: Path, source: str, project: dict) -> bool:
    extra = brush_detail_geometry(project.get("zone1_brush") or [])
    if extra is None or extra.is_empty:
        return True
    if not bool(project.get("road_only", True)):
        return True
    path = project_dir / "DEM" / f"{source}_fine.tif"
    if not path.exists():
        return False
    points = _geom_probe_xy(extra)
    if not points:
        return True
    with rasterio.open(path) as dataset:
        nodata = dataset.nodata
        for row in dataset.sample(points):
            value = float(row[0])
            if not np.isfinite(value) or value < -500 or value > 5000:
                return False
            if nodata is not None and value == nodata:
                return False
    return True


def elevation_paths(project_dir: Path, source: str) -> list[Path]:
    dem = project_dir / "DEM"
    ordered = [
        dem / f"{source}_coarse.tif",
        dem / f"{source}_fine.tif",
        dem / "imported.tif",
    ]
    if source == "dmp1g":
        ordered = [dem / "dmr5g_coarse.tif", dem / "dmr5g_fine.tif", *ordered]
    return [path for path in ordered if path.exists()]


def _service_box():
    return box(*SERVICE_BOUNDS)


def _intersects_service(network) -> bool:
    return network.intersects(_service_box())


def _polygon_parts(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [part for part in geom.geoms if not part.is_empty]
    if geom.geom_type == "GeometryCollection":
        parts = []
        for part in geom.geoms:
            parts.extend(_polygon_parts(part))
        return parts
    return [geom]


def _geom_probe_xy(geom) -> list[tuple[float, float]]:
    points = []
    for part in _polygon_parts(geom):
        sample = part.representative_point()
        points.append((float(sample.x), float(sample.y)))
    return points


def _tiles_for_geom(geom, pixel: float, max_px: int = 4000):
    if geom is None or geom.is_empty:
        return []
    tiles = []
    seen = set()
    for part in _polygon_parts(geom):
        minx, miny, maxx, maxy = snap_bounds(*part.bounds, pixel)
        for tile in iter_tiles(minx, miny, maxx, maxy, pixel, max_px=max_px):
            key = tuple(round(value, 2) for value in tile[:4])
            if key in seen:
                continue
            if not box(tile[0], tile[1], tile[2], tile[3]).intersects(part):
                continue
            seen.add(key)
            tiles.append(tile)
    return tiles


def _tiles_along(network, road_buffer_m: float, pixel: float, rect):
    tiles = []
    seen = set()
    parts = [network] if network.geom_type == "LineString" else list(network.geoms)
    for part in parts:
        if part.length <= 0:
            continue
        start = 0.0
        chunk = 1500.0
        while start < part.length:
            end = min(part.length, start + chunk)
            piece = _substring(part, start, end)
            geom = piece.buffer(road_buffer_m).intersection(rect)
            if not geom.is_empty:
                for tile in _tiles_for_geom(geom, pixel):
                    key = tuple(round(value, 2) for value in tile[:4])
                    if key not in seen:
                        seen.add(key)
                        tiles.append((*tile, "fine"))
            if end >= part.length:
                break
            start += max(chunk - 40, pixel)
    return tiles


def _substring(line, start: float, end: float):
    count = max(2, int((end - start) / 10) or 2)
    distances = np.linspace(start, end, count)
    return type(line)([line.interpolate(float(distance)).coords[0] for distance in distances])


def _guard_size(tiles, label: str) -> None:
    pixels = sum(tile[4] * tile[5] for tile in tiles)
    if pixels > 40_000_000:
        raise ValueError(
            f"{label.capitalize()} stažení má zhruba {pixels / 1_000_000:.0f} milionů buněk. "
            "Zapněte koridor silnice nebo zmenšete hranici terénu."
        )


def _download_tiles(url: str, tiles: list, temp: Path, progress) -> list[tuple[Path, str]]:
    if not tiles:
        raise ValueError("Pro zvolenou oblast nevznikla žádná dlaždice.")

    def fetch(index_tile):
        index, tile = index_tile
        minx, miny, maxx, maxy, width, height, kind = tile
        params = export_image_params(minx, miny, maxx, maxy, width, height)
        destination = temp / f"{kind}_{index}.tif"
        last_error = None
        for _attempt in range(3):
            try:
                response = session().get(url, params=params, timeout=180)
                response.raise_for_status()
                _save_elevation(response.content, (minx, miny, maxx, maxy), destination)
                return destination, kind
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Dlaždici se nepodařilo stáhnout: {last_error}")

    saved = []
    workers = min(3, len(tiles))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in enumerate(tiles)]
        for done in as_completed(futures):
            saved.append(done.result())
            progress(5 + 80 * len(saved) / len(tiles), f"Staženo {len(saved)}/{len(tiles)}")
    return saved


def _save_elevation(content: bytes, bounds, dest: Path) -> None:
    if len(content) < 8 or content[:2] not in (b"II", b"MM"):
        snippet = content[:180].decode("utf-8", "replace")
        raise RuntimeError(f"ČÚZK nevrátil TIFF s výškami: {snippet}")
    with MemoryFile(content) as memory:
        with memory.open() as source:
            if source.count < 1 or np.dtype(source.dtypes[0]).itemsize < 2:
                raise RuntimeError("Služba vrátila obrázek místo výšek.")
            data = source.read(1).astype(np.float32)
    if data.ndim != 2:
        raise RuntimeError("Služba vrátila obrázek místo výšek.")
    height, width = data.shape
    data[~np.isfinite(data)] = -9999
    data[(data < -500) | (data > 5000)] = -9999
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:5514",
        "transform": from_bounds(*bounds, width, height),
        "nodata": -9999,
        "compress": "lzw",
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dest, "w", **profile) as dataset:
        dataset.write(data, 1)


def _mosaic(paths: list[Path], dest: Path) -> None:
    datasets = [rasterio.open(path) for path in paths]
    try:
        array, transform = merge(datasets, nodata=-9999, method="first")
        profile = {
            "driver": "GTiff",
            "height": array.shape[1],
            "width": array.shape[2],
            "count": 1,
            "dtype": "float32",
            "crs": "EPSG:5514",
            "transform": transform,
            "nodata": -9999,
            "compress": "lzw",
        }
        if array.shape[1] >= 256 and array.shape[2] >= 256:
            profile.update(tiled=True, blockxsize=256, blockysize=256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(dest, "w", **profile) as dataset:
            dataset.write(array.astype(np.float32))
    finally:
        for dataset in datasets:
            dataset.close()


def _raster_stats(path: Path) -> dict:
    with rasterio.open(path) as dataset:
        minimum = None
        maximum = None
        total = 0.0
        count = 0
        nodata_count = 0
        cells = 0
        nodata = dataset.nodata
        for _, window in dataset.block_windows(1):
            data = dataset.read(1, window=window).astype(np.float64)
            cells += data.size
            valid = np.isfinite(data) & (data > -500) & (data < 5000)
            if nodata is not None:
                valid &= data != nodata
            nodata_count += int((~valid).sum())
            if valid.any():
                values = data[valid]
                low = float(values.min())
                high = float(values.max())
                minimum = low if minimum is None else min(minimum, low)
                maximum = high if maximum is None else max(maximum, high)
                total += float(values.sum())
                count += int(valid.sum())
        left, bottom, right, top = dataset.bounds
        return {
            "name": path.name,
            "pixel_m": abs(float(dataset.transform.a)),
            "width": dataset.width,
            "height": dataset.height,
            "min": minimum,
            "max": maximum,
            "mean": (total / count) if count else None,
            "nodata_ratio": nodata_count / cells if cells else 1,
            "bounds": [left, bottom, right, top],
            "crs": str(dataset.crs),
        }


def _import_geotiff(source: Path, dest: Path) -> None:
    with rasterio.open(source) as dataset:
        if dataset.crs is None:
            raise ValueError("GeoTIFF nemá souřadnicový systém.")
        transform, width, height = calculate_default_transform(
            dataset.crs, "EPSG:5514", dataset.width, dataset.height, *dataset.bounds
        )
        profile = {
            "driver": "GTiff",
            "height": height,
            "width": width,
            "count": 1,
            "dtype": "float32",
            "crs": "EPSG:5514",
            "transform": transform,
            "nodata": -9999,
            "compress": "lzw",
        }
        dest.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(dest, "w", **profile) as target:
            reproject(
                source=rasterio.band(dataset, 1),
                destination=rasterio.band(target, 1),
                resampling=WarpResampling.bilinear,
                src_nodata=dataset.nodata,
                dst_nodata=-9999,
            )


def _import_pointcloud(source: Path, dest: Path) -> None:
    try:
        import laspy
    except ImportError as exc:
        raise ValueError("Pro LAS/LAZ je potřeba balíček laspy.") from exc
    try:
        cloud = laspy.read(source)
    except Exception as exc:
        raise ValueError(
            "Mračno se nepodařilo přečíst. U LAZ může chybět knihovna lazrs; uložte soubor jako LAS."
        ) from exc
    xs = np.asarray(cloud.x, dtype=np.float64)
    ys = np.asarray(cloud.y, dtype=np.float64)
    zs = np.asarray(cloud.z, dtype=np.float64)
    crs = None
    try:
        crs = cloud.header.parse_crs()
    except Exception:
        crs = None
    if crs is not None:
        from pyproj import Transformer

        transformer = Transformer.from_crs(crs, "EPSG:5514", always_xy=True)
        xs, ys = transformer.transform(xs, ys)
    elif np.nanmax(np.abs(np.r_[xs, ys])) < 360:
        xs, ys = to_5514(xs, ys)
    pixel = 2.0
    minx, miny, maxx, maxy = snap_bounds(float(np.min(xs)), float(np.min(ys)), float(np.max(xs)), float(np.max(ys)), pixel)
    width = int(round((maxx - minx) / pixel))
    height = int(round((maxy - miny) / pixel))
    if width * height > 40_000_000:
        raise ValueError("Mračno je na krok 2 m příliš velké.")
    cols = ((xs - minx) / pixel).astype(np.int64)
    rows = ((maxy - ys) / pixel).astype(np.int64)
    valid = (cols >= 0) & (cols < width) & (rows >= 0) & (rows < height) & np.isfinite(zs)
    grid = np.full(height * width, np.inf, dtype=np.float64)
    np.minimum.at(grid, rows[valid] * width + cols[valid], zs[valid])
    array = grid.reshape(height, width).astype(np.float32)
    array[np.isinf(array)] = -9999
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:5514",
        "transform": from_bounds(minx, miny, maxx, maxy, width, height),
        "nodata": -9999,
        "compress": "lzw",
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dest, "w", **profile) as dataset:
        dataset.write(array, 1)


def _best_raster(project_dir: Path, source: str) -> Path:
    for name in (f"{source}_fine.tif", f"{source}_coarse.tif", "imported.tif"):
        path = project_dir / "DEM" / name
        if path.exists():
            return path
    raise ValueError("Není výškový rastr k ořezu.")


def _visibility_pixel(length_m: float) -> float:
    pixel = 5.0
    radius = VISIBILITY_RADIUS_M
    while pixel < 20:
        cells = (length_m + 2 * radius) * (2 * radius) / (pixel * pixel)
        if cells <= 300_000:
            return pixel
        pixel *= 2
    return pixel


def _visibility_key(segments: list, features: list) -> str:
    points = []
    for segment in segments:
        if not isinstance(segment, list):
            continue
        for point in segment:
            if not isinstance(point, dict):
                continue
            points.append(f"{float(point.get('lat') or 0):.5f},{float(point.get('lon') or 0):.5f}")
    ids = []
    for feature in features or []:
        if isinstance(feature, dict):
            ids.append(str(feature.get("id") or ""))
    return "v1|" + ";".join(points) + "#" + ",".join(sorted(ids))


def _read_visibility_meta(output: Path):
    path = output / "visibility.json"
    if not path.is_file():
        return None
    try:
        meta = __import__("json").loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _download_corridor(url: str, network, corridor, pixel: float, max_px: int, folder: Path, dest: Path, progress, start: float, span: float) -> None:
    tiles = [(*tile, "fine") for tile in _tiles_for_geom(corridor, pixel, max_px)]
    if not tiles and getattr(network, "geom_type", "") in ("LineString", "MultiLineString"):
        tiles = _tiles_along(network, VISIBILITY_RADIUS_M, pixel, _service_box())
    if not tiles:
        raise ValueError("Kolem tratě nevznikla žádná dlaždice výšek.")
    _guard_size(tiles, "viditelnost")

    def scaled(pct: float, message: str, event: str | None = None) -> None:
        del event
        progress(start + span * (float(pct) / 100.0), message)

    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    saved = _download_tiles(url, tiles, folder, scaled)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _mosaic([path for path, _kind in saved], dest)
    shutil.rmtree(folder, ignore_errors=True)


def _visibility_footprints(features: list, corridor, polygons_of):
    footprints = []
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        for polygon in polygons_of(feature.get("geometry") or {}):
            if polygon.is_empty or not polygon.intersects(corridor):
                continue
            footprints.append(polygon)
    return footprints


def _building_surface(cache: Path, footprints: list, progress):
    from blender_lidartool.engine.cuzk import DMPOK_URL
    from blender_lidartool.engine.rasterutil import ElevationSampler

    area = sum(polygon.area for polygon in footprints)
    pixel = 2.0 if area > 8_000_000 else 1.0
    region = footprints[0]
    for polygon in footprints[1:]:
        region = region.union(polygon)
    region = region.buffer(20.0).intersection(_service_box())
    if region.is_empty:
        raise ValueError("Vybrané budovy leží mimo pokrytí výškových dat ČÚZK.")
    dest = cache / "TEMP" / "visibility_dmpok.tif"
    _download_corridor(
        DMPOK_URL,
        region,
        region,
        pixel,
        2048,
        cache / "TEMP" / "visibility_dmpok_tiles",
        dest,
        progress,
        34,
        30,
    )
    return ElevationSampler([dest])


def _roof_heights(footprints: list, surface) -> list:
    from shapely import contains_xy

    roofs = []
    for polygon in footprints:
        minx, miny, maxx, maxy = polygon.bounds
        xs = np.arange(minx + 1.0, maxx, 2.0)
        ys = np.arange(miny + 1.0, maxy, 2.0)
        if xs.size == 0 or ys.size == 0:
            xs = np.array([(minx + maxx) / 2.0])
            ys = np.array([(miny + maxy) / 2.0])
        grid_x, grid_y = np.meshgrid(xs, ys)
        flat_x = grid_x.ravel()
        flat_y = grid_y.ravel()
        inner = polygon.buffer(-1.5)
        target = inner if not inner.is_empty and inner.area >= 1 else polygon
        inside = contains_xy(target, flat_x, flat_y)
        if not inside.any():
            continue
        sampled = np.asarray(surface.sample(flat_x[inside], flat_y[inside]), dtype=np.float64).reshape(-1)
        finite = sampled[np.isfinite(sampled)]
        if finite.size == 0:
            continue
        low, high = np.percentile(finite, [10, 90])
        kept = finite[(finite >= low) & (finite <= high)]
        if kept.size == 0:
            kept = finite
        roofs.append((polygon, float(np.median(kept))))
    return roofs


def _coarse_or_fine(project_dir: Path, source: str) -> Path:
    for name in (f"{source}_coarse.tif", f"{source}_fine.tif", "imported.tif", "dmr5g_coarse.tif", "dmr5g_fine.tif"):
        path = project_dir / "DEM" / name
        if path.exists():
            return path
    raise ValueError("Nejdřív stáhněte výškový rastr.")


def _eyes(lines_xy, step: float, eye_height: float, array, transform) -> np.ndarray:
    eyes = []
    for xy in lines_xy:
        dense = densify_xy(xy, step)
        if len(dense) > 500:
            dense = dense[:: max(1, int(np.ceil(len(dense) / 500)))]
        for x_coord, y_coord in dense:
            inverse = ~transform
            col = int(inverse.a * x_coord + inverse.b * y_coord + inverse.c)
            row = int(inverse.d * x_coord + inverse.e * y_coord + inverse.f)
            if 0 <= row < array.shape[0] and 0 <= col < array.shape[1] and np.isfinite(array[row, col]):
                ground = float(array[row, col])
            else:
                continue
            eyes.append((x_coord, y_coord, ground + eye_height))
    if not eyes:
        raise ValueError("Z tratě se nepodařilo odečíst výšku oka. Zkontrolujte, že rastr trať pokrývá.")
    return np.array(eyes, dtype=np.float64)


def _warp_mask(mask: np.ndarray, transform):
    return _warp_array(mask.astype(np.uint8), transform, WarpResampling.nearest)


def _warp_byte(array: np.ndarray, transform):
    return _warp_array(array.astype(np.uint8), transform, WarpResampling.bilinear)


def _warp_array(array: np.ndarray, transform, resampling):
    height, width = array.shape
    left, bottom, right, top = bounds_from_transform(transform, height, width)
    out_transform, out_width, out_height = calculate_default_transform(
        "EPSG:5514", "EPSG:4326", width, height, left, bottom, right, top
    )
    dest = np.zeros((out_height, out_width), dtype=np.uint8)
    reproject(
        source=array,
        destination=dest,
        src_transform=transform,
        src_crs="EPSG:5514",
        dst_transform=out_transform,
        dst_crs="EPSG:4326",
        resampling=resampling,
    )
    return dest, out_transform
