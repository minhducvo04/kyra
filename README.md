# Kyra

Kyra is a personal AI system that runs on one owner's machines. It has a long-term memory and a team of AI coding agents that a Board and planners coordinate. Hosted and local models sit behind a router with a spend cap, and a landing gate merges agent work only after its tests pass. Kyra also has a local voice pipeline, a 3D viewer with an animated bust of herself, and a CAD kernel.

This repository holds the parts of Kyra that carry no personal data, with their tests. It is a set of components taken out of a larger private application, so some seams that need the private parts refuse instead of running. Each part has its own README.

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="docs/img/architecture-narrow-dark.svg">
  <source media="(max-width: 600px)" srcset="docs/img/architecture-narrow-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/architecture-wide-dark.svg">
  <source srcset="docs/img/architecture-wide-light.svg">
  <img src="docs/img/architecture.png" width="960" alt="How Kyra fits together. The owner talks through voice or text to a turn router that picks a local model or a hosted one behind a labelled gate with a daily spend cap. A feature request goes through Plan, Build, Test and Land to master, and the keeper holds the queue when master is red. Memory gives the router and the agent lanes context packs and takes inbox lines back. The CAD kernel builds GLB files for the 3D viewer.">
</picture>

<sub>Diagram source: [docs/img/architecture.py](docs/img/architecture.py) writes the four SVG files (wide and narrow, light and dark).</sub>

| Part | What it does | README |
|---|---|---|
| Memory | Versioned, tenant-isolated memory with budgeted context packs and real erasure | [packages/kyra-memory](packages/kyra-memory/README.md) |
| Agent team | Feature DAGs, lane picking, file leases, sending work to agent CLIs, the Board inbox, QC rules, agent audit | [parts/agent-team](parts/agent-team/README.md) |
| Models and routing | Turn router, labelled hosted request gate, spend cap, local model manager, model-free decision gates | [parts/models](parts/models/README.md) |
| Landing gate | Queue, trains, test shards across machines, the merge gate, master keeper | [parts/landing](parts/landing/README.md) |
| Voice | Local speech to text and text to speech, push to talk and VAD capture, the spoken register | [parts/voice](parts/voice/README.md) |
| 3D | three.js viewer, a mate solver in Python and JS, gear and flow lessons, the Blender bust | [parts/3d](parts/3d/README.md) |
| CAD | Part spec validation, a 2D sketch solver, build123d builds to GLB and STEP, drawings, assemblies | [parts/cad](parts/cad/README.md) |

## What the memory quickstart prints

<img src="docs/img/memory-quickstart.png" width="560" alt="Terminal output of the memory quickstart: two tenants each find only their own fact, and a forgotten fact no longer turns up in search">

Two tenants each find only their own fact, and once `acme` forgets its fact, searching for it finds nothing. The code is in [packages/kyra-memory](packages/kyra-memory/README.md).

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
