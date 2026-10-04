bl_info = {
    "name": "Traťový terén",
    "author": "lidar-toolbox",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Terén",
    "description": "Terén tratě z výšek a ortofota ČÚZK jako mesh",
    "category": "Import-Export",
}


def register():
    from . import addon

    addon.register()


def unregister():
    from . import addon

    addon.unregister()
