"""Core reduction logic: polygon decimation and texture down-scaling.

Deliberately free of operator / UI concerns so it can be exercised headlessly.
Two independent operations, neither of which disturbs the UV layout:

* Polygon reduction — a Decimate ▸ Collapse modifier. Collapse interpolates and
  carries the UVs along with the surviving vertices, so the texture keeps
  landing on the same surfaces.
* Texture reduction — each over-cap image is replaced by a fresh, packed image
  holding its down-scaled pixels, and the material node is rewired to it. UVs
  are normalized 0..1 coordinates, so the smaller image is sampled by the exact
  same coordinates; only the resolution drops. The new image is embedded in the
  .blend (persists on reload); the source image and its file are never modified.
"""

from __future__ import annotations

import bpy
import numpy as np


# ============================== textures ================================== #

def _principled(mat):
    if mat is None or not mat.use_nodes:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _base_color_images(mat):
    """Set of image datablocks feeding the Principled 'Base Color' input.

    Walks upstream through intermediate nodes (Mix, Hue/Sat, Gamma, color-ramp,
    reroutes, …) so a base color driven through a node chain is still found —
    not just a directly-linked Image Texture node.
    """
    node = _principled(mat)
    if node is None:
        return set()
    inp = node.inputs.get("Base Color")
    if inp is None or not inp.is_linked:
        return set()
    images = set()
    stack = [link.from_node for link in inp.links]
    seen = set()
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if n.type == "TEX_IMAGE":
            if n.image is not None:
                images.add(n.image)
            continue
        for socket in n.inputs:
            for link in socket.links:
                stack.append(link.from_node)
    return images


def iter_image_nodes(obj, base_color_only=False):
    """Yield (material, image_node) for TEX_IMAGE nodes on ``obj``'s materials.

    With ``base_color_only`` the result is restricted to nodes whose image feeds
    the Principled Base Color input (directly or through intermediate nodes).
    """
    seen_mats = set()
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or mat.name in seen_mats or not mat.use_nodes:
            continue
        seen_mats.add(mat.name)
        target_imgs = _base_color_images(mat) if base_color_only else None
        for node in mat.node_tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            if base_color_only and node.image not in target_imgs:
                continue
            yield mat, node


def target_dims(w, h, max_size):
    """New (w, h) with the longest side capped at ``max_size``, aspect ratio
    preserved and never upscaled. Returns None when no change is needed."""
    longest = max(w, h)
    if longest <= max_size:
        return None
    factor = max_size / float(longest)
    nw = max(1, round(w * factor))
    nh = max(1, round(h * factor))
    if (nw, nh) == (w, h):
        return None
    return nw, nh


def _reduced_image(src, dims):
    """A fresh, packed image holding ``src``'s pixels down-scaled to ``dims``.

    Built from a throwaway scaled copy, so it is fully independent of ``src``:
    the source image datablock (and its external file, if any) is never touched.
    A brand-new image packs its own pixel buffer cleanly, which is what makes
    the reduction reliably persist in the .blend across save/reload.
    """
    w, h = dims
    tmp = src.copy()
    new = None
    try:
        # Give the copy its own decodable buffer to scale (an unpacked external
        # file is embedded first; this reads the source file but never writes it).
        if tmp.source == "FILE" and not tmp.packed_file:
            try:
                tmp.pack()
            except RuntimeError:
                pass
        tmp.scale(w, h)
        buf = np.empty(w * h * 4, dtype=np.float32)   # Blender pixels are RGBA
        tmp.pixels.foreach_get(buf)

        new = bpy.data.images.new(src.name + "_reduced", w, h,
                                  alpha=True, float_buffer=getattr(src, "is_float", False))
        try:
            new.colorspace_settings.name = src.colorspace_settings.name
        except Exception:
            pass   # keep the default colorspace if the source's isn't assignable
        new.pixels.foreach_set(buf)
        new.pack()
        new.update()
    except Exception:
        # Never strand a half-built datablock on the error path.
        if new is not None and new.users == 0:
            bpy.data.images.remove(new)
        raise
    finally:
        bpy.data.images.remove(tmp)
    return new


def reduce_textures(obj, max_size, base_color_only=False):
    """Replace each over-cap image texture on ``obj`` with a fresh, packed,
    down-scaled image and rewire the node to it.

    Non-destructive to the source images: a fresh image is always built, so the
    original image datablocks (and any external files) are never modified. Images
    already within the cap, missing files, and UDIM/tiled sets are left as-is.

    (Node rewiring edits ``obj``'s material node trees; the operator single-users
    shared materials in IN_PLACE mode so this cannot affect other objects.)

    Returns a list of (new_name, old_size, new_size) for images that changed.
    """
    changed = []
    cache = {}   # source image datablock -> its single reduced replacement
    for _mat, node in iter_image_nodes(obj, base_color_only):
        src = node.image
        if src.source == "TILED":
            continue   # a UDIM set — don't flatten it to a single tile
        # Missing files report size (0, 0), so target_dims returns None below.
        dims = target_dims(src.size[0], src.size[1], max_size)
        if dims is None:
            continue
        new = cache.get(src)   # datablock key: avoids local/linked name collisions
        if new is None:
            try:
                new = _reduced_image(src, dims)
            except Exception:
                continue   # undecodable source (e.g. missing file) — leave it as-is
            cache[src] = new
            changed.append((new.name, (src.size[0], src.size[1]), dims))
        node.image = new
    return changed


def single_user_materials(obj):
    """Give ``obj`` private copies of any material it shares with other users, so
    rewiring its material nodes (texture reduction) cannot affect other objects.
    Only genuinely shared materials (users > 1) are copied; slots sharing one
    material still share its single copy."""
    cache = {}
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or mat.users <= 1:
            continue
        copy = cache.get(mat.as_pointer())
        if copy is None:
            copy = mat.copy()
            cache[mat.as_pointer()] = copy
        slot.material = copy


# ============================== polygons ================================== #

def triangle_count(mesh):
    """Exact triangle count in O(1): a polygon with n corners is n-2 triangles,
    so total triangles == (total loops) - 2 * (total polygons). Decimate ▸
    Collapse works on triangles, so this is the number its ratio scales."""
    return max(0, len(mesh.loops) - 2 * len(mesh.polygons))


def add_decimate(obj, reduction_pct, triangulate=True, symmetry=False, axis="X"):
    """Add a Decimate ▸ Collapse modifier; ratio derived from a reduction %.

    reduction_pct is the percentage of triangles to remove: 60 -> ratio 0.4.
    """
    ratio = max(0.0, min(1.0, 1.0 - reduction_pct / 100.0))
    mod = obj.modifiers.new(name="Reducer Decimate", type="DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = ratio
    mod.use_collapse_triangulate = triangulate
    if symmetry:
        mod.use_symmetry = True
        mod.symmetry_axis = axis
    return mod


def apply_modifier(context, obj, mod):
    """Bake a single modifier into the mesh. Ensures single-user mesh data so
    the apply cannot fail on / mutate a shared mesh."""
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    with context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
        bpy.ops.object.modifier_apply(modifier=mod.name)


def projected_quads(tri_count, reduction_pct):
    """Quad Remesher target count equivalent to removing ``reduction_pct`` % of
    the current triangles: a quad covers ~2 triangles."""
    return max(1, int(round(tri_count * (1.0 - reduction_pct / 100.0) / 2.0)))


def transfer_uvs(context, src, dst):
    """Copy ``src``'s UV layers onto ``dst``'s (different) topology with a Data
    Transfer modifier — nearest-face interpolated — then bake it. Layers are
    matched by name; missing ones are created on ``dst`` first. The modifier
    maps through both objects' world transforms, so it works as long as the two
    meshes overlap in world space. Returns True when a transfer ran."""
    src_layers = src.data.uv_layers
    if not src_layers:
        return False
    for layer in src_layers:
        if layer.name not in dst.data.uv_layers and len(dst.data.uv_layers) < 8:
            dst.data.uv_layers.new(name=layer.name, do_init=False)
    if not dst.data.uv_layers:
        return False
    if 0 <= src_layers.active_index < len(dst.data.uv_layers):
        dst.data.uv_layers.active_index = src_layers.active_index
    mod = dst.modifiers.new(name="Reducer UV Transfer", type="DATA_TRANSFER")
    mod.object = src
    mod.use_loop_data = True
    mod.data_types_loops = {"UV"}
    mod.loop_mapping = "POLYINTERP_NEAREST"
    mod.layers_uv_select_src = "ALL"
    mod.layers_uv_select_dst = "NAME"
    apply_modifier(context, dst, mod)
    return True
