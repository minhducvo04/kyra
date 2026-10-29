"""Child process for POST /api/cad/import: python -m companion.cad.import_worker <source.step> <folder> [display name].
Writes folder/model.glb and folder/result.json ({"out": body} or {"error": reason}); the server kills it on timeout."""
import json
import sys
from pathlib import Path


def main(source: str, folder: str, filename: str = "model.step") -> int:
    try:
        from companion.cad.build import import_step_file
        result = {"out": import_step_file(Path(source), Path(folder), filename)}
    except ValueError as exc:
        result = {"error": str(exc)}
    except Exception as exc:
        result = {"error": f"the kernel could not read this STEP file: {exc}"[:300]}
    Path(folder, "result.json").write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
