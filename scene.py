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
