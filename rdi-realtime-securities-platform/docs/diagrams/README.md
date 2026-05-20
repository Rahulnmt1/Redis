# Architecture diagrams

These are real PNG + SVG images (no Mermaid plugin required) generated
from [`generate_diagrams.py`](generate_diagrams.py) using the
[`diagrams`](https://diagrams.mingrammer.com/) Python library on top of
Graphviz.

| File | What it shows |
|---|---|
| `architecture.png` / `.svg` | High-level component layout + the six headline data-flow arrows (A–F) |
| `cdc-write-path.png` / `.svg` | What happens when a Postgres row changes — seven numbered steps from `INSERT` to "trader sees it" |
| `dashboard-read-path.png` / `.svg` | What happens when a trader loads their portfolio — six numbered steps, all sub-ms |

## Regenerating the diagrams

You need Graphviz and the Python `diagrams` package.

```bash
# 1. Install Graphviz (macOS)
brew install graphviz
# On Linux/WSL: sudo apt-get install graphviz

# 2. Create a venv and install the diagrams library
python3 -m venv .venv
.venv/bin/pip install diagrams

# 3. Generate (from the repo root)
.venv/bin/python3 docs/diagrams/generate_diagrams.py
```

PNG and SVG files are written next to `generate_diagrams.py`. Commit
both PNG (for GitHub rendering) and SVG (for crisp zoom in the deck).

## Editing the diagrams

Open [`generate_diagrams.py`](generate_diagrams.py) — the three
`Diagram(...)` blocks at the bottom are independent and read top-to-bottom.
Edge labels include the headline letters (A–F) and step numbers (① ② ③)
so the diagrams stay in sync with the explanatory tables in
[`../../README.md`](../../README.md).

> ⚑ When you add or rename a component, also update the workflow tables
> in `README.md` so the numbered/lettered steps still match the picture.
