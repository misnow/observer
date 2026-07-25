"""
3D model loading and software rendering for the Output Canvas.

Two deliberate design choices worth stating up front:

1. **Software rendering, not OpenGL.** This machine has no usable GPU, and
   this app already has a painful history of native crashes around Qt's
   GPU-adjacent widgets. A numpy + QPainter rasterizer has no driver
   surface to crash against and is fast enough for the few-thousand-face
   models this is for. It does flat shading with a directional light,
   orthographic projection, backface culling, and painter's-algorithm depth
   sorting - no z-buffer, so heavily self-intersecting geometry can show
   sorting artifacts.

2. **Loading happens on a worker thread.** STEP import cost scales with
   face count, and a faceted STEP (like the ones mesh_to_step.py writes)
   can hold tens of thousands of planar faces - one measured at 100 seconds
   to tessellate. Blocking the UI that long is indistinguishable from a
   hang, so ModelLoadWorker keeps it off the main thread.
"""

import os
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal


# Anything past this gets decimated on load: face count drives both render
# time (one polygon draw per face) and rotation responsiveness.
DEFAULT_MAX_FACES = 4000


def load_model(path, max_faces=DEFAULT_MAX_FACES):
    """Load .stl/.obj/.ply/.glb (trimesh) or .step/.stp (OpenCASCADE).

    Returns (vertices Nx3 float64, faces Mx3 int32), centered on the origin
    so rotation happens about the model's own middle rather than drifting
    around some arbitrary modeling origin.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in (".step", ".stp"):
        vertices, faces = _load_step(path)
    else:
        import trimesh
        mesh = trimesh.load(path, force="mesh")
        if mesh.is_empty:
            raise ValueError(f"No geometry found in {os.path.basename(path)}")
        vertices = np.asarray(mesh.vertices, dtype=np.float64)
        faces = np.asarray(mesh.faces, dtype=np.int32)

    if max_faces and len(faces) > max_faces:
        vertices, faces = _decimate(vertices, faces, max_faces)

    # Center on the centroid of the bounding box, not the vertex mean: the
    # mean is pulled toward whichever region happens to be densely
    # tessellated, which makes rotation look off-axis.
    if len(vertices):
        bb_center = (vertices.min(axis=0) + vertices.max(axis=0)) / 2.0
        vertices = vertices - bb_center

    return vertices, faces


def _load_step(path):
    """Tessellate a STEP solid into a triangle mesh via OpenCASCADE."""
    import cadquery as cq

    result = cq.importers.importStep(path)
    shape = result.val()
    # Tolerance is a balance: finer means more triangles and slower import.
    # 0.1mm is well below anything visible at canvas scale.
    verts, tris = shape.tessellate(0.1)
    if not tris:
        raise ValueError(f"STEP file produced no triangles: {os.path.basename(path)}")
    vertices = np.array([[v.x, v.y, v.z] for v in verts], dtype=np.float64)
    faces = np.array(tris, dtype=np.int32)
    return vertices, faces


def _decimate(vertices, faces, max_faces):
    try:
        import trimesh
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        # face_count must be a keyword - trimesh 4.x's first positional
        # parameter is `percent`, so passing it positionally silently
        # reinterprets the budget as a percentage.
        reduced = mesh.simplify_quadric_decimation(face_count=max_faces)
        return (np.asarray(reduced.vertices, dtype=np.float64),
                np.asarray(reduced.faces, dtype=np.int32))
    except Exception:
        # Decimation needs the `fast_simplification` backend. Without it a
        # dense model still renders, just more slowly - better than refusing
        # to load at all.
        return vertices, faces


def rotation_matrix(rx, ry, rz):
    """Combined XYZ rotation matrix from degrees."""
    ax, ay, az = np.radians([rx, ry, rz])
    cx, sx = np.cos(ax), np.sin(ax)
    cy, sy = np.cos(ay), np.sin(ay)
    cz, sz = np.cos(az), np.sin(az)
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return mz @ my @ mx


def project(vertices, faces, rx, ry, rz, scale, tx, ty, width, height):
    """Transform, project, cull and depth-sort.

    Returns (polygons, shades, depths) where polygons is an (K,3,2) array of
    2D triangle corners in widget pixels, shades a (K,) array in 0..1, and
    depths a (K,) array already sorted back-to-front.
    """
    if len(vertices) == 0 or len(faces) == 0:
        return np.empty((0, 3, 2)), np.empty(0), np.empty(0)

    rotated = vertices @ rotation_matrix(rx, ry, rz).T

    # Auto-fit: normalize the model to the viewport, then apply the user's
    # scale on top. Without this a 2mm part and a 2m part would need wildly
    # different scale values to be visible at all.
    extent = np.abs(rotated).max()
    if extent == 0:
        extent = 1.0
    fit = (min(width, height) * 0.4) / extent
    pts = rotated * fit * scale

    # Screen space: X right, Y down (negate model Y so +Y renders upward,
    # matching how CAD tools present it), Z toward viewer.
    sx = pts[:, 0] + width / 2.0 + tx
    sy = -pts[:, 1] + height / 2.0 + ty
    sz = pts[:, 2]

    tri2d = np.stack([sx[faces], sy[faces]], axis=-1)   # (M,3,2)
    tri3d_z = sz[faces]                                  # (M,3)

    # Backface culling by 2D winding: with orthographic projection the sign
    # of the projected triangle's area tells you which way it faces.
    v0, v1, v2 = tri2d[:, 0], tri2d[:, 1], tri2d[:, 2]
    area2 = (v1[:, 0] - v0[:, 0]) * (v2[:, 1] - v0[:, 1]) - \
            (v1[:, 1] - v0[:, 1]) * (v2[:, 0] - v0[:, 0])
    front = area2 < 0
    if not np.any(front):
        # Fully inverted winding (some exporters do this) - keep everything
        # rather than render an empty view.
        front = np.ones(len(faces), dtype=bool)
    tri2d, tri3d_z = tri2d[front], tri3d_z[front]

    # Flat shading from the rotated face normal against a fixed headlight.
    rf = rotated[faces[front]]
    normals = np.cross(rf[:, 1] - rf[:, 0], rf[:, 2] - rf[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals = normals / lengths[:, None]
    light = np.array([0.3, 0.4, 0.85])
    light = light / np.linalg.norm(light)
    shades = np.clip(np.abs(normals @ light), 0.0, 1.0) * 0.75 + 0.25

    # Painter's algorithm: farthest first.
    depths = tri3d_z.mean(axis=1)
    order = np.argsort(depths)
    return tri2d[order], shades[order], depths[order]


class ModelLoadWorker(QThread):
    """Loads a model off the UI thread.

    STEP import in particular can take a very long time on face-heavy files,
    and freezing the interface for that long is indistinguishable from the
    app hanging.
    """
    loaded = pyqtSignal(object, object, str)   # vertices, faces, error

    def __init__(self, path, max_faces=DEFAULT_MAX_FACES, parent=None):
        super().__init__(parent)
        self.path = path
        self.max_faces = max_faces

    def run(self):
        try:
            vertices, faces = load_model(self.path, self.max_faces)
            self.loaded.emit(vertices, faces, "")
        except Exception as e:
            self.loaded.emit(None, None, f"{type(e).__name__}: {e}")
