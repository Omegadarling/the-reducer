"""User-facing settings for The Reducer, stored on the Scene as ``scene.the_reducer``."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import PropertyGroup


class ReducerSettings(PropertyGroup):
    # ---- pre-clean --------------------------------------------------------- #
    clean_merge: BoolProperty(
        name="Merge Vertices (by Distance)",
        description="Weld duplicate / near-coincident vertices before reducing. Cleans up "
                    "split-vertex exports so Decimate collapses evenly; UVs are untouched",
        default=False,
    )
    merge_distance: FloatProperty(
        name="Merge Distance",
        description="Vertices closer than this are welded into one",
        default=0.0001, min=0.0, soft_max=0.1, step=0.01, precision=5,
        subtype="DISTANCE",
    )
    clean_instant: BoolProperty(
        name="Instant Clean",
        description="Run the Instant Clean add-on's \"Clean\" command on the object before "
                    "reducing, using the settings from Instant Clean's own panel "
                    "(shown only while that add-on is installed)",
        default=False,
    )

    # ---- polygons ---------------------------------------------------------- #
    reduce_polys: BoolProperty(
        name="Reduce Polygons",
        description="Collapse the mesh to fewer triangles. UV coordinates are carried "
                    "along, so the texture keeps mapping to the same surfaces",
        default=True,
    )
    poly_method: EnumProperty(
        name="Method",
        description="How the polygon count is reduced",
        items=[
            ("DECIMATE", "Decimate",
             "Collapse edges with the Decimate modifier — fast, triangle output, "
             "UVs carried along natively"),
            ("QUADREMESH", "Quad Remesher",
             "Rebuild clean quad topology with the Quad Remesher add-on (Exoside), "
             "then transfer the original UVs onto the result. Engine settings not "
             "listed here (adaptive size, hard edges, …) are taken from the "
             "Quad Remesher panel"),
        ],
        default="DECIMATE",
    )
    qr_preserve_uvs: BoolProperty(
        name="Transfer UVs",
        description="Quad Remesher output has no UVs; copy the original mesh's UV "
                    "layout onto the new topology (Data Transfer, nearest-face "
                    "interpolated) so textures keep mapping to the same surfaces",
        default=True,
    )
    # Runtime status line written by the Quad Remesher watcher (quadremesh.py);
    # informational only, shown under the Reduce button while/after a run.
    qr_status: StringProperty(default="")

    poly_reduction: FloatProperty(
        name="Polygon Reduction",
        description="Percentage of triangles to remove (Decimate ▸ Collapse). "
                    "60% removes 60% of the triangles, keeping 40%",
        default=50.0, min=0.0, max=99.0, subtype="PERCENTAGE", precision=1,
    )
    poly_triangulate: BoolProperty(
        name="Triangulate Result",
        description="Keep the collapsed geometry triangulated (recommended, and required "
                    "for clean 3D-print / 3MF export)",
        default=True,
    )
    poly_symmetry: BoolProperty(
        name="Preserve Symmetry",
        description="Collapse symmetrically across an axis so a mirrored model stays mirrored",
        default=False,
    )
    poly_symmetry_axis: EnumProperty(
        name="Axis",
        description="Axis of symmetry to preserve while collapsing",
        items=[("X", "X", ""), ("Y", "Y", ""), ("Z", "Z", "")],
        default="X",
    )
    keep_modifier: BoolProperty(
        name="Keep Decimate Live",
        description="Leave the Decimate as an unapplied modifier so you can fine-tune the "
                    "ratio in the Modifier panel, instead of baking it into the mesh now",
        default=False,
    )

    # ---- textures ---------------------------------------------------------- #
    reduce_textures: BoolProperty(
        name="Reduce Textures",
        description="Down-scale the material's image textures. UVs are untouched, so the "
                    "smaller image still maps to the exact same places (just lower detail)",
        default=True,
    )
    texture_size: EnumProperty(
        name="Max Texture Size",
        description="Longest edge of each texture after reduction. Images already smaller "
                    "are left as-is (never upscaled); non-square images keep their aspect",
        items=[
            ("4096", "4096 px (4K)", "Cap textures at 4096 px"),
            ("2048", "2048 px (2K)", "Cap textures at 2048 px"),
            ("1024", "1024 px (1K)", "Cap textures at 1024 px"),
            ("512", "512 px", "Cap textures at 512 px"),
            ("256", "256 px", "Cap textures at 256 px"),
        ],
        default="2048",
    )
    texture_maps: EnumProperty(
        name="Maps",
        description="Which image textures to resize",
        items=[
            ("ALL", "All Maps", "Every image texture on the object "
                                "(base color, metallic/roughness, normal, …)"),
            ("BASE_COLOR", "Base Color Only", "Only the image feeding the Principled "
                                              "Base Color — leaves roughness/normal/etc. alone"),
        ],
        default="ALL",
    )

    # ---- output ------------------------------------------------------------ #
    output_target: EnumProperty(
        name="Apply To",
        description="Produce an independent reduced copy, or modify the active object directly",
        items=[
            ("DUPLICATE", "New Copy",
             "Create '<name>_reduced' with its own mesh and textures; the original is untouched"),
            ("IN_PLACE", "Active Object",
             "Modify the active object directly; its materials are rewired to fresh reduced textures"),
        ],
        default="DUPLICATE",
    )


CLASSES = (ReducerSettings,)
