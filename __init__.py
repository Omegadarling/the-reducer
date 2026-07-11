"""The Reducer — Blender extension.

Lower a mesh's polygon count and shrink its texture maps while preserving the
UV texture mapping — everything keeps landing on the exact same places.

Metadata lives in blender_manifest.toml (no bl_info — this is an extension).
"""

import bpy

from . import operator, props, ui

_CLASSES = (*props.CLASSES, *operator.CLASSES, *ui.CLASSES)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.the_reducer = bpy.props.PointerProperty(type=props.ReducerSettings)


def unregister():
    if hasattr(bpy.types.Scene, "the_reducer"):
        del bpy.types.Scene.the_reducer
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
