"""
Faceted STEP (ISO-10303-21) export for triangle meshes.

Purpose: the tail end of an image -> 3D -> CAD pipeline. Single-image-to-3D
models (TripoSR, TRELLIS, Hunyuan3D, Tripo/Meshy APIs, ...) all emit *meshes*
(.glb/.obj/.stl). CAD tooling wants STEP. This module bridges that gap.

Scope, stated plainly: this produces a FACETED B-rep - every triangle of the
mesh becomes its own planar face. It is NOT parametric CAD geometry: there
are no analytic cylinders, fillets, or feature history, and nothing here
"reverse engineers" a mesh back into clean solids (that is a genuinely hard,
semi-manual problem that commercial tools charge a lot for). What you get is
a real, valid STEP solid that CAD systems can open, measure, boolean, and
position - built out of a lot of little flat faces.

Because face count == triangle count, decimate aggressively before export.
A 100k-triangle mesh yields a 100k-face STEP that will bring most CAD
readers to their knees; a few thousand faces is usually plenty for
placement/fixturing work.

Pure standard library + trimesh. Deliberately no OpenCASCADE dependency for
the export path, so this stays lightweight and importable anywhere.
"""

import time
import os


def _fmt(v):
    """STEP reals must always carry a decimal point."""
    out = repr(float(v))
    if "e" in out or "E" in out:
        # STEP accepts E notation but wants an explicit decimal point in the
        # mantissa (1.E-05, not 1E-05).
        mant, _, exp = out.partition("e")
        if "." not in mant:
            mant += "."
        return f"{mant}E{exp}"
    if "." not in out:
        out += "."
    return out


def mesh_to_step(vertices, faces, step_path, product_name="LightGuideSolid",
                 author="LightGuide", organization="LightGuide"):
    """Write a faceted STEP AP214 solid.

    vertices: (N,3) sequence of XYZ
    faces:    (M,3) sequence of vertex indices (triangles)
    """
    lines = []
    nid = 0

    def add(entity):
        nonlocal nid
        nid += 1
        lines.append(f"#{nid}={entity};")
        return nid

    # --- Units: mm, radians, steradians, with a length tolerance -----------
    si_len = add("(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))")
    si_ang = add("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
    si_sol = add("(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())")
    tol = add(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{si_len},"
              f"'distance_accuracy_value','confusion accuracy')")
    ctx = add(
        f"(GEOMETRIC_REPRESENTATION_CONTEXT(3)"
        f"GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{tol}))"
        f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{si_len},#{si_ang},#{si_sol}))"
        f"REPRESENTATION_CONTEXT('Context','3D'))")

    # --- Geometry: one CARTESIAN_POINT per unique vertex -------------------
    point_ids = [add(f"CARTESIAN_POINT('',({_fmt(v[0])},{_fmt(v[1])},{_fmt(v[2])}))")
                 for v in vertices]

    # --- One planar FACE_SURFACE per triangle ------------------------------
    # POLY_LOOP references CARTESIAN_POINTs directly, which is exactly what
    # the faceted case is for - no edge curves or vertex topology needed.
    face_ids = []
    for f in faces:
        i0, i1, i2 = int(f[0]), int(f[1]), int(f[2])
        p0, p1, p2 = vertices[i0], vertices[i1], vertices[i2]

        # Plane placement: origin at the first corner, Z = triangle normal,
        # X = the first edge direction.
        ux, uy, uz = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
        vx, vy, vz = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
        nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        nlen = (nx * nx + ny * ny + nz * nz) ** 0.5
        if nlen == 0:
            continue  # skip degenerate/zero-area triangles
        nx, ny, nz = nx / nlen, ny / nlen, nz / nlen
        ulen = (ux * ux + uy * uy + uz * uz) ** 0.5
        if ulen == 0:
            continue
        ux, uy, uz = ux / ulen, uy / ulen, uz / ulen

        origin = add(f"CARTESIAN_POINT('',({_fmt(p0[0])},{_fmt(p0[1])},{_fmt(p0[2])}))")
        axis = add(f"DIRECTION('',({_fmt(nx)},{_fmt(ny)},{_fmt(nz)}))")
        refd = add(f"DIRECTION('',({_fmt(ux)},{_fmt(uy)},{_fmt(uz)}))")
        placement = add(f"AXIS2_PLACEMENT_3D('',#{origin},#{axis},#{refd})")
        plane = add(f"PLANE('',#{placement})")

        loop = add(f"POLY_LOOP('',(#{point_ids[i0]},#{point_ids[i1]},#{point_ids[i2]}))")
        bound = add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
        face_ids.append(add(f"FACE_SURFACE('',(#{bound}),#{plane},.T.)"))

    if not face_ids:
        raise ValueError("No non-degenerate triangles to export.")

    shell = add(f"CLOSED_SHELL('',({','.join('#' + str(i) for i in face_ids)}))")
    solid = add(f"FACETED_BREP('{product_name}',#{shell})")

    # --- Product / shape-representation boilerplate ------------------------
    app_ctx = add("APPLICATION_CONTEXT('automotive design')")
    add(f"APPLICATION_PROTOCOL_DEFINITION('international standard',"
        f"'automotive_design',2000,#{app_ctx})")
    prod_ctx = add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{app_ctx},'design')")
    mech_ctx = add(f"PRODUCT_CONTEXT('',#{app_ctx},'mechanical')")
    product = add(f"PRODUCT('{product_name}','{product_name}','',(#{mech_ctx}))")
    formation = add(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
    pdef = add(f"PRODUCT_DEFINITION('design','',#{formation},#{prod_ctx})")
    pshape = add(f"PRODUCT_DEFINITION_SHAPE('','',#{pdef})")

    origin0 = add("CARTESIAN_POINT('',(0.,0.,0.))")
    zdir = add("DIRECTION('',(0.,0.,1.))")
    xdir = add("DIRECTION('',(1.,0.,0.))")
    world = add(f"AXIS2_PLACEMENT_3D('',#{origin0},#{zdir},#{xdir})")
    brep_rep = add(f"ADVANCED_BREP_SHAPE_REPRESENTATION('{product_name}',"
                   f"(#{world},#{solid}),#{ctx})")
    add(f"SHAPE_DEFINITION_REPRESENTATION(#{pshape},#{brep_rep})")

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    header = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION((''),'2;1');\n"
        f"FILE_NAME('{os.path.basename(step_path)}','{stamp}',('{author}'),"
        f"('{organization}'),'LightGuide mesh_to_step','','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        "ENDSEC;\n"
        "DATA;\n"
    )
    footer = "ENDSEC;\nEND-ISO-10303-21;\n"

    with open(step_path, "w", encoding="ascii") as fh:
        fh.write(header)
        fh.write("\n".join(lines))
        fh.write("\n" + footer)

    return {"faces": len(face_ids), "vertices": len(vertices), "path": step_path}


def convert_mesh_file(mesh_path, step_path, max_faces=5000):
    """Load any trimesh-readable mesh (.stl/.obj/.glb/.ply/...), optionally
    decimate it, and write a faceted STEP.

    max_faces exists because face count drives STEP size directly - a
    100k-triangle mesh makes a 100k-face STEP that most CAD readers choke
    on. Pass None to skip decimation.
    """
    import trimesh

    mesh = trimesh.load(mesh_path, force="mesh")
    if mesh.is_empty:
        raise ValueError(f"No geometry found in {mesh_path}")

    original_faces = len(mesh.faces)
    decimation_note = None
    if max_faces and original_faces > max_faces:
        try:
            # face_count MUST be passed by keyword: trimesh 4.x's first
            # positional parameter is `percent`, so a positional call
            # silently reinterprets the face budget as a percentage.
            # Requires the `fast_simplification` package as its backend.
            mesh = mesh.simplify_quadric_decimation(face_count=max_faces)
        except Exception as e:
            # Deliberately loud rather than silent: falling through leaves
            # one STEP face per triangle, which for a dense scan-style mesh
            # means a multi-hundred-megabyte file that no CAD reader will
            # open. Silence here previously produced a 10MB file from a
            # 20k-triangle sphere with no indication anything went wrong.
            decimation_note = (
                f"Decimation to {max_faces} faces FAILED ({type(e).__name__}: {e}). "
                f"Exporting all {original_faces} faces - expect a very large STEP file. "
                f"Install 'fast_simplification' to enable decimation.")

    result = mesh_to_step(mesh.vertices, mesh.faces, step_path,
                          product_name=os.path.splitext(os.path.basename(mesh_path))[0])
    result["original_faces"] = original_faces
    result["decimated"] = decimation_note is None and original_faces != result["faces"]
    if decimation_note:
        result["warning"] = decimation_note
    return result
