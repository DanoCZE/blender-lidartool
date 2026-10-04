"""Místní mapa pro kreslení tratě. Běží v Pythonu Blenderu. Zjednodušení a přichycení počítá prostředí addonu."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

EDITOR_ROOT = Path(__file__).resolve().parent / "editor"
EDITOR_HTML = EDITOR_ROOT / "index.html"
_LOCK = threading.Lock()
_SERVER: ThreadingHTTPServer | None = None
DEFAULT_ZONES = [
    {"id": 1, "radius_m": 12.0, "step_m": 2.0},
    {"id": 2, "radius_m": 80.0, "step_m": 6.0},
    {"id": 3, "radius_m": 400.0, "step_m": 20.0},
    {"id": 4, "radius_m": None, "step_m": 40.0},
]

_TOKEN = ""
_SEGMENTS: list = []
_ZONES: list = [dict(zone) for zone in DEFAULT_ZONES]
_BUFFER = 2000.0
_CORRIDOR = 25.0
_BRUSHES: list = []
_APPLIED_REVISION = 0
_APPLIED_SEGMENTS: list = []
_APPLIED_ZONES: list = [dict(zone) for zone in DEFAULT_ZONES]
_APPLIED_BUFFER = 2000.0
_APPLIED_CORRIDOR = 25.0
_APPLIED_BRUSHES: list = []
_REFERENCE: dict | None = None
_APPLIED_REFERENCE: dict | None = None
_REFERENCE_PATH = Path(tempfile.gettempdir()) / "blender_lidartool_reference"
_REFERENCE_TYPE = "image/png"
_MAX_REFERENCE = 40_000_000
_OSRM = "https://router.project-osrm.org"
_PROFILE = "driving"
_PROFILES = ("driving", "cycling", "walking")


def normalize_segments(payload) -> list:
    if isinstance(payload, dict):
        payload = payload.get("segments", [])
    if not isinstance(payload, list):
        raise ValueError("Trať nemá seznam úseků.")
    segments = []
    for segment in payload:
        if not isinstance(segment, list):
            raise ValueError("Úsek tratě je neplatný.")
        points = []
        for point in segment:
            if not isinstance(point, dict):
                raise ValueError("Bod tratě je neplatný.")
            try:
                lat = float(point["lat"])
                lon = float(point["lon"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Bod tratě nemá platnou šířku a délku.") from exc
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError("Souřadnice bodu jsou mimo rozsah.")
            points.append({"lat": lat, "lon": lon})
        if points:
            segments.append(points)
    return segments


def normalize_zones(raw) -> list:
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError("Jsou potřeba čtyři zóny.")
    zones = []
    last = 0.0
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("Zóna je neplatná.")
        try:
            step = float(item.get("step_m"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Krok zóny musí být číslo.") from exc
        if step <= 0:
            raise ValueError("Krok zóny musí být větší než nula.")
        radius = item.get("radius_m")
        if radius in (None, "", 0, 0.0):
            radius_m = None
        else:
            try:
                radius_m = float(radius)
            except (TypeError, ValueError) as exc:
                raise ValueError("Poloměr zóny musí být číslo.") from exc
            if radius_m <= 0:
                raise ValueError("Poloměr zóny musí být větší než nula.")
            if radius_m < last:
                raise ValueError("Poloměry zón musí růst od silnice ven.")
            last = radius_m
        zones.append({"id": index, "radius_m": radius_m, "step_m": step})
    return zones


def normalize_corridor(value) -> float:
    try:
        corridor_m = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Koridor silnice musí být číslo.") from exc
    if corridor_m < 5:
        raise ValueError("Koridor silnice musí být aspoň 5 m.")
    return corridor_m


def normalize_buffer(value) -> float:
    try:
        buffer_m = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Hranice terénu musí být číslo.") from exc
    if buffer_m <= 0:
        raise ValueError("Hranice terénu musí být větší než nula.")
    return buffer_m


def normalize_brushes(raw) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Štětec je neplatný.")
    stamps = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Otisk štětce je neplatný.")
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
            radius_m = float(item["radius_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Otisk štětce nemá platné souřadnice.") from exc
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError("Otisk štětce je mimo rozsah souřadnic.")
        if not 1.0 <= radius_m <= 500.0:
            raise ValueError("Poloměr štětce musí být mezi 1 a 500 m.")
        target = item.get("target")
        if target in (None, "", 1, "1"):
            target = "1"
        else:
            target = str(target).strip()
            if target not in {"1", "2", "3", "4", "corridor"}:
                raise ValueError("Cíl štětce musí být zóna 1 až 4 nebo koridor silnice.")
        stamps.append(
            {"lat": lat, "lon": lon, "radius_m": radius_m, "erase": bool(item.get("erase")), "target": target}
        )
    if len(stamps) > 4000:
        raise ValueError("Štětec má příliš mnoho otisků.")
    return stamps


def sniff_image(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def parse_archive_link(url: str) -> dict | None:
    parsed = urlparse(url)
    if "openmap.html" not in parsed.path:
        return None
    query = parse_qs(parsed.query)
    center = (query.get("bz") or [""])[0].split(",")
    if len(center) != 2:
        return None
    try:
        x_coord = float(center[0])
        y_coord = float(center[1])
    except ValueError:
        return None
    return {
        "id": (query.get("idrastru") or [""])[0],
        "x": x_coord,
        "y": y_coord,
    }


def normalize_reference(raw) -> dict | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Předloha je neplatná.")
    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
        rotation = float(raw.get("rotation") or 0)
        scale = float(raw.get("scale") or 1)
        opacity = float(0.6 if raw.get("opacity") is None else raw.get("opacity"))
        width_m = float(raw["width_m"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Předloha nemá platnou polohu.") from exc
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("Předloha je mimo rozsah souřadnic.")
    if not 0.05 <= scale <= 20:
        raise ValueError("Měřítko předlohy musí být mezi 0,05 a 20.")
    if not 0.0 <= opacity <= 1.0:
        raise ValueError("Průhlednost předlohy musí být mezi 0 a 1.")
    if width_m <= 0:
        raise ValueError("Šířka předlohy musí být větší než nula.")
    return {
        "lat": lat,
        "lon": lon,
        "rotation": rotation,
        "scale": scale,
        "opacity": opacity,
        "width_m": width_m,
    }


def clean_profile(value) -> str:
    profile = str(value or "").strip()
    return profile if profile in _PROFILES else "driving"


def open_session(
    segments: list,
    zones: list | None = None,
    terrain_buffer_m: float = 2000.0,
    brushes: list | None = None,
    reference: dict | None = None,
    osrm_url: str = "https://router.project-osrm.org",
    profile: str = "driving",
    road_buffer_m: float = 25.0,
) -> str:
    global _TOKEN, _SEGMENTS, _ZONES, _BUFFER, _CORRIDOR, _BRUSHES, _REFERENCE, _OSRM, _PROFILE
    cleaned = normalize_segments(segments)
    cleaned_zones = normalize_zones(zones if zones is not None else DEFAULT_ZONES)
    cleaned_buffer = normalize_buffer(terrain_buffer_m)
    cleaned_brushes = normalize_brushes(brushes)
    cleaned_reference = normalize_reference(reference) if reference else _REFERENCE
    with _LOCK:
        _TOKEN = uuid.uuid4().hex
        _SEGMENTS = cleaned
        _ZONES = cleaned_zones
        _BUFFER = cleaned_buffer
        _CORRIDOR = normalize_corridor(road_buffer_m)
        _BRUSHES = cleaned_brushes
        if cleaned_reference is not None:
            _REFERENCE = cleaned_reference
        _PROFILE = clean_profile(profile)
        osrm = str(osrm_url or "").strip()
        _OSRM = osrm if osrm.startswith(("http://", "https://")) else "https://router.project-osrm.org"
        token = _TOKEN
    _ensure_server()
    assert _SERVER is not None
    port = _SERVER.server_address[1]
    return f"http://127.0.0.1:{port}/?token={token}"


def latest_apply() -> tuple[int, list, list, float, list, dict | None, float]:
    with _LOCK:
        return (
            _APPLIED_REVISION,
            [list(segment) for segment in _APPLIED_SEGMENTS],
            [dict(zone) for zone in _APPLIED_ZONES],
            _APPLIED_BUFFER,
            [dict(stamp) for stamp in _APPLIED_BRUSHES],
            None if _APPLIED_REFERENCE is None else dict(_APPLIED_REFERENCE),
            _APPLIED_CORRIDOR,
        )


def reference_path() -> Path:
    return _REFERENCE_PATH


def restore_reference_file(path: Path) -> None:
    global _REFERENCE_TYPE
    if _REFERENCE_PATH.is_file() or not path.is_file():
        return
    _REFERENCE_PATH.write_bytes(path.read_bytes())
    _REFERENCE_TYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower(), "image/png")


def reference_type() -> str:
    return _REFERENCE_TYPE


def stop() -> None:
    global _SERVER
    server = _SERVER
    _SERVER = None
    if server is not None:
        threading.Thread(target=server.shutdown, daemon=True).start()


def _ensure_server() -> None:
    global _SERVER
    if _SERVER is not None:
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _SERVER = server


def _run_track_edit(action: str, segments: list, profile: str, osrm: str) -> tuple[list, str]:
    try:
        from blender_lidartool.runtime import venv_python, venv_ready, worker_env
    except ImportError as exc:
        raise RuntimeError("Zjednodušení a přichycení běží jen v Blenderu s připraveným prostředím.") from exc
    if not venv_ready():
        raise RuntimeError("Nejdřív v Blenderu připravte prostředí addonu.")
    folder = Path(tempfile.gettempdir()) / "blender_lidartool_edit"
    folder.mkdir(parents=True, exist_ok=True)
    track = folder / "track_in.json"
    result = folder / "result.json"
    status = folder / "status.json"
    track.write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
    if status.exists():
        status.unlink()
    command = [
        str(venv_python()),
        "-m",
        "blender_lidartool.worker",
        "--status",
        str(status),
        action,
        "--track",
        str(track),
        "--out",
        str(result),
    ]
    if action == "snap":
        command.extend(["--osrm", osrm, "--profile", profile])
    try:
        completed = subprocess.run(
            command,
            env=worker_env(),
            capture_output=True,
            timeout=55,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Úprava tratě trvala příliš dlouho.") from exc
    if completed.returncode != 0 or not result.is_file():
        detail = ""
        if status.is_file():
            try:
                detail = json.loads(status.read_text(encoding="utf-8")).get("error") or ""
            except json.JSONDecodeError:
                detail = ""
        if not detail:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[-400:]
        raise RuntimeError(detail or "Úprava tratě selhala.")
    payload = json.loads(result.read_text(encoding="utf-8"))
    updated = normalize_segments(payload.get("segments"))
    if not updated or len(updated[0]) < 2:
        raise RuntimeError("Úprava tratě nevrátila použitelnou linii.")
    return updated, str(payload.get("message") or "Trať je upravená.")


def _command_executable(command: str) -> Path | None:
    text = command.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            return Path(text[1:end])
    part = text.split(" ", 1)[0]
    return Path(part) if part else None


def _default_browser_command() -> str:
    if os.name != "nt":
        return ""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return ""
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"Software\Classes\{prog_id}\shell\open\command") as key:
                command, _ = winreg.QueryValueEx(key, "")
                return str(command)
        except OSError:
            continue
    return ""


def _chromium_executable() -> Path | None:
    command = _default_browser_command().lower()
    if "chrome.exe" in command or "msedge.exe" in command:
        path = _command_executable(_default_browser_command())
        if path is not None and path.is_file():
            return path
    roots = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    relatives = (
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    )
    for root in roots:
        if not root:
            continue
        for relative in relatives:
            candidate = Path(root) / relative
            if candidate.is_file():
                return candidate
    return None


def open_browser(url: str) -> None:
    executable = _chromium_executable()
    if executable is not None:
        subprocess.Popen(
            [str(executable), f"--app={url}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    import webbrowser

    webbrowser.open(url)


def _download_reference(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Odkaz na předlohu musí začínat na http nebo https.")
    request = Request(url, headers={"User-Agent": "TrackTerrain-Blender/0.1"})
    try:
        with urlopen(request, timeout=40) as response:
            raw = response.read(_MAX_REFERENCE + 1)
    except URLError as exc:
        raise ValueError("Předlohu se nepodařilo stáhnout.") from exc
    if len(raw) > _MAX_REFERENCE:
        raise ValueError("Obrázek předlohy je větší než 40 MB.")
    return raw


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, EDITOR_HTML.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/session":
            query = parse_qs(urlparse(self.path).query)
            token = (query.get("token") or [""])[0]
            with _LOCK:
                if token != _TOKEN:
                    self._send_json(409, {"detail": "Editor byl otevřený znovu. Obnovte stránku."})
                    return
                payload = {
                    "token": _TOKEN,
                    "segments": _SEGMENTS,
                    "zones": _ZONES,
                    "terrain_buffer_m": _BUFFER,
                    "road_buffer_m": _CORRIDOR,
                    "zone1_brush": _BRUSHES,
                    "reference": None if _REFERENCE is None or not _REFERENCE_PATH.is_file() else _REFERENCE,
                    "reference_ready": _REFERENCE_PATH.is_file(),
                    "profile": _PROFILE,
                }
            self._send_json(200, payload)
            return
        if path == "/api/reference":
            if not _REFERENCE_PATH.is_file():
                self._send_json(404, {"detail": "Předloha není vložená."})
                return
            self._send(200, _REFERENCE_PATH.read_bytes(), _REFERENCE_TYPE)
            return
        if path == "/api/geocode":
            query = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            try:
                self._send_json(200, {"results": _geocode(query)})
            except ValueError as exc:
                self._send_json(400, {"detail": str(exc)})
            except URLError:
                self._send_json(502, {"detail": "Vyhledání adresy se nezdařilo."})
            return
        if self._send_static(path):
            return
        self._send_json(404, {"detail": "Stránka neexistuje."})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/reference":
            self._save_reference()
            return
        if path == "/api/track/edit":
            self._edit_track()
            return
        if path != "/api/track":
            self._send_json(404, {"detail": "Stránka neexistuje."})
            return
        global _APPLIED_REVISION, _APPLIED_SEGMENTS, _APPLIED_ZONES, _APPLIED_BUFFER, _APPLIED_CORRIDOR, _APPLIED_BRUSHES, _APPLIED_REFERENCE
        global _SEGMENTS, _ZONES, _BUFFER, _CORRIDOR, _BRUSHES, _REFERENCE
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
            token = str(body.get("token") or "")
            segments = normalize_segments(body)
            zones = normalize_zones(body.get("zones"))
            terrain_buffer_m = normalize_buffer(body.get("terrain_buffer_m"))
            corridor_m = normalize_corridor(body["road_buffer_m"]) if "road_buffer_m" in body else _CORRIDOR
            brushes = normalize_brushes(body.get("zone1_brush"))
            reference = normalize_reference(body.get("reference"))
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            self._send_json(400, {"detail": str(exc) if isinstance(exc, ValueError) else "Neplatná trať."})
            return
        if not segments or len(segments[0]) < 2:
            self._send_json(400, {"detail": "Trať potřebuje aspoň dva body."})
            return
        with _LOCK:
            if token != _TOKEN:
                self._send_json(409, {"detail": "Editor byl otevřený znovu. Obnovte stránku."})
                return
            _SEGMENTS = segments
            _ZONES = zones
            _BUFFER = terrain_buffer_m
            _CORRIDOR = corridor_m
            _BRUSHES = brushes
            _REFERENCE = reference
            _APPLIED_SEGMENTS = segments
            _APPLIED_ZONES = zones
            _APPLIED_BUFFER = terrain_buffer_m
            _APPLIED_CORRIDOR = corridor_m
            _APPLIED_BRUSHES = brushes
            _APPLIED_REFERENCE = reference
            _APPLIED_REVISION += 1
            revision = _APPLIED_REVISION
        message = "Trať a zóny jsou ve scéně Blenderu."
        if reference:
            message = "Trať a zóny jsou ve scéně. Předlohu vložíte v Blenderu tlačítkem Vložit referenci."
        self._send_json(200, {"revision": revision, "message": message})

    def _edit_track(self) -> None:
        global _SEGMENTS
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
            token = str(body.get("token") or "")
            action = str(body.get("action") or "")
            segments = normalize_segments(body)
            profile = clean_profile(body.get("profile") or _PROFILE)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            self._send_json(400, {"detail": str(exc) if isinstance(exc, ValueError) else "Neplatná trať."})
            return
        if action not in ("simplify", "snap"):
            self._send_json(400, {"detail": "Neznámá úprava tratě."})
            return
        if not segments or len(segments[0]) < 2:
            self._send_json(400, {"detail": "Trať potřebuje aspoň dva body."})
            return
        with _LOCK:
            if token != _TOKEN:
                self._send_json(409, {"detail": "Editor byl otevřený znovu. Obnovte stránku."})
                return
            osrm = _OSRM
        try:
            updated, message = _run_track_edit(action, segments, profile, osrm)
        except RuntimeError as exc:
            self._send_json(400, {"detail": str(exc)})
            return
        with _LOCK:
            if token != _TOKEN:
                self._send_json(409, {"detail": "Editor byl otevřený znovu. Obnovte stránku."})
                return
            _SEGMENTS = updated
        self._send_json(200, {"segments": updated, "message": message})

    def _save_reference(self) -> None:
        global _REFERENCE_TYPE
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > _MAX_REFERENCE:
            self._send_json(400, {"detail": "Obrázek předlohy je prázdný nebo větší než 40 MB."})
            return
        raw = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type == "application/json":
            try:
                body = json.loads(raw.decode("utf-8"))
                url = str(body.get("url") or "").strip()
            except (json.JSONDecodeError, UnicodeError):
                self._send_json(400, {"detail": "Neplatný odkaz na předlohu."})
                return
            if parse_archive_link(url):
                self._send_json(
                    400,
                    {
                        "detail": (
                            "Odkaz vede na prohlížeč archiválií, ne na samotný snímek. "
                            "V prohlížeči snímek uložte a vložte ho jako soubor."
                        )
                    },
                )
                return
            try:
                raw = _download_reference(url)
            except ValueError as exc:
                self._send_json(400, {"detail": str(exc)})
                return
        kind = sniff_image(raw)
        if kind is None:
            self._send_json(400, {"detail": "Soubor není obrázek PNG, JPEG ani WEBP."})
            return
        _REFERENCE_PATH.write_bytes(raw)
        _REFERENCE_TYPE = kind
        self._send_json(200, {"message": "Předloha je vložená."})

    def log_message(self, format, *args):
        return

    def _send_static(self, url_path: str) -> bool:
        relative = url_path.lstrip("/")
        if not relative or "\\" in relative:
            return False
        posix = Path(relative).as_posix()
        if posix not in {"index.html", "help.md"} and not posix.startswith(("css/", "js/", "vendor/")):
            return False
        target = (EDITOR_ROOT / posix).resolve()
        try:
            target.relative_to(EDITOR_ROOT.resolve())
        except ValueError:
            return False
        if not target.is_file():
            return False
        types = {
            ".css": "text/css; charset=utf-8",
            ".gif": "image/gif",
            ".html": "text/html; charset=utf-8",
            ".ico": "image/x-icon",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".webp": "image/webp",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }
        self._send(200, target.read_bytes(), types.get(target.suffix.lower(), "application/octet-stream"))
        return True

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


def _geocode(query: str) -> list[dict]:
    text = (query or "").strip()
    if len(text) < 2:
        raise ValueError("Zadejte aspoň dva znaky adresy.")
    params = urlencode(
        {
            "q": text,
            "format": "jsonv2",
            "limit": 6,
            "countrycodes": "cz",
            "addressdetails": 1,
        }
    )
    request = Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "TrackTerrain-Blender/0.1", "Accept-Language": "cs"},
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    results = []
    for item in payload or []:
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        label = (item.get("display_name") or "").strip()
        if label:
            results.append({"label": label, "lat": lat, "lon": lon})
    if not results:
        raise ValueError("Adresa se nenašla. Zkuste obec, ulici a číslo popisné.")
    return results
