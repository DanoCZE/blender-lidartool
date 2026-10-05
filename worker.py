from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


_EVENTS: list[str] = []


def write_status(
    path: Path,
    pct: float,
    message: str,
    done: bool = False,
    error: str | None = None,
    event: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if event:
        _EVENTS.append(f"{time.strftime('%H:%M:%S')}  {event}")
        del _EVENTS[:-300]
    text = json.dumps(
        {
            "pct": float(pct),
            "message": message,
            "done": bool(done),
            "error": error,
            "events": list(_EVENTS),
        },
        ensure_ascii=False,
    )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    try:
        path.write_text(text, encoding="utf-8")
    except PermissionError:
        if done:
            raise
    finally:
        temporary.unlink(missing_ok=True)


def _read_track(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    segments = payload["segments"] if isinstance(payload, dict) else payload
    if not segments:
        raise ValueError("Nejdřív načtěte trať.")
    return segments


def _write_track(path: Path, segments: list, message: str) -> str:
    from blender_lidartool.engine.trackedit import track_stats

    stats = track_stats(segments)
    path.write_text(
        json.dumps({"segments": segments, "message": message, **stats}, ensure_ascii=False),
        encoding="utf-8",
    )
    return message


def _progress(status: Path):
    def inner(pct: float, message: str, event: str | None = None) -> None:
        write_status(status, pct, message, event=event)

    return inner


def cmd_import(args) -> str:
    from blender_lidartool.engine.gpxio import parse_track_file

    source = Path(args.file)
    segments = parse_track_file(source.name, source.read_bytes())
    return _write_track(Path(args.out), segments, "Trať je načtená.")


def cmd_rally(args) -> str:
    from blender_lidartool.engine.http import session
    from blender_lidartool.engine.rallymaps import extract_track

    url = args.url.strip()
    if not (url.startswith("https://www.rally-maps.com/") or url.startswith("https://rally-maps.com/")):
        raise ValueError("Očekávám odkaz na stránku rally-maps.com.")
    response = session().get(url, timeout=40)
    response.raise_for_status()
    return _write_track(Path(args.out), [extract_track(response.text)], "Trať z Rally-Maps je načtená.")


def cmd_simplify(args) -> str:
    from blender_lidartool.engine.trackedit import simplify_segments

    segments = simplify_segments(_read_track(Path(args.track)))
    return _write_track(Path(args.out), segments, "Trať je zjednodušená.")


def cmd_snap(args) -> str:
    from blender_lidartool.engine.snap import snap_track

    segments = _read_track(Path(args.track))
    snapped = snap_track(segments[0], args.osrm, args.profile)
    return _write_track(Path(args.out), [snapped, *segments[1:]], f"Trať je přichycená na silnice ({len(snapped)} bodů).")


def cmd_extend(args) -> str:
    from blender_lidartool.engine.snap import extend_track

    segments = _read_track(Path(args.track))
    extended = extend_track(segments[0], args.osrm, args.profile, float(args.distance))
    return _write_track(Path(args.out), [extended, *segments[1:]], "Start a cíl jsou prodloužené.")


def cmd_junctions(args) -> str:
    from blender_lidartool.engine.junctions import append_junctions

    segments = _read_track(Path(args.track))
    updated, count = append_junctions(segments, float(args.length), args.profile)
    return _write_track(Path(args.out), updated, f"Doplněno odboček: {count}.")


def _load_request(path: Path) -> tuple[Path, list, dict]:
    request = json.loads(path.read_text(encoding="utf-8"))
    segments = request.get("segments") or []
    if not segments:
        raise ValueError("Nejdřív načtěte trať.")
    return Path(request["cache"]), segments, request["project"]


def cmd_terrain(args) -> str:
    from blender_lidartool.engine.crsutil import segments_to_xy
    from blender_lidartool.engine.demops import download_elevation, elevation_paths, fine_covers_brushes, source_ready
    from blender_lidartool.engine.geometry import parse_zones, zone_brush_extras
    from blender_lidartool.engine.mesh import build_mesh, write_mesh_files
    from blender_lidartool.engine.rasterutil import ElevationSampler

    cache, segments, project = _load_request(Path(args.request))
    progress = _progress(Path(args.status))
    source = project.get("mesh_source") or "dmr5g"
    if source_ready(cache, source) and fine_covers_brushes(cache, source, project):
        progress(8, "Výšky už jsou stažené")
    else:
        progress(2, "Stahuji výšky")
        download_elevation(cache, source, segments, project, progress)
    progress(60, "Sestavuji mesh")
    lines = segments_to_xy(segments)
    sampler = ElevationSampler(elevation_paths(cache, source))
    mesh = build_mesh(
        lines,
        parse_zones(project["zones"]),
        float(project["terrain_buffer_m"]),
        sampler,
        extras=zone_brush_extras(project.get("zone1_brush") or []),
    )
    output = cache / "OUTPUT"
    written = write_mesh_files(output, mesh)
    for name in ("layers.npz", "layers.json"):
        stale = output / name
        if stale.exists():
            stale.unlink()
    progress(95, "Mesh je uložený")
    return written["message"]


def cmd_texture(args) -> str:
    from blender_lidartool.engine.mesh import load_mesh
    from blender_lidartool.engine.payload import write_layers
    from blender_lidartool.engine.texture import zone_texture_layers

    cache, _segments, project = _load_request(Path(args.request))
    progress = _progress(Path(args.status))
    output = cache / "OUTPUT"
    mesh = load_mesh(output)
    zoom = int(project.get("texture_zoom") or 18)
    refresh = bool(project.get("ortho_refresh"))
    layers = zone_texture_layers(mesh, zoom, cache / "TEMP" / "ortho_cache", output / "textures", progress, refresh=refresh)
    write_layers(output, layers)
    return f"Ortofoto je připravené, {len(layers)} textur."


def _original_ortho(zone: dict) -> Path | None:
    raw = str(zone.get("original") or "").strip()
    if raw and Path(raw).is_file():
        return Path(raw)
    image = Path(str(zone.get("image") or ""))
    if image.suffix.lower() == ".png" and image.is_file():
        return image
    sibling = image.with_suffix(".png")
    if image.suffix and sibling.is_file():
        return sibling
    return None


def _write_manifest(path: Path, manifest: dict) -> None:
    zones = []
    for zone in manifest.get("zones") or []:
        zones.append({key: value for key, value in zone.items() if not str(key).startswith("_")})
    path.write_text(json.dumps({"zones": zones}, ensure_ascii=False), encoding="utf-8")


def _zone_label(zone: dict, scale: int) -> str:
    zone_id = int(zone["id"])
    part = int(zone.get("part") or 0)
    parts = int(zone.get("parts") or 1)
    if parts > 1:
        return f"Zóna {zone_id} díl {part + 1}/{parts} · {scale}×"
    return f"Zóna {zone_id} · {scale}×"


def _upscale_zone(zone: dict, scale: int, tile: int, from_original: bool, upscale_x4, report) -> None:
    from blender_lidartool.engine.upscale import TILE_OVERLAP, _image_size, upscale_png

    original = _original_ortho(zone)
    if from_original:
        if original is None:
            raise ValueError("Originál ortofota už není na disku. Položte ortofoto znovu, nebo zvolte upscalovanou verzi.")
        path = original
    else:
        path = Path(zone["image"])
    saved = Path(str(zone.get("_check_source") or ""))
    if saved.is_file():
        path = saved
    if not path.is_file():
        raise ValueError(f"Chybí ortofoto zóny {int(zone['id'])}.")
    if not zone.get("_check_size"):
        size = _image_size(path)
        if size is None:
            raise ValueError(f"Nejde přečíst rozměr ortofota zóny {int(zone['id'])}.")
        zone["_check_size"] = [size[0], size[1]]
    keep_source = original is not None and path.resolve() == original.resolve()
    zone["_check_source"] = str(path)
    zone["image"] = str(
        upscale_png(
            path,
            scale,
            tile,
            TILE_OVERLAP,
            upscale_x4,
            keep_source=keep_source,
            progress=report,
            label=_zone_label(zone, scale),
        )
    )
    if original is not None:
        zone["original"] = str(original)


def _zone_problem(zone: dict, scale: int, output: Path) -> str | None:
    from blender_lidartool.engine.upscale import upscaled_problem

    if scale <= 1:
        return None
    source = Path(str(zone.get("_check_source") or ""))
    if not source.is_file():
        source = _original_ortho(zone) or Path(str(zone.get("image") or ""))
    raw = zone.get("_check_size")
    source_size = (int(raw[0]), int(raw[1])) if raw else None
    return upscaled_problem(Path(str(zone.get("image") or "")), source, scale, output, source_size)


def cmd_upscale(args) -> str:
    from blender_lidartool.engine.upscale import UPSCALE_SCALES

    cache, _segments, project = _load_request(Path(args.request))
    progress = _progress(Path(args.status))
    output = cache / "OUTPUT"
    manifest_path = output / "layers.json"
    if not manifest_path.exists():
        raise ValueError("Nejdřív položte ortofoto na materiál.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    zones = manifest.get("zones") or []
    if not zones:
        raise ValueError("Ortofoto nemá žádnou zónu.")
    scales = {int(key): int(value) for key, value in (project.get("upscale") or {}).items()}
    for scale in scales.values():
        if scale not in UPSCALE_SCALES:
            raise ValueError("Násobek upscale musí být 1, 2, 4, 8 nebo 16.")
    tile = int(project.get("upscale_tile") or 384)
    from_original = str(project.get("upscale_from") or "original") != "upscaled"
    weights = Path(project.get("weights") or "")
    try:
        from blender_lidartool.engine.realesrgan_model import load_upscaler

        progress(5, "Načítám Real-ESRGAN", event="načítám Real-ESRGAN")
        upscale_x4 = load_upscaler(weights)
    except ModuleNotFoundError as exc:
        if "torch" not in str(exc).lower():
            raise
        raise ValueError("Ve workeru chybí PyTorch. V panelu Terén klikněte Nainstalovat AI model.") from exc
    changed = 0
    total = len(zones)
    for index, zone in enumerate(zones):
        scale = scales.get(int(zone["id"]), 1)
        start = 10 + 78 * index / total
        end = 10 + 78 * (index + 1) / total
        label = _zone_label(zone, scale)
        if scale == 1:
            progress(end, f"{label} · zůstává 1×", event=f"{label} zůstává 1×")
            continue

        def report(frac, message, event=None, start=start, end=end):
            progress(start + (end - start) * frac, message, event=event)

        progress(start, f"{label} · připravuji", event=f"{label} začátek")
        try:
            _upscale_zone(zone, scale, tile, from_original, upscale_x4, report)
            changed += 1
        except Exception as exc:
            progress(end, f"{label} · chyba, zkusím znovu po kontrole", event=f"{label} chyba: {exc}")
            zone["_check_error"] = str(exc)
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:
            pass
    _write_manifest(manifest_path, manifest)
    pending = []
    for zone in zones:
        scale = scales.get(int(zone["id"]), 1)
        if scale <= 1:
            continue
        reason = zone.pop("_check_error", None) or _zone_problem(zone, scale, output)
        if reason:
            pending.append((zone, reason))
    redone = 0
    if pending:
        progress(90, f"Kontrola OUTPUT · chybí nebo nesedí {len(pending)}, generuji znovu", event=f"kontrola OUTPUT · chybí nebo nesedí {len(pending)}")
        still = []
        for index, (zone, reason) in enumerate(pending):
            scale = scales.get(int(zone["id"]), 1)
            label = _zone_label(zone, scale)
            start = 90 + 8 * index / len(pending)
            end = 90 + 8 * (index + 1) / len(pending)

            def report(frac, message, event=None, start=start, end=end):
                progress(start + (end - start) * frac, message, event=event)

            progress(start, f"{label} · znovu, předtím: {reason}", event=f"{label} korekce znovu: {reason}")
            try:
                _upscale_zone(zone, scale, tile, from_original, upscale_x4, report)
                redone += 1
                again = _zone_problem(zone, scale, output)
            except Exception as exc:
                again = str(exc)
            if again:
                still.append(f"{label}: {again}")
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
        _write_manifest(manifest_path, manifest)
        if still:
            raise ValueError("Ve složce OUTPUT se nepodařilo doplnit: " + "; ".join(still))
    else:
        progress(94, "Kontrola OUTPUT · soubory sedí", event="kontrola OUTPUT · soubory sedí")
    _write_manifest(manifest_path, manifest)
    progress(98, "Ortofoto je zvětšené")
    if redone:
        return f"Zvětšeno zón: {changed}. Znovu vygenerováno: {redone}."
    return f"Zvětšeno zón: {changed}."


def cmd_reference(args) -> str:
    from blender_lidartool.engine.geometry import reference_local

    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    origin = None
    mesh_path = Path(request["cache"]) / "OUTPUT" / "mesh.npz"
    if mesh_path.exists():
        import numpy as np

        origin = np.load(mesh_path)["origin"]
    placement = reference_local(request["segments"], request["reference"], origin)
    Path(request["out"]).write_text(json.dumps(placement), encoding="utf-8")
    return "Předloha je umístěná."


def cmd_driveline(args) -> str:
    from blender_lidartool.engine.demops import elevation_paths
    from blender_lidartool.engine.driveline import finite_lines, sample_driveline
    from blender_lidartool.engine.mesh import load_mesh
    from blender_lidartool.engine.payload import save_driveline
    from blender_lidartool.engine.rasterutil import ElevationSampler

    cache, segments, project = _load_request(Path(args.request))
    progress = _progress(Path(args.status))
    output = cache / "OUTPUT"
    mesh = load_mesh(output)
    step = float(project.get("driveline_step_m") or 5)
    source = project.get("mesh_source") or "dmr5g"
    progress(20, "Vzorkuji výšky na ose")
    sampler = ElevationSampler(elevation_paths(cache, source))
    sampled = sample_driveline(segments, step, sampler, mesh["origin"])
    lines = finite_lines(sampled["local"])
    if not lines:
        raise ValueError("Osa nemá žádné body s výškou.")
    save_driveline(output, lines)
    progress(90, "Osa je uložená")
    return f"Osa má {sum(len(line) for line in lines)} bodů."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blender_lidartool.worker")
    parser.add_argument("--status", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    imported = commands.add_parser("import")
    imported.add_argument("--file", required=True)
    imported.add_argument("--out", required=True)
    imported.set_defaults(func=cmd_import)

    rally = commands.add_parser("rally")
    rally.add_argument("--url", required=True)
    rally.add_argument("--out", required=True)
    rally.set_defaults(func=cmd_rally)

    simplify = commands.add_parser("simplify")
    simplify.add_argument("--track", required=True)
    simplify.add_argument("--out", required=True)
    simplify.set_defaults(func=cmd_simplify)

    snap = commands.add_parser("snap")
    snap.add_argument("--track", required=True)
    snap.add_argument("--out", required=True)
    snap.add_argument("--osrm", required=True)
    snap.add_argument("--profile", required=True)
    snap.set_defaults(func=cmd_snap)

    extend = commands.add_parser("extend")
    extend.add_argument("--track", required=True)
    extend.add_argument("--out", required=True)
    extend.add_argument("--osrm", required=True)
    extend.add_argument("--profile", required=True)
    extend.add_argument("--distance", required=True, type=float)
    extend.set_defaults(func=cmd_extend)

    junctions = commands.add_parser("junctions")
    junctions.add_argument("--track", required=True)
    junctions.add_argument("--out", required=True)
    junctions.add_argument("--profile", required=True)
    junctions.add_argument("--length", required=True, type=float)
    junctions.set_defaults(func=cmd_junctions)

    reference = commands.add_parser("reference")
    reference.add_argument("--request", required=True)
    reference.set_defaults(func=cmd_reference)

    for name, func in (("terrain", cmd_terrain), ("texture", cmd_texture), ("driveline", cmd_driveline), ("upscale", cmd_upscale)):
        command = commands.add_parser(name)
        command.add_argument("--request", required=True)
        command.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status = Path(args.status)
    try:
        message = args.func(args)
    except Exception as exc:
        write_status(status, 100, str(exc), done=True, error=str(exc), event=f"chyba: {exc}")
        return 1
    write_status(status, 100, message, done=True, event=message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
