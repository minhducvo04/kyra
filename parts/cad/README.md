# CAD

**What it does.** A small parametric CAD kernel. A part is a JSON spec of sketches and features; the kernel validates it, solves its sketches, builds it with build123d (OpenCascade B-rep) and exports GLB and STEP. Assemblies, mates, drawings, sheet metal and mass properties sit on top of that.

**Key idea.** Validation and building are separate. The spec checks import no kernel, so a spec can be checked where build123d is not installed, and every problem comes back as a reason tied to a feature id. A model may propose a spec from words, but only the validator and the kernel decide whether it builds.

The HTTP routes that serve these modules in the private web app are not included here.

## How it works

- **Spec.** [`spec.py`](../../src/companion/cad/spec.py) validates a part spec with no kernel import. [`variables.py`](../../src/companion/cad/variables.py) resolves bounded arithmetic in variables and configurations without executing source.
- **Sketches.** [`constraints.py`](../../src/companion/cad/constraints.py) is a pure 2D constraint solver: rough positions and constraints in, exact positions or a refusal per constraint id out. Redundant but consistent constraints are accepted and listed.
- **Build.** [`build.py`](../../src/companion/cad/build.py) builds a valid spec with build123d and exports GLB and STEP. Problems the validator cannot see, such as a hole off the solid or a fillet that is too large, come back on that feature id, and a failed build writes nothing.
- **Features.** [`holes.py`](../../src/companion/cad/holes.py) covers metric coarse threads from M2 to M20 and refuses other sizes. [`threads.py`](../../src/companion/cad/threads.py) adds cosmetic or modelled threads and helixes. [`sheet.py`](../../src/companion/cad/sheet.py) computes bend allowance from rough textbook K factors.
- **Assemblies.** [`assembly.py`](../../src/companion/cad/assembly.py) validates an assembly spec and its mates. [`interference.py`](../../src/companion/cad/interference.py) finds exact intersections at a driven pose, and [`bom.py`](../../src/companion/cad/bom.py) lists the bill of materials.
- **Drawings.** [`drawing.py`](../../src/companion/cad/drawing.py) makes orthographic A4 sheets from the built solid, so each dimension on the sheet is measured from the real B-rep.
- **Proposals.** [`propose.py`](../../src/companion/cad/propose.py) lets a model propose a spec from words, with one first call and at most two repairs that carry the exact refusals. [`worked_examples.py`](../../src/companion/cad/worked_examples.py) picks a worked example for the prompt.

## Main files

| File | Purpose |
|---|---|
| [`cad/spec.py`](../../src/companion/cad/spec.py) | Part spec validation, no kernel import |
| [`cad/constraints.py`](../../src/companion/cad/constraints.py) | 2D sketch constraint solver |
| [`cad/build.py`](../../src/companion/cad/build.py) | build123d build, GLB and STEP export |
| [`cad/assembly.py`](../../src/companion/cad/assembly.py) | Assembly spec and mates |
| [`cad/drawing.py`](../../src/companion/cad/drawing.py) | Orthographic drawing sheets |
| [`cad/propose.py`](../../src/companion/cad/propose.py) | Spec proposals from words, checked by the kernel |

## Tests

```sh
python -m pip install -e '.[test,cad]'
python -m pytest -q tests/test_cad_*.py
```

Tests that build solids need build123d, which the `cad` extra installs.
