from __future__ import annotations

import array
import json
from pathlib import Path

import bpy
import numpy as np


def _packed(values, typecode: str, dtype) -> array.array:
    flat = np.ascontiguousarray(values, dtype=dtype).reshape(-1)
    packed = array.array(typecode)
    packed.frombytes(flat.tobytes())
    return packed


def _floats(values) -> array.array:
    return _packed(values, "f", np.float32)


def _ints(values) -> array.array:
    return _packed(values, "i", np.int32)


def insert_terrain(context, output: Path) -> None:
    mesh_path = output / "mesh.npz"
    if not mesh_path.exists():
        raise ValueError("Nejdřív vložte terén.")
    data = np.load(mesh_path)
    vertices = np.asarray(data["vertices"], dtype=np.float32)
    faces = np.asarray(data["faces"], dtype=np.int32)
    layers = _read_layers(output)
    _remove_object("Teren")
    mesh = bpy.data.meshes.new("Teren")
    mesh.vertices.add(len(vertices))
    mesh.vertices.foreach_set("co", _floats(vertices))
    mesh.loops.add(len(faces) * 3)
    mesh.loops.foreach_set("vertex_index", _ints(faces))
    mesh.polygons.add(len(faces))
    mesh.polygons.foreach_set("loop_start", _ints(np.arange(0, len(faces) * 3, 3)))
    mesh.polygons.foreach_set("loop_total", _ints(np.full(len(faces), 3)))
    if layers:
        uv_all = np.zeros((len(faces), 3, 2), dtype=np.float32)
        material_index = np.zeros(len(faces), dtype=np.int32)
        for slot, layer in enumerate(layers):
            rows = np.asarray(layer["rows"], dtype=np.int32)
            uv_all[rows] = np.asarray(layer["uv"], dtype=np.float32)
            material_index[rows] = slot
            mesh.materials.append(_textured_material(layer))
        uv_layer = mesh.uv_layers.new(name="Ortofoto")
        uv_layer.data.foreach_set("uv", _floats(uv_all))
        mesh.polygons.foreach_set("material_index", _ints(material_index))
    else:
        mesh.materials.append(_plain_material())
    mesh.update(calc_edges=True)
    mesh.validate()
    _link(context, "Teren", mesh)
    show_material(context)


def insert_driveline(context, output: Path) -> None:
    path = output / "driveline.npz"
    if not path.exists():
        raise ValueError("Osa ještě není spočítaná.")
    data = np.load(path)
    count = int(np.asarray(data["count"]).reshape(-1)[0])
    lines = [np.asarray(data[f"line_{index}"], dtype=np.float32) for index in range(count)]
    _remove_object("Osa")
    curve = bpy.data.curves.new("Osa", type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = 0.35
    curve.bevel_resolution = 2
    curve.materials.append(_axis_material())
    for line in lines:
        if len(line) < 2:
            continue
        spline = curve.splines.new("POLY")
        spline.points.add(len(line) - 1)
        coords = np.ones((len(line), 4), dtype=np.float32)
        coords[:, :3] = line
        spline.points.foreach_set("co", _floats(coords))
    _link(context, "Osa", curve)
    show_material(context)


def insert_buildings(context, output: Path) -> None:
    manifest_path = output / "buildings.json"
    if not manifest_path.is_file():
        raise ValueError("Budovy se nepodařilo uložit.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("buildings") or []
    if not items:
        raise ValueError("Z vybraných půdorysů nevznikla žádná budova.")
    collection = _buildings_collection(context)
    folder = (output / "buildings").resolve()
    lookup = _roof_lookup(output)
    roofs = False
    for item in items:
        building_id = str(item.get("id") or "").strip()
        if not building_id:
            continue
        path = (output / str(item.get("file") or "")).resolve()
        try:
            path.relative_to(folder)
        except ValueError as exc:
            raise ValueError("Soubor budovy je mimo výstup.") from exc
        if not path.is_file():
            raise ValueError("Soubor budovy chybí.")
        data = np.load(path)
        vertices = np.asarray(data["vertices"], dtype=np.float32)
        faces = np.asarray(data["faces"], dtype=np.int32)
        if len(vertices) < 3 or len(faces) == 0:
            continue
        _remove_building(building_id)
        name = _building_name(str(item.get("name") or ""), building_id)
        mesh = bpy.data.meshes.new(name)
        mesh.vertices.add(len(vertices))
        mesh.vertices.foreach_set("co", _floats(vertices))
        mesh.loops.add(len(faces) * 3)
        mesh.loops.foreach_set("vertex_index", _ints(faces))
        mesh.polygons.add(len(faces))
        mesh.polygons.foreach_set("loop_start", _ints(np.arange(0, len(faces) * 3, 3)))
        mesh.polygons.foreach_set("loop_total", _ints(np.full(len(faces), 3)))
        mesh.update(calc_edges=True)
        mesh.validate()
        _shade_building(mesh)
        if _apply_roof_texture(mesh, output, lookup):
            roofs = True
        obj = bpy.data.objects.new(name, mesh)
        obj["lidar_building_id"] = building_id
        collection.objects.link(obj)
        try:
            obj.select_set(True)
            context.view_layer.objects.active = obj
        except RuntimeError:
            pass
    if roofs:
        show_material(context)


def apply_building_roofs(context, output: Path) -> tuple[int, int]:
    """Doplní střechy už vložených budov z ortofota terénu. Zvětšený snímek má přednost."""
    lookup = _roof_lookup(output)
    if lookup is None:
        raise ValueError("Nejdřív položte ortofoto na terén.")
    _terrain_xy, _terrain_faces, _terrain_uv, _terrain_slot, materials = lookup
    if not any(material is not None for material in materials):
        raise ValueError("Soubor ortofota chybí. Nejdřív ho položte na terén.")
    buildings = [
        obj for obj in bpy.data.objects
        if obj.get("lidar_building_id") and obj.type == "MESH" and obj.data is not None
    ]
    if not buildings:
        raise ValueError("Nejdřív vložte budovy.")
    painted = 0
    for obj in buildings:
        if _apply_roof_texture(obj.data, output, lookup):
            painted += 1
    if painted == 0:
        raise ValueError("Střechy nezasahují do ortofota terénu.")
    show_material(context)
    return painted, len(buildings) - painted


def _apply_roof_texture(mesh, output: Path, lookup=None) -> bool:
    from blender_lidartool.engine.roofuv import project_roof_uv

    if len(mesh.materials) == 0:
        mesh.materials.append(_building_material())
    if lookup is None:
        lookup = _roof_lookup(output)
    if lookup is None:
        return False
    terrain_xy, terrain_faces, terrain_uv, terrain_slot, materials = lookup
    triangles = _mesh_triangles(mesh)
    if len(triangles) == 0:
        return False
    uv, slots = project_roof_uv(
        _mesh_coords(mesh), triangles, terrain_xy, terrain_faces, terrain_uv, terrain_slot
    )
    return _paint_roof_faces(mesh, uv, slots, materials)


def _roof_lookup(output: Path):
    layers = _read_layers(output)
    terrain_path = output / "mesh.npz"
    if not layers or not terrain_path.is_file():
        return None
    terrain = np.load(terrain_path)
    terrain_xy = np.asarray(terrain["vertices"], dtype=np.float64)[:, :2]
    terrain_faces = np.asarray(terrain["faces"], dtype=np.int32)
    face_slot = np.full(len(terrain_faces), -1, dtype=np.int32)
    face_uv = np.zeros((len(terrain_faces), 3, 2), dtype=np.float32)
    for slot, layer in enumerate(layers):
        rows = np.asarray(layer["rows"], dtype=np.int32)
        uv = np.asarray(layer["uv"], dtype=np.float32)
        if len(rows) != len(uv):
            continue
        valid = (rows >= 0) & (rows < len(terrain_faces))
        face_slot[rows[valid]] = slot
        face_uv[rows[valid]] = uv[valid]
    if not np.any(face_slot >= 0):
        return None
    materials = [_material_for_layer(layer) for layer in layers]
    if not any(material is not None for material in materials):
        return None
    return terrain_xy, terrain_faces, face_uv, face_slot, materials


def _material_for_layer(layer: dict):
    path = Path(str(layer.get("image") or ""))
    if not path.is_file():
        return None
    found = _material_using_image(path)
    if found is not None:
        _reload_material_image(found, path)
        return found
    return _textured_material(layer)


def _material_using_image(path: Path):
    for material in bpy.data.materials:
        if material is None or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            image = getattr(node, "image", None)
            if image is None or not image.filepath:
                continue
            if _same_file(bpy.path.abspath(image.filepath), path):
                return material
    return None


def _reload_material_image(material, path: Path) -> None:
    if material.node_tree is None:
        return
    for node in material.node_tree.nodes:
        image = getattr(node, "image", None)
        if image is None or not image.filepath:
            continue
        if not _same_file(bpy.path.abspath(image.filepath), path):
            continue
        try:
            image.reload()
        except (RuntimeError, OSError):
            pass


def _same_file(left, right) -> bool:
    try:
        return Path(left).resolve().samefile(right)
    except OSError:
        return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()


def _mesh_coords(mesh) -> np.ndarray:
    flat = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", flat)
    return flat.reshape(-1, 3)


def _mesh_triangles(mesh) -> np.ndarray:
    count = len(mesh.polygons)
    if count == 0:
        return np.zeros((0, 3), dtype=np.int32)
    totals = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    if not np.all(totals == 3):
        return np.zeros((0, 3), dtype=np.int32)
    loops = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", loops)
    starts = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    return np.stack((loops[starts], loops[starts + 1], loops[starts + 2]), axis=1).astype(np.int32)


def _paint_roof_faces(mesh, uv, slots, materials) -> bool:
    import bmesh

    slots = np.asarray(slots)
    if not np.any(slots >= 0):
        return False
    if len(mesh.materials) == 0:
        mesh.materials.append(_building_material())
    slot_index = {}
    for slot, material in enumerate(materials):
        if material is None or not np.any(slots == slot):
            continue
        slot_index[slot] = _material_slot(mesh, material)
    if not slot_index:
        return False
    saved = list(mesh.materials)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    layer = bm.loops.layers.uv.get("Ortofoto") or bm.loops.layers.uv.new("Ortofoto")
    for face in bm.faces:
        index = face.index
        if index >= len(slots):
            continue
        material_index = slot_index.get(int(slots[index]))
        if material_index is None:
            continue
        face.material_index = material_index
        corner = uv[index]
        for loop_index, loop in enumerate(face.loops):
            if loop_index >= 3:
                break
            loop[layer].uv = (float(corner[loop_index, 0]), float(corner[loop_index, 1]))
    bm.to_mesh(mesh)
    bm.free()
    _restore_materials(mesh, saved)
    uv_layer = mesh.uv_layers.get("Ortofoto")
    if uv_layer is not None:
        uv_layer.active = True
        uv_layer.active_render = True
    mesh.update()
    return True


def _restore_materials(mesh, materials) -> None:
    mesh.materials.clear()
    for material in materials:
        mesh.materials.append(material)


def orient_existing_buildings():
    """Srovná normály budov, které už ve scéně jsou. Nové vložení to dělá samo."""
    try:
        for obj in list(bpy.data.objects):
            if obj.get("lidar_building_id") and obj.type == "MESH" and obj.data is not None:
                _shade_building(obj.data)
    except Exception:
        return None
    return None


def _shade_building(mesh) -> None:
    """Střecha je spojitá, hrana mezi střechou a stěnou zůstane ostrá. Normály míří ven."""
    import bmesh

    saved = list(mesh.materials)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.normal_update()
    _point_normals_out(bm)
    bm.normal_update()
    for edge in bm.edges:
        faces = edge.link_faces
        if len(faces) < 2:
            edge.smooth = False
            continue
        edge.smooth = faces[0].normal.dot(faces[1].normal) > 0.75
    for face in bm.faces:
        face.smooth = True
    bm.to_mesh(mesh)
    bm.free()
    _restore_materials(mesh, saved)


def _point_normals_out(bm) -> None:
    from mathutils import Vector

    if not bm.verts or not bm.faces:
        return
    center = Vector((0.0, 0.0, 0.0))
    for vert in bm.verts:
        center += vert.co
    center /= len(bm.verts)
    walls = []
    roofs = []
    wall_score = 0.0
    roof_up = 0
    roof_down = 0
    for face in bm.faces:
        if face.normal.z > 0.55:
            roofs.append(face)
            roof_up += 1
        elif face.normal.z < -0.55:
            roofs.append(face)
            roof_down += 1
        else:
            walls.append(face)
            direction = face.calc_center_median() - center
            direction.z = 0.0
            wall_score += face.normal.dot(direction)
    if roof_down > roof_up:
        for face in roofs:
            if face.normal.z < 0.0:
                face.normal_flip()
    if wall_score < 0.0:
        for face in walls:
            face.normal_flip()


def _buildings_collection(context):
    collection = bpy.data.collections.get("Budovy")
    if collection is None:
        collection = bpy.data.collections.new("Budovy")
    try:
        context.scene.collection.children.link(collection)
    except RuntimeError:
        pass
    return collection


def facade_targets(context):
    """Vybrané budovy ze editoru. Když výběr ve scéně není, vezmou se všechny vložené."""
    objects = [
        obj for obj in bpy.data.objects
        if obj.get("lidar_building_id") and obj.type == "MESH" and obj.data is not None
    ]
    chosen = _selected_building_ids(context)
    if chosen:
        matched = [obj for obj in objects if str(obj.get("lidar_building_id")) in chosen]
        if matched:
            objects = matched
    tall = []
    low = []
    for obj in objects:
        if _is_low_building(obj):
            low.append(obj)
        else:
            tall.append(obj)
    return tall, low


def apply_facade(context, output: Path) -> tuple[int, int]:
    """Nalepí fasádu na vyšší budovy a plech na nižší. Původní jednobarevný materiál sundá."""
    from blender_lidartool.engine.facade import METAL_TILE_M, TILE_M, assign_variants

    tall, low = facade_targets(context)
    if not tall and not low:
        raise ValueError("Nejdřív vložte budovy.")
    facade_paths = sorted(output.glob("facade_*.png"))
    metal_paths = sorted(output.glob("metal_*.png"))
    houses = _paint_group(tall, facade_paths, "facade", TILE_M)
    sheds = _paint_group(low, metal_paths, "metal", METAL_TILE_M)
    if houses + sheds == 0:
        raise ValueError("U budov nejsou stěny, na které jde fasádu nalepit.")
    return houses, sheds


def _paint_group(buildings, paths, kind: str, tile: float) -> int:
    from blender_lidartool.engine.facade import assign_variants

    if not buildings:
        return 0
    if not paths:
        raise ValueError("Textura fasády se neuložila.")
    points = np.array([_building_xy(obj) for obj in buildings], dtype=np.float64)
    choice = assign_variants(points, len(paths))
    materials = [_facade_material(path, index, kind) for index, path in enumerate(paths)]
    painted = 0
    for obj, variant in zip(buildings, choice):
        if _paint_facade(obj.data, materials[int(variant)], tile):
            painted += 1
    return painted


def _paint_facade(mesh, material, tile: float) -> bool:
    import bmesh
    from mathutils import Vector

    old = list(mesh.materials)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bm.normal_update()
    layer = bm.loops.layers.uv.get("Ortofoto") or bm.loops.layers.uv.new("Ortofoto")
    walls = []
    roof_material = {}
    for face in bm.faces:
        if abs(face.normal.z) > 0.55:
            index = face.material_index
            current = old[index] if 0 <= index < len(old) else None
            if current is not None and not _is_plain_wall_material(current):
                roof_material[face.index] = current
            continue
        walls.append(face.index)
        if abs(face.normal.x) >= abs(face.normal.y):
            axis = Vector((0.0, 1.0, 0.0))
        else:
            axis = Vector((1.0, 0.0, 0.0))
        for loop in face.loops:
            co = loop.vert.co
            loop[layer].uv = (co.dot(axis) / tile, co.z / tile)
    if not walls:
        bm.free()
        return False
    bm.to_mesh(mesh)
    bm.free()
    kept = []
    seen = set()
    for current in roof_material.values():
        if id(current) in seen:
            continue
        seen.add(id(current))
        kept.append(current)
    materials = kept + [material]
    wall_slot = len(materials) - 1
    slot_of = {id(current): index for index, current in enumerate(kept)}
    mesh.materials.clear()
    for current in materials:
        mesh.materials.append(current)
    indices = np.full(len(mesh.polygons), wall_slot, dtype=np.int32)
    for face_index, current in roof_material.items():
        if face_index < len(indices):
            indices[face_index] = slot_of[id(current)]
    mesh.polygons.foreach_set("material_index", _ints(indices))
    uv_layer = mesh.uv_layers.get("Ortofoto")
    if uv_layer is not None:
        uv_layer.active = True
        if hasattr(uv_layer, "active_render"):
            uv_layer.active_render = True
    mesh.update()
    return True


def _is_plain_wall_material(material) -> bool:
    name = material.name or ""
    return name == "Budova" or name.startswith("Fasada") or name.startswith("Plech")


def _selected_building_ids(context) -> set[str]:
    raw = getattr(context.scene, "lidar_buildings", "") or "[]"
    try:
        features = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(features, list):
        return set()
    chosen = set()
    for item in features:
        if not isinstance(item, dict):
            continue
        fid = item.get("id") or (item.get("properties") or {}).get("id")
        fid = str(fid or "").strip()
        if fid:
            chosen.add(fid)
    return chosen


def _is_low_building(obj) -> bool:
    from blender_lidartool.engine.facade import LOW_BUILDING_M

    mesh = obj.data
    if mesh is None or len(mesh.vertices) == 0:
        return False
    flat = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", flat)
    heights = flat.reshape(-1, 3)[:, 2]
    scale = abs(float(obj.matrix_world.to_scale().z)) or 1.0
    return float(heights.max() - heights.min()) * scale < LOW_BUILDING_M


def _material_slot(mesh, material) -> int:
    for index, current in enumerate(mesh.materials):
        if current == material:
            return index
    mesh.materials.append(material)
    return len(mesh.materials) - 1


def _building_xy(obj) -> tuple[float, float]:
    from mathutils import Vector

    center = sum((Vector(corner) for corner in obj.bound_box), Vector()) / 8.0
    world = obj.matrix_world @ center
    return (float(world.x), float(world.y))


def _facade_material(image_path: Path, index: int, kind: str):
    name = f"Plech {index + 1}" if kind == "metal" else f"Fasada {index + 1}"
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
    material.use_nodes = True
    _double_sided(material)
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    shader = tree.nodes.new("ShaderNodeBsdfPrincipled")
    texture = tree.nodes.new("ShaderNodeTexImage")
    output.location = (420, 0)
    shader.location = (140, 0)
    texture.location = (-240, 0)
    image = bpy.data.images.load(str(image_path), check_existing=True)
    image.reload()
    try:
        image.colorspace_settings.name = "sRGB"
    except (TypeError, AttributeError):
        pass
    texture.image = image
    texture.extension = "REPEAT"
    texture.interpolation = "Linear"
    tree.links.new(texture.outputs["Color"], shader.inputs.get("Base Color") or shader.inputs[0])
    roughness = shader.inputs.get("Roughness")
    metallic = shader.inputs.get("Metallic")
    if kind == "metal":
        if roughness is not None:
            roughness.default_value = 0.38
        if metallic is not None:
            metallic.default_value = 0.85
    elif roughness is not None:
        roughness.default_value = 0.86
    tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    return material


def _building_material():
    material = bpy.data.materials.get("Budova")
    if material is None:
        material = bpy.data.materials.new("Budova")
        material.use_nodes = True
        _double_sided(material)
        shader = material.node_tree.nodes.get("Principled BSDF")
        if shader is not None:
            socket = shader.inputs.get("Base Color") or shader.inputs[0]
            socket.default_value = (0.72, 0.66, 0.58, 1.0)
    return material


def _building_name(name: str, building_id: str) -> str:
    label = " ".join((name or "").split())
    for char in '\\/:*?"<>|':
        label = label.replace(char, " ")
    label = " ".join(label.split())
    if not label:
        label = f"Budova {building_id}".strip()
    return label[:63] or "Budova"


def _remove_building(building_id: str) -> None:
    for obj in list(bpy.data.objects):
        if obj.get("lidar_building_id") != building_id:
            continue
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0 and isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)


def insert_reference(context, image_path: Path, placement: dict) -> None:
    _remove_object("Predloha")
    image = bpy.data.images.load(str(image_path), check_existing=True)
    image.reload()
    obj = bpy.data.objects.new("Predloha", None)
    obj.empty_display_type = "IMAGE"
    obj.data = image
    obj.empty_display_size = max(float(placement["width"]), 0.1)
    obj.empty_image_offset = (-0.5, -0.5)
    if hasattr(obj, "use_empty_image_alpha"):
        obj.use_empty_image_alpha = True
    opacity = float(placement.get("opacity", 0.6))
    obj.color = (1.0, 1.0, 1.0, opacity)
    try:
        obj.empty_image_side = "DOUBLE_SIDED"
    except TypeError:
        pass
    obj.location = (float(placement["x"]), float(placement["y"]), float(placement["z"]))
    obj.rotation_euler = (0.0, 0.0, float(placement["rotation"]))
    obj.show_in_front = True
    try:
        obj.empty_image_depth = "FRONT"
    except TypeError:
        pass
    collection = context.collection or context.scene.collection
    collection.objects.link(obj)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def remove_reference() -> None:
    _remove_object("Predloha")


def _find_terrain_object():
    for obj in bpy.data.objects:
        if obj.type == "MESH" and (obj.name == "Teren" or obj.name.startswith("Teren.")):
            return obj
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is not None and material.name.startswith("Teren"):
                return obj
    return None


def apply_ortho(context, output: Path) -> None:
    """Položí nové ortofoto na terén, který už ve scéně je, a mesh nevymění."""
    obj = _find_terrain_object()
    if obj is None or obj.data is None:
        raise ValueError("Ve scéně není terén.")
    layers = _read_layers(output)
    if not layers:
        raise ValueError("Ortofoto se nepodařilo uložit.")
    mesh = obj.data
    stored = output / "mesh.npz"
    stored_faces = int(np.load(stored)["faces"].shape[0]) if stored.is_file() else None
    if stored_faces is not None and len(mesh.polygons) != stored_faces:
        try:
            reload_ortho(context, output)
        except ValueError:
            raise ValueError("Ortofoto je stažené, ale terén ve scéně má jiný počet ploch, takže se na něj samo nepoložilo.") from None
        return
    mesh.materials.clear()
    uv_all = np.zeros((len(mesh.polygons), 3, 2), dtype=np.float32)
    material_index = np.zeros(len(mesh.polygons), dtype=np.int32)
    for slot, layer in enumerate(layers):
        rows = np.asarray(layer["rows"], dtype=np.int32)
        uv_all[rows] = np.asarray(layer["uv"], dtype=np.float32)
        material_index[rows] = slot
        mesh.materials.append(_textured_material(layer))
    uv_layer = mesh.uv_layers.get("Ortofoto") or mesh.uv_layers.new(name="Ortofoto")
    uv_layer.data.foreach_set("uv", _floats(uv_all))
    mesh.polygons.foreach_set("material_index", _ints(material_index))
    mesh.update()
    show_material(context)


def reload_ortho(context, output: Path) -> None:
    manifest_path = output / "layers.json"
    if not manifest_path.exists():
        raise ValueError("Nejdřív položte ortofoto na materiál.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    found = 0
    for item in manifest.get("zones") or []:
        path = str(Path(item["image"]).resolve())
        name = item.get("name") or f"teren_zona_{int(item['id'])}"
        image = bpy.data.images.get(name)
        if image is None:
            image = bpy.data.images.get(f"teren_zona_{int(item['id'])}")
        if image is None:
            continue
        image.filepath = path
        image.reload()
        found += 1
    if found == 0:
        raise ValueError("Ve scéně není materiál ortofota. Nejdřív spusťte Ortofoto na materiál.")
    show_material(context)


def show_material(context) -> None:
    for window in context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.shading.type = "MATERIAL"


def _read_layers(output: Path) -> list[dict]:
    manifest_path = output / "layers.json"
    data_path = output / "layers.npz"
    if not manifest_path.exists() or not data_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = np.load(data_path)
    layers = []
    for item in manifest["zones"]:
        zone_id = int(item["id"])
        key = str(item.get("key") or f"zone_{zone_id}")
        if f"{key}_rows" not in data:
            key = f"zone_{zone_id}"
        layers.append(
            {
                "zone_id": zone_id,
                "rows": data[f"{key}_rows"],
                "uv": data[f"{key}_uv"],
                "image": item["image"],
                "name": item.get("name") or f"teren_zona_{zone_id}",
                "part": int(item.get("part") or 0),
                "parts": int(item.get("parts") or 1),
            }
        )
    return layers


def _plain_material():
    material = bpy.data.materials.new("Teren")
    material.use_nodes = True
    _double_sided(material)
    shader = material.node_tree.nodes.get("Principled BSDF")
    if shader is not None:
        socket = shader.inputs.get("Base Color") or shader.inputs[0]
        socket.default_value = (0.45, 0.42, 0.32, 1.0)
    return material


def _textured_material(layer: dict):
    zone_id = int(layer["zone_id"])
    parts = int(layer.get("parts") or 1)
    part = int(layer.get("part") or 0)
    title = f"Teren zóna {zone_id}" if parts <= 1 else f"Teren zóna {zone_id} ({part + 1}/{parts})"
    material = bpy.data.materials.new(title)
    material.use_nodes = True
    _double_sided(material)
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    texture = tree.nodes.new("ShaderNodeTexImage")
    image = bpy.data.images.load(layer["image"], check_existing=True)
    image.reload()
    desired = str(layer.get("name") or f"teren_zona_{zone_id}")
    if image.name != desired and bpy.data.images.get(desired) is None:
        image.name = desired
    texture.image = image
    if shader is not None:
        socket = shader.inputs.get("Base Color") or shader.inputs[0]
        tree.links.new(texture.outputs["Color"], socket)
    return material


def _double_sided(material) -> None:
    if hasattr(material, "use_backface_culling"):
        material.use_backface_culling = False


def _axis_material():
    material = bpy.data.materials.new("Osa")
    material.use_nodes = True
    shader = material.node_tree.nodes.get("Principled BSDF")
    if shader is not None:
        socket = shader.inputs.get("Base Color") or shader.inputs[0]
        socket.default_value = (0.9, 0.35, 0.05, 1.0)
    return material


def _link(context, name: str, data) -> None:
    obj = bpy.data.objects.new(name, data)
    collection = context.collection or context.scene.collection
    collection.objects.link(obj)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _remove_object(name: str) -> None:
    obj = bpy.data.objects.get(name)
    if obj is None:
        return
    data = obj.data
    materials = list(data.materials) if data is not None else []
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        if isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)
        elif isinstance(data, bpy.types.Curve):
            bpy.data.curves.remove(data)
    for material in materials:
        if material is None or material.users != 0:
            continue
        images = []
        if material.use_nodes and material.node_tree is not None:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image is not None:
                    images.append(node.image)
        bpy.data.materials.remove(material)
        for image in images:
            if image.users == 0:
                bpy.data.images.remove(image)
