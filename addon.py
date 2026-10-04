import json
import math
import shutil
import threading
from pathlib import Path

import bpy

from . import editor_server, runtime, scene
from .engine.upscale_usage import estimate_upscale_usage, jobs_from_manifest

_BUSY = False
_OPERATORS: list = []


def _attach(context, operator) -> None:
    operator._cancel_requested = False
    if operator not in _OPERATORS:
        _OPERATORS.append(operator)
    context.scene.lidar_busy = True


def _detach(context, operator) -> None:
    if operator in _OPERATORS:
        _OPERATORS.remove(operator)
    if context is not None and getattr(context, "scene", None) is not None:
        context.scene.lidar_busy = bool(_OPERATORS)


def _cache(context) -> Path:
    custom = context.scene.lidar_cache.strip()
    path = Path(custom) if custom else runtime.default_cache()
    for name in ("DEM", "ORTHO", "OUTPUT", "TEMP"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _project(scene) -> dict:
    zones = []
    for index in range(1, 5):
        radius = float(getattr(scene, f"lidar_zone{index}_radius"))
        step = float(getattr(scene, f"lidar_zone{index}_step"))
        zones.append(
            {
                "id": index,
                "radius_m": None if index == 4 and radius <= 0 else radius,
                "step_m": step,
            }
        )
    return {
        "terrain_buffer_m": float(scene.lidar_buffer),
        "road_buffer_m": float(scene.lidar_corridor),
        "road_only": bool(scene.lidar_road_only),
        "mesh_source": scene.lidar_source,
        "zones": zones,
        "zone1_brush": _zone1_brush(scene),
        "texture_zoom": int(scene.lidar_zoom),
        "driveline_step_m": float(scene.lidar_drive_step),
        "native_pixel_m": 2,
        "coarse_pixel_m": 20,
        "snap_profile": scene.lidar_profile,
        "osrm_url": scene.lidar_osrm.strip() or "https://router.project-osrm.org",
        "upscale": {str(index): int(getattr(scene, f"lidar_upscale_{index}")) for index in range(1, 5)},
        "upscale_tile": int(scene.lidar_upscale_tile),
        "upscale_from": scene.lidar_upscale_source,
        "weights": str(runtime.weights_path()),
    }


def _write_request(context, project_extra: dict | None = None, cache: Path | None = None) -> Path:
    cache = cache or _cache(context)
    segments = json.loads(context.scene.lidar_track or "[]")
    project = _project(context.scene)
    if project_extra:
        project.update(project_extra)
    path = cache / "TEMP" / "request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cache": str(cache), "segments": segments, "project": project}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _terrain_object():
    for obj in bpy.data.objects:
        if obj.type == "MESH" and (obj.name == "Teren" or obj.name.startswith("Teren.")):
            return obj
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is not None and material.name.startswith("Teren"):
                return obj
    return None


def _output_dir_from_texture(path: Path) -> Path | None:
    folder = path.parent
    if folder.name == "textures":
        folder = folder.parent
    if folder.name == "OUTPUT" and (folder / "mesh.npz").is_file():
        return folder
    return None


def _terrain_output(context) -> Path | None:
    current = _cache(context) / "OUTPUT"
    if (current / "mesh.npz").is_file():
        return current
    paths = []
    obj = _terrain_object()
    if obj is not None and obj.data is not None:
        for slot in obj.material_slots:
            material = slot.material
            if material is None or not material.use_nodes or material.node_tree is None:
                continue
            for node in material.node_tree.nodes:
                image = getattr(node, "image", None)
                if image is not None and image.filepath:
                    paths.append(Path(bpy.path.abspath(image.filepath)))
    for image in bpy.data.images:
        if image.filepath and image.name.startswith("teren_zona_"):
            paths.append(Path(bpy.path.abspath(image.filepath)))
    for path in paths:
        found = _output_dir_from_texture(path)
        if found is not None:
            return found
    return None


def _track_length_m(segments) -> float:
    total = 0.0
    for segment in segments:
        for start, end in zip(segment, segment[1:]):
            total += _haversine_m(start, end)
    return total


def _haversine_m(start, end) -> float:
    radius = 6371000.0
    lat1 = math.radians(float(start["lat"]))
    lat2 = math.radians(float(end["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(end["lon"]) - float(start["lon"]))
    arc = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(arc)))


def _apply_segments(context, segments) -> None:
    context.scene.lidar_track = json.dumps(segments, ensure_ascii=False)
    context.scene.lidar_points = sum(len(segment) for segment in segments)
    context.scene.lidar_length = _track_length_m(segments)


def _store_reference(output: Path, reference) -> dict:
    source = editor_server.reference_path()
    if not source.is_file():
        raise RuntimeError("Soubor předlohy v editoru chybí. Vložte snímek znovu a přeneste ho ještě jednou.")
    output.mkdir(parents=True, exist_ok=True)
    suffix = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(editor_server.reference_type(), ".png")
    destination = output / f"reference{suffix}"
    for stale in output.glob("reference.*"):
        if stale.resolve() != destination.resolve():
            stale.unlink()
    shutil.copyfile(source, destination)
    stored = dict(reference)
    stored["image"] = str(destination)
    return stored


def _reference(scene):
    raw = (scene.lidar_reference or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _zone1_brush(scene) -> list:
    try:
        stamps = json.loads(scene.lidar_zone1_brush or "[]")
    except json.JSONDecodeError:
        return []
    return stamps if isinstance(stamps, list) else []


def _estimate_mesh_points(scene) -> int | None:
    try:
        segments = json.loads(scene.lidar_track or "[]")
    except json.JSONDecodeError:
        return None
    coords = [point for segment in segments if isinstance(segment, list) for point in segment if isinstance(point, dict)]
    if len(coords) < 2:
        return None
    lats = [float(point["lat"]) for point in coords]
    lons = [float(point["lon"]) for point in coords]
    lat = sum(lats) / len(lats)
    meters_lat = 111_320.0
    meters_lon = 111_320.0 * math.cos(math.radians(lat))
    span_x = (max(lons) - min(lons)) * meters_lon
    span_y = (max(lats) - min(lats)) * meters_lat
    margin = float(scene.lidar_buffer)
    width = max(span_x + 2 * margin, 1.0)
    height = max(span_y + 2 * margin, 1.0)
    rectangle = width * height
    length = float(scene.lidar_length) or _track_length_m(segments)
    total = 0.0
    previous = 0.0
    for index in range(1, 5):
        radius = float(getattr(scene, f"lidar_zone{index}_radius"))
        step = max(float(getattr(scene, f"lidar_zone{index}_step")), 0.5)
        if radius <= 0:
            outer = rectangle
            perimeter = 2 * (width + height)
        else:
            outer = min(rectangle, length * 2 * radius + math.pi * radius * radius)
            perimeter = 2 * length + 2 * math.pi * radius
        inner = 0.0 if previous <= 0 else min(outer, length * 2 * previous + math.pi * previous * previous)
        total += max(0.0, outer - inner) / (step * step)
        total += perimeter / step
        if index == 1 and radius > 0:
            total += (length / step) * ((2 * radius / step) + 1)
        if radius > 0:
            previous = radius
    return max(0, int(total))


def _upscale_usage(context, scene_data) -> dict:
    scales = {index: int(getattr(scene_data, f"lidar_upscale_{index}")) for index in range(1, 5)}
    try:
        custom = str(scene_data.lidar_cache or "").strip()
        folder = (Path(custom) if custom else runtime.default_cache()) / "OUTPUT"
        jobs = jobs_from_manifest(folder, scales, source=scene_data.lidar_upscale_source)
    except Exception:
        jobs = []
    return estimate_upscale_usage(int(scene_data.lidar_upscale_tile), jobs)


def _apply_zones(context, zones, terrain_buffer_m, brushes=None, corridor_m=None) -> None:
    context.scene.lidar_buffer = float(terrain_buffer_m)
    if corridor_m is not None:
        context.scene.lidar_corridor = float(corridor_m)
    for zone in zones:
        index = int(zone["id"])
        radius = 0.0 if zone["radius_m"] is None else float(zone["radius_m"])
        setattr(context.scene, f"lidar_zone{index}_radius", radius)
        setattr(context.scene, f"lidar_zone{index}_step", float(zone["step_m"]))
    if brushes is not None:
        context.scene.lidar_zone1_brush = json.dumps(brushes, ensure_ascii=False)


def _redraw(context):
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        area.tag_redraw()


def _log_tail(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text[-500:]


def _read_status(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class _Job:
    def _begin(self, context, args):
        global _BUSY
        if _BUSY:
            self.report({"WARNING"}, "Počkejte, než doběhne probíhající úloha.")
            return {"CANCELLED"}
        cache = _cache(context)
        self._status_path = cache / "TEMP" / "status.json"
        self._result_path = cache / "TEMP" / "result.json"
        self._output = cache / "OUTPUT"
        if self._status_path.exists():
            self._status_path.unlink()
        self._log_path = cache / "TEMP" / "worker.log"
        try:
            self._proc, self._log = runtime.start_worker(args, self._log_path)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _BUSY = True
        _attach(context, self)
        context.scene.lidar_status = "Spouštím úlohu"
        window = context.window or context.window_manager.windows[0]
        self._timer = context.window_manager.event_timer_add(0.4, window=window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global _BUSY
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if getattr(self, "_cancel_requested", False):
            self.cancel(context)
            context.scene.lidar_status = "Zrušeno."
            _redraw(context)
            return {"CANCELLED"}
        status = _read_status(self._status_path)
        if status:
            context.scene.lidar_status = f"{int(status['pct'])} % {status['message']}"
            _redraw(context)
        if self._proc.poll() is None:
            return {"RUNNING_MODAL"}
        self._stop(context)
        _BUSY = False
        _detach(context, self)
        runtime.watch_process(None)
        status = _read_status(self._status_path) or status
        if self._proc.returncode != 0:
            message = (status or {}).get("error") or _log_tail(self._log_path) or "Úloha selhala."
            context.scene.lidar_status = message
            self.report({"ERROR"}, message)
            _redraw(context)
            return {"CANCELLED"}
        try:
            self._apply(context)
        except Exception as exc:
            context.scene.lidar_status = str(exc)
            self.report({"ERROR"}, str(exc))
            _redraw(context)
            return {"CANCELLED"}
        if status and status.get("message"):
            context.scene.lidar_status = status["message"]
        _redraw(context)
        return {"FINISHED"}

    def cancel(self, context):
        global _BUSY
        runtime.cancel_current()
        if getattr(self, "_proc", None) is not None and self._proc.poll() is None:
            self._proc.kill()
        self._stop(context)
        _BUSY = False
        _detach(context, self)
        runtime.watch_process(None)

    def _stop(self, context):
        timer = getattr(self, "_timer", None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)
            self._timer = None
        handle = getattr(self, "_log", None)
        if handle is not None:
            handle.close()
            self._log = None

    def _apply_track(self, context):
        payload = json.loads(self._result_path.read_text(encoding="utf-8"))
        context.scene.lidar_track = json.dumps(payload["segments"], ensure_ascii=False)
        context.scene.lidar_points = int(payload["points"])
        context.scene.lidar_length = float(payload["length_m"])
        self.report({"INFO"}, payload.get("message") or "Trať je uložená.")


def _track_args(context, command, extra=()):
    cache = _cache(context)
    segments = json.loads(context.scene.lidar_track or "[]")
    track = cache / "TEMP" / "track_in.json"
    track.write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
    args = [
        "--status",
        str(cache / "TEMP" / "status.json"),
        command,
        "--track",
        str(track),
        "--out",
        str(cache / "TEMP" / "result.json"),
    ]
    args.extend(extra)
    return args


class LIDAR_OT_editor(bpy.types.Operator):
    bl_idname = "lidar.open_editor"
    bl_label = "Otevřít editor"
    bl_description = "Otevře mapu, na které se trať kreslí kliknutím"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            segments = json.loads(context.scene.lidar_track or "[]")
        except json.JSONDecodeError:
            segments = []
        saved = _reference(context.scene)
        image = Path(str((saved or {}).get("image") or ""))
        if image.is_file():
            editor_server.restore_reference_file(image)
        try:
            project = _project(context.scene)
            url = editor_server.open_session(
                segments,
                project["zones"],
                project["terrain_buffer_m"],
                project["zone1_brush"],
                _reference(context.scene),
                project["osrm_url"],
                context.scene.lidar_profile,
                project["road_buffer_m"],
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        editor_server.open_browser(url)
        self._output = _cache(context) / "OUTPUT"
        self._output.mkdir(parents=True, exist_ok=True)
        revision, _segments, _zones, _buffer, _brushes, _reference_state, _corridor = editor_server.latest_apply()
        context.scene.lidar_status = "Editor je otevřený. Tlačítko v mapě přenese trať a zóny."
        if _editor_running():
            self.report({"INFO"}, "Editor je otevřený v prohlížeči.")
            return {"FINISHED"}
        self._revision = revision
        window = context.window or context.window_manager.windows[0]
        self._timer = context.window_manager.event_timer_add(0.4, window=window)
        context.window_manager.modal_handler_add(self)
        _attach(context, self)
        _set_editor_running(True)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if getattr(self, "_cancel_requested", False):
            self.cancel(context)
            context.scene.lidar_status = "Editor je zavřený."
            _redraw(context)
            return {"CANCELLED"}
        revision, segments, zones, terrain_buffer_m, brushes, reference, corridor_m = editor_server.latest_apply()
        if revision != self._revision and segments:
            self._revision = revision
            _apply_segments(context, segments)
            if zones:
                _apply_zones(context, zones, terrain_buffer_m, brushes, corridor_m)
            reference_error = ""
            context.scene.lidar_reference = ""
            if reference:
                try:
                    stored = _store_reference(self._output, reference)
                    context.scene.lidar_reference = json.dumps(stored, ensure_ascii=False)
                except Exception as exc:
                    reference_error = str(exc)
            if reference_error:
                context.scene.lidar_status = reference_error
                self.report({"WARNING"}, reference_error)
            else:
                waiting = " Předlohu vložíte tlačítkem Vložit referenci." if reference else ""
                context.scene.lidar_status = (
                    f"Z editoru: {context.scene.lidar_points} bodů, {context.scene.lidar_length:.0f} m.{waiting}"
                )
                self.report(
                    {"INFO"},
                    "Trať a zóny jsou ve scéně. Předloha čeká na vložení." if reference else "Trať a zóny jsou ve scéně.",
                )
            _redraw(context)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        _set_editor_running(False)
        _detach(context, self)
        timer = getattr(self, "_timer", None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)


_EDITOR_RUNNING = False


def _editor_running() -> bool:
    return _EDITOR_RUNNING


def _set_editor_running(value: bool) -> None:
    global _EDITOR_RUNNING
    _EDITOR_RUNNING = value


class LIDAR_OT_import(_Job, bpy.types.Operator):
    bl_idname = "lidar.import_track"
    bl_label = "Import GPX / KML"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.gpx;*.kml;*.kmz", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        cache = _cache(context)
        return self._begin(
            context,
            [
                "--status",
                str(cache / "TEMP" / "status.json"),
                "import",
                "--file",
                self.filepath,
                "--out",
                str(cache / "TEMP" / "result.json"),
            ],
        )

    def _apply(self, context):
        self._apply_track(context)


class LIDAR_OT_rally(_Job, bpy.types.Operator):
    bl_idname = "lidar.rally"
    bl_label = "Načíst z Rally-Maps"
    bl_options = {"REGISTER"}

    def execute(self, context):
        cache = _cache(context)
        return self._begin(
            context,
            ["--status", str(cache / "TEMP" / "status.json"), "rally", "--url", context.scene.lidar_rally, "--out", str(cache / "TEMP" / "result.json")],
        )

    def _apply(self, context):
        self._apply_track(context)


class LIDAR_OT_simplify(_Job, bpy.types.Operator):
    bl_idname = "lidar.simplify"
    bl_label = "Zjednodušit"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return self._begin(context, _track_args(context, "simplify"))

    def _apply(self, context):
        self._apply_track(context)


class LIDAR_OT_snap(_Job, bpy.types.Operator):
    bl_idname = "lidar.snap"
    bl_label = "Přichytit na silnice"
    bl_options = {"REGISTER"}

    def execute(self, context):
        project = _project(context.scene)
        return self._begin(context, _track_args(context, "snap", ["--osrm", project["osrm_url"], "--profile", context.scene.lidar_profile]))

    def _apply(self, context):
        self._apply_track(context)


class LIDAR_OT_extend(_Job, bpy.types.Operator):
    bl_idname = "lidar.extend"
    bl_label = "Prodloužit"
    bl_options = {"REGISTER"}

    def execute(self, context):
        project = _project(context.scene)
        extra = ["--osrm", project["osrm_url"], "--profile", context.scene.lidar_profile, "--distance", str(context.scene.lidar_extend)]
        return self._begin(context, _track_args(context, "extend", extra))

    def _apply(self, context):
        self._apply_track(context)


class LIDAR_OT_junctions(_Job, bpy.types.Operator):
    bl_idname = "lidar.junctions"
    bl_label = "Doplnit křižovatky"
    bl_options = {"REGISTER"}

    def execute(self, context):
        extra = ["--profile", context.scene.lidar_profile, "--length", str(context.scene.lidar_junction)]
        return self._begin(context, _track_args(context, "junctions", extra))

    def _apply(self, context):
        self._apply_track(context)


class LIDAR_OT_insert_reference(_Job, bpy.types.Operator):
    bl_idname = "lidar.insert_reference"
    bl_label = "Vložit referenci"
    bl_description = "Vloží předlohu přenesenou z editoru jako samostatný objekt"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        reference = _reference(context.scene)
        if not reference:
            self.report({"ERROR"}, "Nejdřív přeneste předlohu z editoru.")
            return {"CANCELLED"}
        image = Path(str(reference.get("image") or ""))
        if not image.is_file():
            self.report({"ERROR"}, "Soubor předlohy chybí. Přeneste ji z editoru znovu.")
            return {"CANCELLED"}
        if not runtime.venv_ready():
            self.report({"ERROR"}, "Pro umístění předlohy nejdřív připravte prostředí addonu.")
            return {"CANCELLED"}
        cache = _cache(context)
        request = cache / "TEMP" / "reference_request.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            json.dumps(
                {
                    "cache": str(cache),
                    "segments": json.loads(context.scene.lidar_track or "[]"),
                    "reference": reference,
                    "out": str(cache / "TEMP" / "result.json"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return self._begin(
            context,
            ["--status", str(cache / "TEMP" / "status.json"), "reference", "--request", str(request)],
        )

    def _apply(self, context):
        reference = _reference(context.scene) or {}
        image = Path(str(reference.get("image") or ""))
        placement = json.loads(self._result_path.read_text(encoding="utf-8"))
        scene.insert_reference(context, image, placement)
        self.report({"INFO"}, "Předloha je ve scéně.")


class LIDAR_OT_terrain(_Job, bpy.types.Operator):
    bl_idname = "lidar.terrain"
    bl_label = "Vložit terén"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        request = _write_request(context)
        cache = _cache(context)
        return self._begin(context, ["--status", str(cache / "TEMP" / "status.json"), "terrain", "--request", str(request)])

    def _apply(self, context):
        scene.insert_terrain(context, self._output)
        self.report({"INFO"}, "Terén je ve scéně.")


class LIDAR_OT_texture(_Job, bpy.types.Operator):
    bl_idname = "lidar.texture"
    bl_label = "Ortofoto na materiál"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        request = _write_request(context)
        cache = _cache(context)
        return self._begin(context, ["--status", str(cache / "TEMP" / "status.json"), "texture", "--request", str(request)])

    def _apply(self, context):
        scene.insert_terrain(context, self._output)
        self.report({"INFO"}, "Ortofoto je na materiálu.")


class LIDAR_OT_redownload_ortho(_Job, bpy.types.Operator):
    bl_idname = "lidar.redownload_ortho"
    bl_label = "Stáhnout ortofoto znovu"
    bl_description = "Znovu stáhne ortofoto z ČÚZK a vymění ho na materiálu. Terén ve scéně nechá"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        output = _terrain_output(context)
        if output is None:
            if _terrain_object() is not None:
                self.report({"ERROR"}, "U terénu ve scéně se nenašel soubor mesh.npz. Zkontrolujte Složku cache.")
            else:
                self.report({"ERROR"}, "Nejdřív vložte terén.")
            return {"CANCELLED"}
        _write_request(context, {"ortho_refresh": True}, cache=output.parent)
        status_cache = _cache(context)
        result = self._begin(context, ["--status", str(status_cache / "TEMP" / "status.json"), "texture", "--request", str(output.parent / "TEMP" / "request.json")])
        self._output = output
        return result

    def _apply(self, context):
        scene.apply_ortho(context, self._output)
        self.report({"INFO"}, "Ortofoto je znovu stažené.")


class LIDAR_OT_upscale(_Job, bpy.types.Operator):
    bl_idname = "lidar.upscale"
    bl_label = "AI upscale ortofota"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not runtime.ai_ready():
            self.report({"ERROR"}, "Nejdřív nainstalujte AI model.")
            return {"CANCELLED"}
        request = _write_request(context)
        cache = _cache(context)
        return self._begin(context, ["--status", str(cache / "TEMP" / "status.json"), "upscale", "--request", str(request)])

    def _apply(self, context):
        scene.reload_ortho(context, self._output)
        self.report({"INFO"}, "Ortofoto je zvětšené.")


class LIDAR_OT_driveline(_Job, bpy.types.Operator):
    bl_idname = "lidar.driveline"
    bl_label = "Osa jako křivka"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        request = _write_request(context)
        cache = _cache(context)
        return self._begin(context, ["--status", str(cache / "TEMP" / "status.json"), "driveline", "--request", str(request)])

    def _apply(self, context):
        scene.insert_driveline(context, self._output)
        self.report({"INFO"}, "Osa je ve scéně.")


class LIDAR_OT_install_ai(bpy.types.Operator):
    bl_idname = "lidar.install_ai"
    bl_label = "Nainstalovat AI model"
    bl_description = "Do zvolené složky nainstaluje CUDA PyTorch a stáhne Real-ESRGAN x4plus"
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _BUSY
        if not runtime.venv_ready():
            self.report({"ERROR"}, "Nejdřív připravte prostředí addonu.")
            return {"CANCELLED"}
        if _BUSY:
            self.report({"WARNING"}, "Počkejte, než doběhne probíhající úloha.")
            return {"CANCELLED"}
        _BUSY = True
        _attach(context, self)
        self._error = None
        log = runtime.config_root() / "install-ai.log"
        python = runtime.venv_python()
        weights = runtime.weights_path()
        venv = runtime.venv_dir()
        record = runtime.install_record_path()
        cleanup_dirs = runtime.ai_locations()

        def work():
            try:
                runtime.install_ai(log, python, weights, venv, record, cleanup_dirs)
            except Exception as exc:
                self._error = str(exc)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        context.scene.lidar_status = "Instaluji PyTorch a Real-ESRGAN do zvolené složky. Může to trvat dlouho."
        window = context.window or context.window_manager.windows[0]
        self._timer = context.window_manager.event_timer_add(0.5, window=window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global _BUSY
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if getattr(self, "_cancel_requested", False):
            runtime.cancel_current()
            self._error = "Zrušeno."
        if self._thread.is_alive() and not getattr(self, "_cancel_requested", False):
            _redraw(context)
            return {"RUNNING_MODAL"}
        context.window_manager.event_timer_remove(self._timer)
        _BUSY = False
        _detach(context, self)
        runtime.watch_process(None)
        if self._error:
            context.scene.lidar_status = self._error
            self.report({"ERROR"} if self._error != "Zrušeno." else {"WARNING"}, self._error)
            _redraw(context)
            return {"CANCELLED"}
        context.scene.lidar_status = "AI model je připravený."
        self.report({"INFO"}, "AI model je připravený.")
        _redraw(context)
        return {"FINISHED"}

    def cancel(self, context):
        global _BUSY
        runtime.cancel_current()
        self._cancel_requested = True
        _BUSY = False
        _detach(context, self)
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)


class LIDAR_OT_uninstall_ai(bpy.types.Operator):
    bl_idname = "lidar.uninstall_ai"
    bl_label = "Odinstalovat AI model"
    bl_description = "Smaže váhy Real-ESRGAN a PyTorch ze složky, kam se model nainstaloval"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        if _BUSY:
            self.report({"WARNING"}, "Počkejte, než doběhne probíhající úloha.")
            return {"CANCELLED"}
        if not runtime.ai_present():
            self.report({"WARNING"}, "AI model není nainstalovaný.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, title="Odinstalovat AI model")

    def draw(self, _context):
        self.layout.label(text="Smaže váhy Real-ESRGAN a PyTorch.")

    def execute(self, context):
        global _BUSY
        if _BUSY:
            self.report({"WARNING"}, "Počkejte, než doběhne probíhající úloha.")
            return {"CANCELLED"}
        if not runtime.ai_present():
            self.report({"WARNING"}, "AI model není nainstalovaný.")
            return {"CANCELLED"}
        _BUSY = True
        _attach(context, self)
        self._error = None
        root = runtime.config_root()
        log = root / "uninstall-ai.log"
        python = runtime.venv_python()
        directories = runtime.ai_locations()
        venv = runtime.venv_dir()
        record = runtime.install_record_path()

        def work():
            try:
                runtime.uninstall_ai(log, python, directories, venv, record)
            except Exception as exc:
                self._error = str(exc)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        context.scene.lidar_status = "Odinstalovávám AI model."
        window = context.window or context.window_manager.windows[0]
        self._timer = context.window_manager.event_timer_add(0.5, window=window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global _BUSY
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if getattr(self, "_cancel_requested", False):
            runtime.cancel_current()
            self._error = "Zrušeno."
        if self._thread.is_alive() and not getattr(self, "_cancel_requested", False):
            _redraw(context)
            return {"RUNNING_MODAL"}
        context.window_manager.event_timer_remove(self._timer)
        _BUSY = False
        _detach(context, self)
        runtime.watch_process(None)
        if self._error:
            context.scene.lidar_status = self._error
            self.report({"ERROR"} if self._error != "Zrušeno." else {"WARNING"}, self._error)
            _redraw(context)
            return {"CANCELLED"}
        context.scene.lidar_status = "AI model je odinstalovaný."
        self.report({"INFO"}, "AI model je odinstalovaný.")
        _redraw(context)
        return {"FINISHED"}

    def cancel(self, context):
        global _BUSY
        runtime.cancel_current()
        self._cancel_requested = True
        _BUSY = False
        _detach(context, self)
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)


class LIDAR_OT_prepare(bpy.types.Operator):
    bl_idname = "lidar.prepare"
    bl_label = "Připravit prostředí"
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _BUSY
        if _BUSY:
            self.report({"WARNING"}, "Počkejte, než doběhne probíhající úloha.")
            return {"CANCELLED"}
        _BUSY = True
        _attach(context, self)
        self._error = None
        root = runtime.config_root()
        log = root / "install.log"
        python = runtime.blender_python()
        target = runtime.venv_dir()
        requirements = runtime.addon_root() / "requirements.txt"
        get_pip = root / "get-pip.py"

        def work():
            try:
                runtime.prepare_venv(log, python, target, requirements, get_pip)
            except Exception as exc:
                self._error = str(exc)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        context.scene.lidar_status = "Instaluji knihovny. Může to trvat několik minut."
        window = context.window or context.window_manager.windows[0]
        self._timer = context.window_manager.event_timer_add(0.5, window=window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global _BUSY
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if getattr(self, "_cancel_requested", False):
            runtime.cancel_current()
            self._error = "Zrušeno."
        if self._thread.is_alive() and not getattr(self, "_cancel_requested", False):
            _redraw(context)
            return {"RUNNING_MODAL"}
        context.window_manager.event_timer_remove(self._timer)
        _BUSY = False
        _detach(context, self)
        runtime.watch_process(None)
        if self._error:
            context.scene.lidar_status = self._error
            self.report({"ERROR"} if self._error != "Zrušeno." else {"WARNING"}, self._error)
            _redraw(context)
            return {"CANCELLED"}
        context.scene.lidar_status = "Prostředí je připravené."
        self.report({"INFO"}, "Prostředí je připravené.")
        _redraw(context)
        return {"FINISHED"}

    def cancel(self, context):
        global _BUSY
        runtime.cancel_current()
        self._cancel_requested = True
        _BUSY = False
        _detach(context, self)
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)


class LIDAR_OT_cancel(bpy.types.Operator):
    bl_idname = "lidar.cancel_job"
    bl_label = "Zrušit"
    bl_description = "Zruší probíhající akci"
    bl_options = {"REGISTER"}

    def execute(self, context):
        runtime.cancel_current()
        if _OPERATORS:
            _OPERATORS[-1]._cancel_requested = True
        else:
            context.scene.lidar_busy = False
            context.scene.lidar_status = "Zrušeno."
        return {"FINISHED"}


class LIDAR_PT_terrain(bpy.types.Panel):
    bl_label = "Traťový terén"
    bl_idname = "LIDAR_PT_terrain"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Terén"

    def draw(self, context):
        layout = self.layout
        scene_data = context.scene
        ready = runtime.venv_ready()
        box = layout.box()
        box.label(text="Prostředí je připravené." if ready else "Prostředí není připravené.")
        box.operator("lidar.prepare", text="Sestavit znovu" if ready else "Připravit prostředí")
        box.prop(scene_data, "lidar_cache")

        box = layout.box()
        box.label(text="Trať")
        box.operator("lidar.open_editor", icon="WORLD")
        box.label(text="Kliknutím do mapy přidáte body.")
        if scene_data.lidar_points:
            box.label(text=f"{scene_data.lidar_points} bodů, {scene_data.lidar_length:.0f} m")
        else:
            box.label(text="Trať není načtená.")
        estimate = _estimate_mesh_points(scene_data)
        if estimate is None:
            box.label(text="Odhad bodů meshe: —")
        else:
            box.label(text=f"Odhad bodů meshe: {estimate:,}".replace(",", " "))

        box = layout.box()
        box.label(text="Data")
        box.prop(scene_data, "lidar_road_only")
        box.prop(scene_data, "lidar_source")
        box.prop(scene_data, "lidar_zoom")
        box.prop(scene_data, "lidar_drive_step")
        box.prop(scene_data, "lidar_osrm")
        box.separator()
        if runtime.ai_ready():
            box.label(text="AI model je připravený.")
        elif runtime.ai_present():
            box.label(text="AI model ve zvolené složce chybí.")
        else:
            box.label(text="AI model není nainstalovaný.")
        box.prop(scene_data, "lidar_ai_dir")
        box.operator("lidar.install_ai", text="Přeinstalovat AI model" if runtime.ai_ready() else "Nainstalovat AI model")
        remove = box.row()
        remove.enabled = runtime.ai_present()
        remove.operator("lidar.uninstall_ai", text="Odinstalovat AI model")
        box.prop(scene_data, "lidar_upscale_source")
        for index in range(1, 5):
            box.prop(scene_data, f"lidar_upscale_{index}")
        split = box.split(factor=0.48)
        split.prop(scene_data, "lidar_upscale_tile")
        usage = _upscale_usage(context, scene_data)
        info = split.column(align=True)
        info.label(text=usage["gpu_line"])
        info.label(text=usage["hint_line"])
        if usage.get("size_line"):
            box.label(text=usage["size_line"])
        box.label(text=usage["ram_line"])

        layout.operator("lidar.terrain", icon="MESH_GRID")
        layout.operator("lidar.insert_reference", icon="OUTLINER_OB_IMAGE")
        layout.operator("lidar.texture", icon="TEXTURE")
        layout.operator("lidar.redownload_ortho", icon="FILE_REFRESH")
        layout.operator("lidar.upscale", icon="IMAGE_DATA")
        layout.operator("lidar.driveline", icon="CURVE_PATH")
        status_row = layout.row(align=True)
        status_col = status_row.column(align=True)
        chunks = [part for part in scene_data.lidar_status.split(" · ") if part]
        if len(chunks) >= 3:
            status_col.label(text=" · ".join(chunks[:2]))
            status_col.label(text=" · ".join(chunks[2:]))
        else:
            status_col.label(text=scene_data.lidar_status)
        if scene_data.lidar_busy:
            status_row.operator("lidar.cancel_job", text="", icon="X")


CLASSES = (
    LIDAR_OT_cancel,
    LIDAR_OT_prepare,
    LIDAR_OT_install_ai,
    LIDAR_OT_uninstall_ai,
    LIDAR_OT_editor,
    LIDAR_OT_import,
    LIDAR_OT_rally,
    LIDAR_OT_simplify,
    LIDAR_OT_snap,
    LIDAR_OT_extend,
    LIDAR_OT_junctions,
    LIDAR_OT_terrain,
    LIDAR_OT_insert_reference,
    LIDAR_OT_texture,
    LIDAR_OT_redownload_ortho,
    LIDAR_OT_upscale,
    LIDAR_OT_driveline,
    LIDAR_PT_terrain,
)

def _ai_dir_get(_self):
    return runtime.ai_dir_text()


def _ai_dir_set(_self, value):
    runtime.set_ai_dir(value)


SCENE_PROPS = (
    ("lidar_cache", bpy.props.StringProperty(name="Složka cache", subtype="DIR_PATH")),
    ("lidar_ai_dir", bpy.props.StringProperty(
        name="Složka AI modelu",
        subtype="DIR_PATH",
        description="Kam se stáhne Real-ESRGAN a nainstaluje PyTorch. Výchozí je složka nastavení Blenderu",
        get=_ai_dir_get,
        set=_ai_dir_set,
    )),
    ("lidar_track", bpy.props.StringProperty(default="[]")),
    ("lidar_points", bpy.props.IntProperty(name="Body", default=0)),
    ("lidar_length", bpy.props.FloatProperty(name="Délka", default=0.0)),
    ("lidar_status", bpy.props.StringProperty(name="Stav", default="Připraveno")),
    ("lidar_busy", bpy.props.BoolProperty(name="Běží", default=False)),
    ("lidar_rally", bpy.props.StringProperty(name="Rally-Maps URL")),
    ("lidar_profile", bpy.props.EnumProperty(
        name="Profil",
        items=(
            ("driving", "auto", ""),
            ("cycling", "kolo", ""),
            ("walking", "chůze", ""),
        ),
        default="driving",
    )),
    ("lidar_extend", bpy.props.FloatProperty(name="Prodloužení", default=100.0, min=10.0, unit="LENGTH")),
    ("lidar_junction", bpy.props.FloatProperty(name="Délka odbočky", default=40.0, min=10.0, unit="LENGTH")),
    ("lidar_buffer", bpy.props.FloatProperty(name="Hranice terénu", default=2000.0, min=50.0, unit="LENGTH")),
    ("lidar_corridor", bpy.props.FloatProperty(name="Koridor silnice", default=25.0, min=5.0, unit="LENGTH")),
    ("lidar_road_only", bpy.props.BoolProperty(
        name="Podrobně jen koridor",
        default=True,
        description="Podrobný výškový model v koridoru silnice a v namalovaných zónách, okolí hruběji",
    )),
    ("lidar_source", bpy.props.EnumProperty(
        name="Zdroj",
        items=(
            ("dmr5g", "DMR 5G, terén", ""),
            ("dmp1g", "DMP 1G, včetně vegetace", ""),
        ),
        default="dmr5g",
    )),
    ("lidar_zone1_brush", bpy.props.StringProperty(default="[]")),
    ("lidar_reference", bpy.props.StringProperty(default="")),
    ("lidar_zone1_radius", bpy.props.FloatProperty(name="Poloměr", default=12.0, min=0.0)),
    ("lidar_zone1_step", bpy.props.FloatProperty(name="Krok", default=2.0, min=0.5)),
    ("lidar_zone2_radius", bpy.props.FloatProperty(name="Poloměr", default=80.0, min=0.0)),
    ("lidar_zone2_step", bpy.props.FloatProperty(name="Krok", default=6.0, min=0.5)),
    ("lidar_zone3_radius", bpy.props.FloatProperty(name="Poloměr", default=400.0, min=0.0)),
    ("lidar_zone3_step", bpy.props.FloatProperty(name="Krok", default=20.0, min=0.5)),
    ("lidar_zone4_radius", bpy.props.FloatProperty(name="Poloměr", default=0.0, min=0.0, description="0 znamená zbytek terénu")),
    ("lidar_zone4_step", bpy.props.FloatProperty(name="Krok", default=40.0, min=0.5)),
    ("lidar_zoom", bpy.props.IntProperty(
        name="Zoom ortofota",
        default=18,
        min=6,
        max=20,
        description="Zóna 1 drží tento zoom. Dlouhá trať se rozdělí na víc textur, aby se kvalita nesnižovala",
    )),
    ("lidar_upscale_source", bpy.props.EnumProperty(
        name="Upscale z",
        items=(
            ("original", "originálu", "Stažené ortofoto, bez předchozího zvětšení"),
            ("upscaled", "upscalované verze", "Obrázek, který je teď na materiálu"),
        ),
        default="original",
    )),
    ("lidar_upscale_1", bpy.props.EnumProperty(name="Zóna 1", items=(("1", "1×", ""), ("2", "2×", ""), ("4", "4×", ""), ("8", "8×", ""), ("16", "16×", "")), default="8")),
    ("lidar_upscale_2", bpy.props.EnumProperty(name="Zóna 2", items=(("1", "1×", ""), ("2", "2×", ""), ("4", "4×", ""), ("8", "8×", ""), ("16", "16×", "")), default="4")),
    ("lidar_upscale_3", bpy.props.EnumProperty(name="Zóna 3", items=(("1", "1×", ""), ("2", "2×", ""), ("4", "4×", ""), ("8", "8×", ""), ("16", "16×", "")), default="2")),
    ("lidar_upscale_4", bpy.props.EnumProperty(name="Zóna 4", items=(("1", "1×", ""), ("2", "2×", ""), ("4", "4×", ""), ("8", "8×", ""), ("16", "16×", "")), default="1")),
    ("lidar_upscale_tile", bpy.props.IntProperty(
        name="Dlaždice",
        default=384,
        min=128,
        max=512,
        description="Větší dlaždice je rychlejší, ale potřebuje víc VRAM. 384 je pro 8 GB grafiku",
    )),
    ("lidar_drive_step", bpy.props.FloatProperty(name="Krok osy", default=5.0, min=1.0, unit="LENGTH")),
    ("lidar_osrm", bpy.props.StringProperty(name="OSRM URL", default="https://router.project-osrm.org")),
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for name, prop in SCENE_PROPS:
        setattr(bpy.types.Scene, name, prop)


def unregister():
    editor_server.stop()
    for name, _prop in reversed(SCENE_PROPS):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
