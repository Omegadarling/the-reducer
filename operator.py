"""The Reduce operator and the object-duplication plumbing it drives."""

from __future__ import annotations

import bpy

from . import preclean, quadremesh, reduce


def _duplicate_object(context, obj):
    """Return an independent copy of ``obj``: new object, new mesh, single-user
    materials, linked into the same collections. Materials are copied so that
    rewiring texture nodes on the copy can never touch the source object."""
    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = obj.name + "_reduced"

    mat_cache = {}
    for slot in dup.material_slots:
        mat = slot.material
        if mat is None:
            continue
        copy = mat_cache.get(mat.name)
        if copy is None:
            copy = mat.copy()
            mat_cache[mat.name] = copy
        slot.material = copy

    collections = list(obj.users_collection) or [context.collection]
    for col in collections:
        col.objects.link(dup)
    return dup


def _discard_duplicate(dup):
    """Roll back a throwaway duplicate AND every datablock it uniquely owns, so a
    failed DUPLICATE run leaves no orphans. Collections are de-duplicated by
    identity (a material in two slots, or one reduced image on two nodes, would
    otherwise be visited twice and the second ``users`` access would touch a
    freed datablock). Order matters: removing the object drops its mesh to 0
    users, removing the copied materials drops the reduced images to 0 users."""
    seen = set()
    mats, imgs = [], []
    for slot in dup.material_slots:
        mat = slot.material
        if mat is not None and mat.as_pointer() not in seen:
            seen.add(mat.as_pointer())
            mats.append(mat)
    for mat in mats:
        if not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            img = getattr(node, "image", None)
            if node.type == "TEX_IMAGE" and img is not None and img.as_pointer() not in seen:
                seen.add(img.as_pointer())
                imgs.append(img)
    data = dup.data
    bpy.data.objects.remove(dup, do_unlink=True)
    if data is not None and data.users == 0:
        bpy.data.meshes.remove(data)
    for mat in mats:
        if mat.users == 0:
            bpy.data.materials.remove(mat)
    for img in imgs:
        if img.users == 0:
            bpy.data.images.remove(img)


class OBJECT_OT_reducer_run(bpy.types.Operator):
    bl_idname = "object.reducer_run"
    bl_label = "Reduce"
    bl_description = ("Reduce polygon count and/or texture size of the active mesh "
                      "while preserving its UV texture mapping")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == "MESH"

    def execute(self, context):
        s = context.scene.the_reducer
        src = context.active_object

        if not s.reduce_polys and not s.reduce_textures:
            self.report({"WARNING"},
                        "Nothing to do — enable polygon and/or texture reduction.")
            return {"CANCELLED"}

        # modifier_apply / make-single-user are OBJECT-mode operations; the user
        # may be in Sculpt/Edit/Paint. Switch to Object now, restore afterwards.
        prev_mode = None
        if context.object is not None and context.object.mode != "OBJECT":
            prev_mode = context.object.mode
            bpy.ops.object.mode_set(mode="OBJECT")

        # ---- Quad Remesher method ----------------------------------------
        # The engine runs as an external process; quadremesh.py invokes it and
        # finishes the whole job (UV transfer, triangulate, duplicate/in-place,
        # texture reduction) from a timer once the retopo object appears. The
        # object must stay in Object mode for the run, so prev_mode is not
        # restored on this path. With the add-on unavailable the panel hides
        # the Method row, so a saved QUADREMESH choice falls back to Decimate.
        if (s.reduce_polys and s.poly_reduction > 0.0
                and s.poly_method == "QUADREMESH" and quadremesh.available()):
            if quadremesh.is_running():
                self.report({"WARNING"},
                            "Quad Remesher is already remeshing — wait for it to finish.")
                return {"CANCELLED"}
            # Quad Remesher reads the ORIGINAL object, so pre-clean may only
            # mutate it when the user chose in-place output; in New Copy mode
            # the original must stay untouched, so pre-clean is skipped.
            qr_pre = ""
            if s.clean_merge or (s.clean_instant and preclean.instant_clean_available()):
                if s.output_target == "IN_PLACE":
                    pre_msgs, pre_err = preclean.run(context, src, s)
                    if pre_err:
                        self.report({"ERROR"}, f"Reduce failed: {pre_err}")
                        return {"CANCELLED"}
                    qr_pre = "pre-clean: " + "; ".join(pre_msgs) + ". "
                else:
                    qr_pre = ("Pre-clean skipped — Quad Remesher remeshes the "
                              "original object (use Apply To: Active Object). ")
            target_quads = reduce.projected_quads(
                reduce.triangle_count(src.data), s.poly_reduction)
            err = quadremesh.start(context, src, target_quads)
            if err:
                self.report({"ERROR"}, f"Reduce failed: {err}")
                return {"CANCELLED"}
            self.report({"INFO"}, f"The Reducer: {qr_pre}Quad Remesher running… the panel "
                        "status shows progress; UVs/textures are finished on completion.")
            return {"FINISHED"}

        duplicate = s.output_target == "DUPLICATE"
        target = _duplicate_object(context, src) if duplicate else src

        mod = None
        applied = False
        poly_note = None
        pre_msgs = []
        try:
            # ---- pre-clean --------------------------------------------------
            # Runs on the target (the copy in DUPLICATE mode), so the original
            # is never touched. Done before counting so the reduction % applies
            # to the cleaned mesh.
            pre_msgs, pre_err = preclean.run(context, target, s)
            if pre_err:
                raise RuntimeError(pre_err)
            tris_before = reduce.triangle_count(target.data)

            # ---- textures ---------------------------------------------------
            tex_changes = []
            if s.reduce_textures:
                if not duplicate:
                    # Isolate shared materials so rewiring their texture nodes
                    # can't downgrade other objects that use the same material.
                    reduce.single_user_materials(target)
                tex_changes = reduce.reduce_textures(
                    target, int(s.texture_size),
                    base_color_only=(s.texture_maps == "BASE_COLOR"))

            # ---- polygons ---------------------------------------------------
            if s.reduce_polys and s.poly_reduction > 0.0:
                mod = reduce.add_decimate(
                    target, s.poly_reduction,
                    triangulate=s.poly_triangulate,
                    symmetry=s.poly_symmetry, axis=s.poly_symmetry_axis)
                if s.keep_modifier:
                    poly_note = "live modifier"
                elif target.data.shape_keys is not None:
                    # A topology-changing modifier can't be baked onto shape keys;
                    # leave it live rather than fail the whole run.
                    poly_note = "kept live (mesh has shape keys)"
                else:
                    reduce.apply_modifier(context, target, mod)
                    applied = True
        except Exception as exc:  # clean error, never a traceback dialog
            import traceback
            traceback.print_exc()
            if duplicate:
                _discard_duplicate(target)
            elif mod is not None and mod.name in target.modifiers:
                target.modifiers.remove(mod)  # roll back the stray modifier
            self.report({"ERROR"}, f"Reduce failed: {exc}")
            return {"CANCELLED"}
        finally:
            # Restore the user's mode only when we modified their object in place.
            # In DUPLICATE mode focus moves to the new copy, so leaving the
            # original in Object mode is correct (and avoids stranding it in a
            # stale sculpt/edit mode once it is no longer active).
            if prev_mode is not None and not duplicate:
                try:
                    with context.temp_override(object=src, active_object=src):
                        bpy.ops.object.mode_set(mode=prev_mode)
                except Exception:
                    pass

        # Select / activate the result when we produced a new object.
        if duplicate:
            for o in context.selected_objects:
                o.select_set(False)
            target.select_set(True)
            context.view_layer.objects.active = target

        # ---- report -------------------------------------------------------- #
        msgs = list(pre_msgs)
        if s.reduce_polys and s.poly_reduction > 0.0:
            if applied:
                msgs.append(f"{tris_before:,}→{reduce.triangle_count(target.data):,} tris")
            else:
                proj = int(round(tris_before * (1.0 - s.poly_reduction / 100.0)))
                msgs.append(f"{tris_before:,}→~{proj:,} tris ({poly_note})")
        if s.reduce_textures:
            msgs.append(f"{len(tex_changes)} texture(s) → {s.texture_size}px"
                        if tex_changes else "no textures needed resizing")
        self.report({"INFO"}, "The Reducer: " + "; ".join(msgs) +
                    (f" → '{target.name}'" if duplicate else ""))
        return {"FINISHED"}


CLASSES = (OBJECT_OT_reducer_run,)
