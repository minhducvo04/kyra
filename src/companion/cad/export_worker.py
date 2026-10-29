"""Isolated STL conversion. Output and CPU limits supplement the parent's kill deadline."""
import sys

try:
    import resource
except ImportError:
    if sys.platform != 'win32':
        raise
    resource = None


def main():
    from companion.cad.export import MAX_TRIANGLES
    if resource is not None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (84 + 50 * MAX_TRIANGLES,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    from build123d import export_stl, import_step
    shape = import_step(sys.argv[1])
    return 0 if export_stl(shape, sys.argv[2], tolerance=0.05,
                           angular_tolerance=0.1, ascii_format=False) else 1


if __name__ == '__main__':
    raise SystemExit(main())
