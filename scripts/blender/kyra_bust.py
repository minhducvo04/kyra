"""Build Kyra's stylized bust in Blender, export it as GLB and optionally render a turntable.

Run headless (bpy only, no add-ons, no downloads):
    blender -b --factory-startup -P scripts/blender/kyra_bust.py -- --glb web/models/kyra-bust.glb
    blender -b --factory-startup -P scripts/blender/kyra_bust.py -- --render-dir <dir> --frames 72

The look follows the 3D storyboard v0: aqua hair (icy roots to turquoise tips), turquoise eyes, a soft face,
dark navy top and a cyan floor ring. Node names are the contract with web/model3d-kyra.js:
Kyra (root), Body, Neck, Head (pivot at the neck), Face, EyeL, EyeR, Mouth, Hair, Ring.
"""
import argparse
import math
import sys

import bpy
from mathutils import Vector

SKIN = (0.93, 0.80, 0.72, 1)
TOP = (0.06, 0.10, 0.16, 1)
SCLERA = (0.96, 0.97, 0.98, 1)
IRIS = (0.10, 0.80, 0.88, 1)
PUPIL = (0.02, 0.05, 0.08, 1)
LIP = (0.78, 0.45, 0.46, 1)
HAIR_ROOT = (0.86, 0.98, 1.0, 1)
HAIR_TIP = (0.08, 0.78, 0.90, 1)
RING = (0.16, 0.85, 1.0, 1)  # #29d8ff, the page accent
HEAD_PIVOT = Vector((0, 0, 0.50))


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--glb")
    p.add_argument("--render-dir")
    p.add_argument("--frames", type=int, default=72)
    p.add_argument("--size", type=int, default=640)
    return p.parse_args(argv)


def material(name, color, rough=0.55, emit=0.0, vertex_colors=False):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Roughness"].default_value = rough
    if emit:
        bsdf.inputs["Emission Color"].default_value = color
        bsdf.inputs["Emission Strength"].default_value = emit
    if vertex_colors:
        attr = mat.node_tree.nodes.new("ShaderNodeVertexColor")
        attr.layer_name = "Col"
        mat.node_tree.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def finish(obj, name, mat, parent=None, smooth=True):
    obj.name = name
    obj.data.name = name
    obj.data.materials.append(mat)
    if smooth:
        for poly in obj.data.polygons:
            poly.use_smooth = True
    if parent is not None:
        world = obj.matrix_world.copy()
        obj.parent = parent
        obj.matrix_world = world
    return obj


def sphere(name, loc, scale, mat, parent=None, segments=48, rings=24):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, location=loc)
    obj = bpy.context.active_object
    obj.scale = scale
    bpy.ops.object.transform_apply(scale=True)
    return finish(obj, name, mat, parent)


def empty(name, loc, parent=None):
    obj = bpy.data.objects.new(name, None)
    obj.location = loc
    bpy.context.collection.objects.link(obj)
    if parent is not None:
        obj.parent = parent
        obj.location = loc - parent.matrix_world.translation
    return obj


def gradient(obj, top_z, bottom_z):
    """Hair vertex colors: icy at the roots (top), turquoise at the tips (bottom)."""
    mesh = obj.data
    layer = mesh.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
    span = max(top_z - bottom_z, 1e-6)
    for i, v in enumerate(mesh.vertices):
        z = (obj.matrix_world @ v.co).z
        t = min(max((top_z - z) / span, 0.0), 1.0) ** 0.8
        layer.data[i].color = tuple(HAIR_ROOT[c] * (1 - t) + HAIR_TIP[c] * t for c in range(3)) + (1.0,)


def hair_shell(center, scale, mat, parent):
    """A unit sphere reshaped in its own coordinates: the lower half drops into a long curtain, a window is cut
    for the face, small waves run down the strands, then the colors fade from icy roots to turquoise tips."""
    import bmesh
    bpy.ops.mesh.primitive_uv_sphere_add(segments=72, ring_count=48, radius=1, location=(0, 0, 0))
    obj = bpy.context.active_object
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    for v in bm.verts:
        x, y, z = v.co
        if z < 0:
            rxy = math.hypot(x, y)
            grow = max(rxy, 0.9) / rxy if rxy > 1e-6 else 0
            x, y, z = x * grow, y * grow, z * 2.3
            wave = 0.04 * math.sin(z * 9 + math.atan2(y, x) * 7)
            x, y = x * (1 + wave), y * (1 + wave)
        v.co = Vector((x, y, z))
    doomed = []
    for f in bm.faces:
        m = f.calc_center_median()
        if (m.y < -0.25 and m.z < 0.42 and abs(m.x) < 0.72) or m.z < -2.15:
            doomed.append(f)
    bmesh.ops.delete(bm, geom=doomed, context="FACES")
    bm.to_mesh(obj.data)
    bm.free()
    obj.location = center
    obj.scale = scale
    bpy.ops.object.transform_apply(location=False, scale=True)
    shell = obj.modifiers.new("thickness", "SOLIDIFY")
    shell.thickness = 0.012
    bpy.ops.object.modifier_apply(modifier="thickness")
    finish(obj, "HairShell", mat, parent)
    gradient(obj, center.z + scale[2], center.z - scale[2] * 2.2)
    return obj


def build():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    skin = material("Skin", SKIN, 0.6)
    top = material("Top", TOP, 0.8)
    sclera = material("Sclera", SCLERA, 0.3)
    iris = material("Iris", IRIS, 0.25, emit=0.6)
    pupil = material("Pupil", PUPIL, 0.2)
    lip = material("Lip", LIP, 0.5)
    hair = material("HairMat", HAIR_TIP, 0.45, vertex_colors=True)
    ring = material("RingMat", RING, 0.3, emit=4.0)
    browmat = material("BrowMat", (0.05, 0.45, 0.55, 1), 0.6)

    root = empty("Kyra", Vector((0, 0, 0)))

    # Shoulders and chest: a flattened half dome, dark navy top.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, location=(0, 0, 0.12))
    body = bpy.context.active_object
    body.scale = (0.31, 0.17, 0.22)
    bpy.ops.object.transform_apply(scale=True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="DESELECT")
    bpy.ops.object.mode_set(mode="OBJECT")
    for v in body.data.vertices:
        v.select = v.co.z < 0.0
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.delete(type="VERT")
    bpy.ops.object.mode_set(mode="OBJECT")
    finish(body, "Body", top, root)

    bpy.ops.mesh.primitive_cylinder_add(vertices=40, radius=0.055, depth=0.2, location=(0, 0, 0.42))
    finish(bpy.context.active_object, "Neck", skin, root)

    head = empty("Head", HEAD_PIVOT, root)
    c = Vector((0, 0, 0.66))  # head center
    sphere("Face", c, (0.135, 0.145, 0.175), skin, head)
    sphere("Nose", c + Vector((0, -0.142, -0.02)), (0.012, 0.012, 0.018), skin, head, 16, 8)
    for side, x in (("L", 0.052), ("R", -0.052)):
        eye = empty(f"Eye{side}", c + Vector((x, -0.128, 0.018)), head)
        sphere(f"Sclera{side}", c + Vector((x, -0.128, 0.018)), (0.026, 0.012, 0.02), sclera, eye, 24, 12)
        sphere(f"Iris{side}", c + Vector((x, -0.138, 0.018)), (0.016, 0.004, 0.017), iris, eye, 24, 12)
        sphere(f"Pupil{side}", c + Vector((x, -0.1415, 0.018)), (0.006, 0.002, 0.007), pupil, eye, 16, 8)
        bpy.ops.mesh.primitive_cube_add(size=1, location=c + Vector((x, -0.136, 0.058)))
        brow = bpy.context.active_object
        brow.scale = (0.03, 0.004, 0.004)
        brow.rotation_euler = (0, math.radians(8 if side == "L" else -8), 0)
        bpy.ops.object.transform_apply(scale=True, rotation=True)
        finish(brow, f"Brow{side}", browmat, head, smooth=False)
    mouth = empty("Mouth", c + Vector((0, -0.130, -0.075)), head)
    sphere("Lips", c + Vector((0, -0.130, -0.075)), (0.024, 0.006, 0.006), lip, mouth, 24, 12)

    # Hair: one shell over the crown that falls past the jaw like a curtain, with the face cut open.
    hair_root = empty("Hair", c, head)
    hair_shell(c + Vector((0, 0.012, 0.012)), (0.158, 0.165, 0.185), hair, hair_root)

    # Floor ring: the state light.
    bpy.ops.mesh.primitive_torus_add(major_radius=0.42, minor_radius=0.012, major_segments=96,
                                     minor_segments=12, location=(0, 0, 0.0))
    finish(bpy.context.active_object, "Ring", ring, root)
    return root


def export_glb(path):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=False, export_apply=True,
                              export_yup=True, export_cameras=False, export_lights=False)
    print(f"kyra_bust: wrote {path}")


def render_turntable(out_dir, frames, size):
    scene = bpy.context.scene
    for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            scene.render.engine = engine
            break
        except TypeError:
            continue
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("Room")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.012, 0.025, 0.045, 1)
    scene.world = world
    # The camera orbits; the character stays still (a pivot empty carries the camera).
    pivot = empty("TurntablePivot", Vector((0, 0, 0.5)))
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    bpy.context.collection.objects.link(cam)
    cam.data.lens = 60
    cam.parent = pivot
    cam.location = (0, -2.1, 0.22)
    track = cam.constraints.new("TRACK_TO")
    track.target = pivot
    scene.camera = cam
    for name, loc, energy, color in (("Key", (1.2, -1.5, 1.6), 40, (1, 0.97, 0.93)),
                                     ("Fill", (-1.4, -1.0, 0.9), 15, (0.8, 0.9, 1.0)),
                                     ("Rim", (0.0, 1.6, 1.4), 60, (0.5, 0.9, 1.0))):
        light = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
        light.data.energy = energy
        light.data.color = color
        light.data.size = 1.0
        light.location = loc
        light.constraints.new("TRACK_TO").target = pivot
        bpy.context.collection.objects.link(light)
    scene.frame_start, scene.frame_end = 1, frames
    pivot.rotation_euler = (0, 0, 0)
    pivot.keyframe_insert("rotation_euler", index=2, frame=1)
    pivot.rotation_euler = (0, 0, math.radians(360 * (frames) / (frames + 1)))
    pivot.keyframe_insert("rotation_euler", index=2, frame=frames)
    fcurves = getattr(pivot.animation_data.action, "fcurves", None)
    if fcurves is None:  # Blender 5 layered actions
        fcurves = [fc for layer in pivot.animation_data.action.layers for strip in layer.strips
                   for bag in strip.channelbags for fc in bag.fcurves]
    for fc in fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = f"{out_dir.rstrip('/')}/turntable_####"
    bpy.ops.render.render(animation=True)
    print(f"kyra_bust: rendered {frames} frames to {out_dir}")


def main():
    opts = args()
    build()
    if opts.glb:
        export_glb(opts.glb)
    if opts.render_dir:
        render_turntable(opts.render_dir, opts.frames, opts.size)


main()
