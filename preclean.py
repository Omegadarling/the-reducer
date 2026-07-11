"""Optional mesh clean-up that runs before reduction.

Two independent steps, executed in the order they appear in the panel:

* Merge Vertices (by distance) — a bmesh ``remove_doubles`` weld. Duplicate /
  near-coincident vertices (common in CAD or photogrammetry exports) make
  Decimate collapse unevenly and can leave slivers; welding them first gives the
  reducer clean connectivity to work with. Loop data (UVs) rides along with the
  surviving faces, so the texture mapping is untouched.
* Instant Clean — if that third-party add-on is installed, its "Clean" command
  (``instantclean.clean``) is run on the target object using whatever settings
  the user configured in Instant Clean's own panel. Detected the same way the
  Quad Remesher bridge detects its add-on; without it the checkbox is hidden.
"""

from __future__ import annotations

import bmesh
import bpy


def instant_clean_available():
    """True when the Instant Clean add-on is installed and enabled."""
    return hasattr(bpy.types, "INSTANTCLEAN_OT_clean")


def merge_by_distance(mesh, distance):
    """Weld vertices closer than ``distance``. Returns the number removed.

    bmesh round-trips UV loop layers and shape-key layers, so welding is safe on
    textured and shape-keyed meshes alike.
    """
    before = len(mesh.vertices)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=max(float(distance), 0.0))
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return before - len(mesh.vertices)


def run_instant_clean(context, obj):
    """Run Instant Clean's "Clean" on exactly ``obj``.

    The operator acts on the current selection, so the selection is narrowed to
    ``obj`` for the call and restored afterwards. Returns an error string or
    None on success.
    """
    if not instant_clean_available():
        return "Instant Clean is not installed"
    prev_active = context.view_layer.objects.active
    prev_sel = [o for o in context.selected_objects]
    try:
        for o in prev_sel:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.instantclean.clean()
        return None
    except Exception as exc:
        return str(exc)
    finally:
        obj.select_set(False)
        for o in prev_sel:
            try:
                o.select_set(True)
            except Exception:
                pass   # an object may have been removed by the clean
        context.view_layer.objects.active = prev_active


def run(context, obj, s):
    """Apply the enabled pre-clean steps to ``obj`` per settings ``s``.

    Returns (messages, error): human-readable notes for the final report, and an
    error string when a step failed hard (merge failures raise instead — they
    indicate a real bug, not an environment issue).
    """
    messages = []
    if s.clean_merge:
        removed = merge_by_distance(obj.data, s.merge_distance)
        messages.append(f"merged {removed:,} vert(s)" if removed
                        else "merge: no doubles found")
    if s.clean_instant and instant_clean_available():
        err = run_instant_clean(context, obj)
        if err:
            return messages, f"Instant Clean failed: {err}"
        messages.append("Instant Clean ran")
    return messages, None
