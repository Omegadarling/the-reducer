"""Quad Remesher (Exoside) bridge — an alternative polygon-reduction backend.

Quad Remesher's ``qremesher.remesh`` operator is modal: it launches an external
engine process (xremesh) and imports the retopo mesh as a *new object* once the
engine finishes, long after the operator call returned. ``start()`` therefore
configures + invokes it and registers a ``bpy.app.timers`` watcher; the watcher
spots the imported retopo object (or a failure code in the engine's progress
file), then finishes The Reducer's job on it: UV transfer from the original
mesh (Quad Remesher output has no UVs), optional triangulation, DUPLICATE /
IN_PLACE integration, and texture reduction. Progress and the final report are
written to ``scene.the_reducer.qr_status`` for the panel to display.

Only the settings The Reducer owns (target count, symmetry, hide-input,
use-materials) are touched on ``scene.qremesher`` — saved before the run and
restored after — so everything else (adaptive size, hard edges, …) is respected
as configured in the Quad Remesher panel.
"""

from __future__ import annotations

import os
import platform
import tempfile
import time

import bpy

from . import reduce

#: scene.qremesher properties The Reducer sets for the duration of a run.
_QR_PROPS = ("target_count", "symmetry_x", "symmetry_y", "symmetry_z",
             "hide_input", "use_materials")

_TICK = 0.5                 # watcher poll interval (s)
_NO_FILE_TIMEOUT = 90.0     # engine never wrote a progress file
_STALL_TIMEOUT = 600.0      # progress value stopped advancing
_IMPORT_TIMEOUT = 60.0      # engine reported success but no object appeared

_job = None   # single active job — the engine remeshes one mesh at a time


def available():
    """True when the Quad Remesher add-on is installed and enabled."""
    return hasattr(bpy.types, "QREMESHER_OT_remesh")


def is_running():
    global _job
    if _job is not None and not bpy.app.timers.is_registered(_tick):
        _job = None   # a file load (or a crash in the tick) killed the watcher
    return _job is not None


class _Job:
    """Everything the watcher needs, snapshotted at start time (the scene
    settings may be edited while the engine is remeshing)."""

    def __init__(self, context, src, s):
        self.scene_name = context.scene.name
        self.src_uid = src.session_uid
        self.before = {o.session_uid for o in bpy.data.objects}
        self.saved = {}
        self.started = time.time()
        self.last_progress = None
        self.last_change = self.started
        self.success_time = None
        self.duplicate = s.output_target == "DUPLICATE"
        self.preserve_uvs = s.qr_preserve_uvs
        self.triangulate = s.poly_triangulate
        self.reduce_textures = s.reduce_textures
        self.texture_size = int(s.texture_size)
        self.base_color_only = s.texture_maps == "BASE_COLOR"


def start(context, src, target_quads):
    """Configure scene.qremesher, invoke the remesh operator on ``src`` and
    register the completion watcher. Returns an error string, or None."""
    global _job
    if is_running():
        return "a Quad Remesher run is already in progress"
    qr = getattr(context.scene, "qremesher", None)
    if qr is None or not available():
        return "Quad Remesher add-on not found"

    s = context.scene.the_reducer
    job = _Job(context, src, s)
    job.saved = {name: getattr(qr, name) for name in _QR_PROPS}

    qr.target_count = max(1, int(target_quads))
    qr.symmetry_x = s.poly_symmetry and s.poly_symmetry_axis == "X"
    qr.symmetry_y = s.poly_symmetry and s.poly_symmetry_axis == "Y"
    qr.symmetry_z = s.poly_symmetry and s.poly_symmetry_axis == "Z"
    qr.hide_input = False   # both output modes keep the source in place
    # Multi-material meshes: have the engine keep material ids, so the retopo
    # comes back with matching slots (Quad Remesher then reassigns the source
    # materials to them itself).
    qr.use_materials = len(src.material_slots) > 1

    # Quad Remesher remeshes "the one selected object".
    for o in context.selected_objects:
        o.select_set(False)
    src.select_set(True)
    context.view_layer.objects.active = src

    try:
        result = bpy.ops.qremesher.remesh("INVOKE_DEFAULT")
    except Exception as exc:
        _restore_qr_settings(job)
        return f"Quad Remesher failed to start: {exc}"
    if "RUNNING_MODAL" not in result:
        # Its execute() bailed before launching the engine (bad selection,
        # engine install, …) and has already reported the specific error.
        _restore_qr_settings(job)
        return "Quad Remesher did not start (see its error message)"

    _job = job
    _set_status(job, "Quad Remesher: starting…")
    bpy.app.timers.register(_tick, first_interval=_TICK)
    return None


# ------------------------------ watcher ---------------------------------- #

def _restore_qr_settings(job):
    scene = bpy.data.scenes.get(job.scene_name)
    qr = getattr(scene, "qremesher", None) if scene else None
    if qr is not None:
        for name, value in job.saved.items():
            setattr(qr, name, value)


def _set_status(job, text):
    scene = bpy.data.scenes.get(job.scene_name)
    if scene is not None:
        scene.the_reducer.qr_status = text
    for wm in bpy.data.window_managers:
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()


def _progress_path():
    """The engine's progress file (same path logic as the Quad Remesher
    add-on): first line is 0..1 while computing, 2 on success, <0 on error."""
    system = platform.system()
    if system in ("Darwin", "macosx"):
        base = "/var/tmp/Exoside"
    elif system == "Linux":
        base = "/tmp/Exoside"
    else:
        base = os.path.join(tempfile.gettempdir(), "Exoside")
    return os.path.join(base, "QuadRemesher", "Blender", "progress.txt")


def _read_progress():
    try:
        with open(_progress_path(), "r") as fh:
            lines = fh.read().splitlines()
        return float(lines[0]), (lines[1] if len(lines) > 1 else "")
    except Exception:
        return None, ""


def _find_result(job):
    """The retopo object: a mesh object that did not exist before the run
    (Quad Remesher imports its result as a new, selected, active object)."""
    fresh = [o for o in bpy.data.objects
             if o.type == "MESH" and o.session_uid not in job.before]
    if not fresh:
        return None
    active = bpy.context.view_layer.objects.active
    return active if active in fresh else fresh[0]


def _fail(job, msg):
    global _job
    _job = None
    _restore_qr_settings(job)
    print(f"The Reducer: Quad Remesher run failed: {msg}")
    _set_status(job, f"Quad Remesher failed: {msg}")
    return None   # stops the timer


def _tick():
    job = _job
    if job is None:
        return None

    ret = _find_result(job)
    if ret is not None:
        _finish(job, ret)
        return None

    now = time.time()
    value, text = _read_progress()
    if value is None:
        if job.last_progress is None and now - job.started > _NO_FILE_TIMEOUT:
            return _fail(job, "engine produced no progress file")
    elif value == 2:
        if job.success_time is None:
            job.success_time = now
            _set_status(job, "Quad Remesher: importing result…")
        elif now - job.success_time > _IMPORT_TIMEOUT:
            return _fail(job, "engine finished but no result object appeared")
    elif value == -2:
        return _fail(job, "license/EULA dialog open — run it once "
                          "from the Quad Remesher panel first")
    elif value < 0 and value != -11:   # -11 = partially written file: wait
        return _fail(job, text or "remeshing failed")
    elif 0 <= value <= 1:
        if value != job.last_progress:
            job.last_change = now
            _set_status(job, f"Quad Remesher: {int(value * 100)}%")
    if value is not None:
        job.last_progress = value
    if now - job.last_change > _STALL_TIMEOUT:
        return _fail(job, "no progress from the engine — giving up")
    return _TICK


# ------------------------------ finishing -------------------------------- #

def _is_identity(m, eps=1e-5):
    return all(abs(m[i][j] - (1.0 if i == j else 0.0)) < eps
               for i in range(4) for j in range(4))


def _ensure_materials(src, ret):
    """Make sure the retopo carries the source's materials. Quad Remesher
    copies them itself when the slot counts happen to match; otherwise rebuild
    the slot list (face material indices survive only in use-materials runs —
    for single-material meshes, everything lands on slot 0 anyway)."""
    src_mats = [slot.material for slot in src.material_slots]
    ret_mats = [slot.material for slot in ret.material_slots]
    if src_mats == ret_mats:
        return
    ret.data.materials.clear()
    for mat in src_mats:
        ret.data.materials.append(mat)


def _finish(job, ret):
    global _job
    _job = None
    _restore_qr_settings(job)
    try:
        _integrate(job, ret)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _set_status(job, f"Quad Remesher post-processing failed: {exc}")


def _integrate(job, ret):
    context = bpy.context
    src = next((o for o in bpy.data.objects
                if o.session_uid == job.src_uid), None)
    if src is None:
        _set_status(job, f"done, but the source object is gone — "
                         f"kept '{ret.name}' as imported")
        return

    tris_before = reduce.triangle_count(src.data)
    _ensure_materials(src, ret)

    # Put the retopo where the source lives (the FBX import linked it to the
    # active collection, which may be anywhere).
    for col in list(ret.users_collection):
        col.objects.unlink(ret)
    for col in (list(src.users_collection) or [context.scene.collection]):
        col.objects.link(ret)

    # UVs first (onto the clean quads), triangulation after.
    if job.preserve_uvs:
        reduce.transfer_uvs(context, src, ret)
    if job.triangulate:
        mod = ret.modifiers.new(name="Reducer Triangulate", type="TRIANGULATE")
        reduce.apply_modifier(context, ret, mod)

    if job.duplicate:
        ret.name = src.name + "_reduced"
        ret.data.name = ret.name
        # Private material copies, so texture reduction (and any later edits)
        # on the copy can never touch the source object — DUPLICATE semantics.
        reduce.single_user_materials(ret)
        target = ret
    else:
        # Swap the retopo mesh into the source object. The FBX round-trip can
        # decompose the transform differently, so bake any delta into the mesh
        # to keep the geometry exactly where the source's transform puts it.
        if src.mode != "OBJECT":
            with context.temp_override(object=src, active_object=src,
                                       selected_objects=[src]):
                bpy.ops.object.mode_set(mode="OBJECT")
        delta = src.matrix_world.inverted() @ ret.matrix_world
        if not _is_identity(delta):
            ret.data.transform(delta)
        old = src.data
        old_name = old.name
        src.data = ret.data
        bpy.data.objects.remove(ret, do_unlink=True)
        if old.users == 0:
            bpy.data.meshes.remove(old)
            src.data.name = old_name
        target = src
        if job.reduce_textures:
            reduce.single_user_materials(src)

    for o in context.selected_objects:
        o.select_set(False)
    target.select_set(True)
    context.view_layer.objects.active = target

    tex_msg = ""
    if job.reduce_textures:
        changes = reduce.reduce_textures(target, job.texture_size,
                                         base_color_only=job.base_color_only)
        tex_msg = (f"; {len(changes)} texture(s) → {job.texture_size}px"
                   if changes else "; no textures needed resizing")
    unit = "tris" if job.triangulate else "faces"
    _set_status(job, f"done: {tris_before:,} tris → "
                     f"{len(target.data.polygons):,} {unit}{tex_msg}"
                     + (f" → '{target.name}'" if job.duplicate else ""))
