# Kyra

Kyra is a personal AI system that runs on one owner's machines. It has a long-term memory and a team of AI coding agents that a Board and planners coordinate. Hosted and local models sit behind a router with a spend cap, and a landing gate merges agent work only after its tests pass. Kyra also has a local voice pipeline, a 3D viewer with an animated bust of herself, and a CAD kernel.

This repository holds the parts of Kyra that carry no personal data, with their tests. It is a set of components taken out of a larger private application, so some seams that need the private parts refuse instead of running. Each part has its own README.

![How the parts of Kyra connect: the owner talks to voice and the turn router, which picks local or hosted models and reads memory; a request becomes a feature DAG, lanes of coding agents do the work, and the Board inbox and landing gate decide what reaches master; the 3D viewer and CAD kernel make things](docs/img/architecture.png)

<sub>Diagram source: [docs/img/architecture.mmd](docs/img/architecture.mmd) (Mermaid).</sub>

| Part | What it does | README |
|---|---|---|
| Memory | Versioned, tenant-isolated memory with budgeted context packs and real erasure | [packages/kyra-memory](packages/kyra-memory/README.md) |
| Agent team | Feature DAGs, lane picking, file leases, sending work to agent CLIs, the Board inbox, QC rules, agent audit | [parts/agent-team](parts/agent-team/README.md) |
| Models and routing | Turn router, labelled hosted request gate, spend cap, local model manager, model-free decision gates | [parts/models](parts/models/README.md) |
| Landing gate | Queue, trains, test shards across machines, the merge gate, master keeper | [parts/landing](parts/landing/README.md) |
| Voice | Local speech to text and text to speech, push to talk and VAD capture, the spoken register | [parts/voice](parts/voice/README.md) |
| 3D | three.js viewer, a mate solver in Python and JS, gear and flow lessons, the Blender bust | [parts/3d](parts/3d/README.md) |
| CAD | Part spec validation, a 2D sketch solver, build123d builds to GLB and STEP, drawings, assemblies | [parts/cad](parts/cad/README.md) |

## Two things you can see

<img src="docs/img/kyra-states.gif" width="300" alt="Kyra's 3D bust cycling through idle, listening, thinking and speaking"> <img src="docs/img/memory-quickstart.png" width="480" alt="Terminal output of the memory quickstart: two tenants each find only their own fact, and a forgotten fact no longer turns up in search">

Left: the Blender bust in the [3D viewer](parts/3d/README.md), rendered headless from this repository's `web/` files. Right: the [memory](packages/kyra-memory/README.md) quickstart. Two tenants each find only their own fact, and once `acme` forgets its fact, searching for it finds nothing.

## Tests

Python 3.11 or newer, from the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test,cad]'
python -m pytest -q -n 2
```

From a fresh clone and a new virtual environment (macOS, Python 3.12, with Git and Node on the path), the whole suite gave **2536 passed and 31 skipped**. Each part README has a narrower command that runs after this install.

## What is not here

The owner's data, settings, keys and private documents, and every feature that handles personal records, stay in the private repository. So do the web app backend, the dispatcher loop that runs the agent team, and the private release policy. In this package the default hosted request gate refuses to send, and the local task catalogue raises an error saying it is not included. Some code paths read files under a data directory at run time; tests point that directory at a temporary folder.

## License

[MIT](LICENSE)
