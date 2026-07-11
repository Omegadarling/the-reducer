"""The Reducer sidebar panel (View3D ▸ N ▸ Reducer)."""

from __future__ import annotations

import bpy

from . import preclean, quadremesh, reduce
from .operator import OBJECT_OT_reducer_run


def _texture_lines(obj, base_color_only, max_size):
    """Draw-safe preview of what texture reduction will do (no pixel reads).

    Returns (lines, any_image). Each line reflects the selected cap: images over
    the cap show their projected new size, images at/under it are marked kept.
    """
    lines = []
    seen = set()
    any_image = False
    for _mat, node in reduce.iter_image_nodes(obj, base_color_only):
        any_image = True
        img = node.image
        if img.name in seen:
            continue
        seen.add(img.name)
        w, h = img.size[0], img.size[1]
        dims = reduce.target_dims(w, h, max_size)
        if dims is None:
            lines.append(f"{img.name}  {w}×{h}  (kept)")
        else:
            lines.append(f"{img.name}  {w}×{h} → {dims[0]}×{dims[1]}")
    return lines, any_image


class VIEW3D_PT_the_reducer(bpy.types.Panel):
    bl_label = "The Reducer"
    bl_idname = "VIEW3D_PT_the_reducer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Reducer"

    def draw(self, context):
        layout = self.layout
        s = context.scene.the_reducer
        obj = context.active_object

        if obj is None or obj.type != "MESH":
            layout.label(text="Select a mesh object", icon="ERROR")
            return

        # ---- pre-clean ----------------------------------------------------- #
        # Optional clean-up applied before reduction, in the order shown.
        layout.label(text="Pre-Clean", icon="BRUSH_DATA")
        col = layout.column(align=True)
        col.prop(s, "clean_merge")
        dist = col.row(align=True)
        dist.enabled = s.clean_merge
        dist.prop(s, "merge_distance")
        if preclean.instant_clean_available():
            col.prop(s, "clean_instant")

        layout.separator()

        # ---- polygons ---------------------------------------------------- #
        header = layout.row(align=True)
        header.prop(s, "reduce_polys", text="")
        header.label(text="Polygons", icon="MOD_DECIM")

        col = layout.column(align=True)
        col.enabled = s.reduce_polys
        # The Quad Remesher method is only offered when the add-on is installed;
        # without it the panel looks exactly as before (Decimate is implied).
        qr_installed = quadremesh.available()
        if qr_installed:
            col.row(align=True).prop(s, "poly_method", expand=True)
        use_qr = qr_installed and s.poly_method == "QUADREMESH"
        col.prop(s, "poly_reduction", slider=True)
        tris = reduce.triangle_count(obj.data)
        if use_qr:
            quads = reduce.projected_quads(tris, s.poly_reduction)
            col.label(text=f"{tris:,} tris → ~{quads:,} quads", icon="MESH_DATA")
        else:
            projected = int(round(tris * (1.0 - s.poly_reduction / 100.0)))
            col.label(text=f"{tris:,} → ~{projected:,} tris", icon="MESH_DATA")
        row = col.row(align=True)
        row.prop(s, "poly_symmetry")
        axis = row.row(align=True)
        axis.enabled = s.poly_symmetry
        axis.prop(s, "poly_symmetry_axis", expand=True)
        col.prop(s, "poly_triangulate")
        if use_qr:
            col.prop(s, "qr_preserve_uvs")
            col.label(text="Engine settings: Quad Remesher panel", icon="INFO")
        else:
            col.prop(s, "keep_modifier")

        layout.separator()

        # ---- textures ---------------------------------------------------- #
        header = layout.row(align=True)
        header.prop(s, "reduce_textures", text="")
        header.label(text="Textures", icon="TEXTURE")

        col = layout.column(align=True)
        col.enabled = s.reduce_textures
        col.prop(s, "texture_size")
        col.prop(s, "texture_maps")
        info = col.box()
        info.scale_y = 0.7
        lines, any_image = _texture_lines(
            obj, s.texture_maps == "BASE_COLOR", int(s.texture_size))
        if lines:
            for line in lines:
                info.label(text=line)
        elif any_image:
            info.label(text="No Base Color image — try All Maps", icon="INFO")
        else:
            info.label(text="No image textures found", icon="INFO")

        layout.separator()

        # ---- output ------------------------------------------------------ #
        layout.prop(s, "output_target")
        row = layout.row()
        row.scale_y = 1.5
        row.operator(OBJECT_OT_reducer_run.bl_idname, text="Reduce", icon="MOD_DECIM")

        # Quad Remesher runs asynchronously; its watcher reports here.
        if quadremesh.is_running() or (use_qr and s.qr_status):
            icon = "ERROR" if "failed" in s.qr_status else "INFO"
            layout.label(text=s.qr_status or "Quad Remesher: starting…", icon=icon)


CLASSES = (VIEW3D_PT_the_reducer,)
