#!/usr/bin/env python3
"""Render the dependency dashboard from build-reports/dependency-graph.json.

* ``--summary FILE``  markdown (with a Mermaid graph) for the GitHub Actions run
                      summary page; pass ``$GITHUB_STEP_SUMMARY`` or ``-`` for stdout.
* ``--html FILE``     self-contained HTML report, uploaded as a run artifact.
* ``--mermaid-dir D`` writes dependency-graph-full.mmd / dependency-graph-changed.mmd.

Arrow convention: ``A --> B`` means *B depends on A* (A is built first).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
BUNDLE, FEATURE = "📦", "🧩"
MARKERS = {"changed": "🔴 ", "impacted": "🟠 ", "cobuilt": "🔵 "}

CLASS_DEFS = """\
  classDef built fill:#e2e8f0,stroke:#475569,color:#0f172a
  classDef changed fill:#fecaca,stroke:#dc2626,stroke-width:3px,color:#0f172a
  classDef impacted fill:#fed7aa,stroke:#ea580c,stroke-width:2px,color:#0f172a
  classDef cobuilt fill:#dbeafe,stroke:#2563eb,color:#0f172a
  classDef skipped fill:#f8fafc,stroke:#94a3b8,stroke-dasharray:4 3,color:#64748b"""


def node_id(component_id: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9]", "_", component_id)


def chip(component: dict, highlight: bool = True) -> str:
    icon = BUNDLE if component["kind"] == "bundle" else FEATURE
    marker = MARKERS.get(component["status"], "") if highlight else ""
    return f"{marker}{icon} {component['name']}"


def mermaid_graph(data: dict, highlight: bool) -> str:
    """One box per product, bundles as rectangles, features as rounded nodes."""
    components = data["components"]
    lines = ["flowchart TB"]
    for product in data["products"]:
        members = [c for c in components.values() if c["product"] == product]
        lines.append(f'  subgraph p_{re.sub(r"[^A-Za-z0-9]", "_", product)}["{product}"]')
        for comp in sorted(members, key=lambda c: (c["kind"] != "bundle", c["id"])):
            label = comp["name"]
            shape = f'["{label}"]' if comp["kind"] == "bundle" else f'(["{label}"])'
            lines.append(f"    {node_id(comp['id'])}{shape}")
        lines.append("  end")

    # Every dependent of a changed/impacted component is itself impacted, so an edge
    # between two "hot" components is exactly an edge a change propagates along.
    hot = {c["id"] for c in components.values() if c["status"] in ("changed", "impacted")} if highlight else set()
    red_links: list[int] = []
    for index, edge in enumerate(data["componentEdges"]):
        # edge["from"] depends on edge["to"]; draw prerequisite -> dependent
        prerequisite, dependent = edge["to"], edge["from"]
        if "contains-bundle" in edge["kinds"]:
            arrow = "-.->"
        elif "requires-feature" in edge["kinds"]:
            arrow = "==>"
        else:
            arrow = "-->"
        lines.append(f"  {node_id(prerequisite)} {arrow} {node_id(dependent)}")
        if prerequisite in hot and dependent in hot:
            red_links.append(index)
    if red_links:
        lines.append(f"  linkStyle {','.join(map(str, red_links))} stroke:#dc2626,stroke-width:3px")

    lines.append(CLASS_DEFS)
    groups: dict[str, list[str]] = {}
    for comp in components.values():
        state = comp["status"] if highlight else "built"
        groups.setdefault(state, []).append(node_id(comp["id"]))
    for state, ids in sorted(groups.items()):
        lines.append(f"  class {','.join(sorted(ids))} {state}")
    return "\n".join(lines) + "\n"


def wave_rows(data: dict) -> list[tuple[int, str]]:
    components = data["components"]
    return [
        (number, " · ".join(chip(components[cid]) for cid in wave))
        for number, wave in enumerate(data["waves"], 1)
    ]


def product_order(data: dict) -> str:
    return " → ".join("[" + ", ".join(level) + "]" for level in data["productLevels"]) or "(nothing to build)"


def scenario_line(data: dict) -> str:
    components = data["components"]
    built = sum(1 for c in components.values() if c["status"] != "skipped")
    text = f"**Scenario:** `{data['effectiveMode']}`"
    if data["requestedMode"] != data["effectiveMode"]:
        text += f" (requested `{data['requestedMode']}`)"
    text += f" — {data['reason']}"
    if data["requestedMode"] == "changed":
        ignored = sum(1 for e in data["changedFiles"] if e["effect"] == "ignored")
        text += (
            f"\n\n**{len(data['changedFiles'])}** changed file(s) → **{len(data['changedComponents'])}** directly changed, "
            f"**{len(data['impactedComponents'])}** impacted dependents → **{built}** of **{len(components)}** components built"
        )
        if ignored:
            text += f" ({ignored} ignored file(s))"
    else:
        text += f"\n\n**{built}** components in **{len(data['waves'])}** waves"
    return text


def findings_table(data: dict) -> str:
    rows = ["| # | Component | Finding | Suggested fix |", "|---|---|---|---|"]
    for number, finding in enumerate(data["findings"], 1):
        name = data["components"].get(finding["subject"], {}).get("name", finding["subject"])
        message, hint = (html.escape(finding[key], quote=False) for key in ("message", "hint"))
        rows.append(f"| {number} | `{name}` | {message} | {hint} |")
    return "\n".join(rows)


def path_lines(data: dict) -> list[str]:
    components = data["components"]
    lines: list[str] = []
    for start, chains in sorted(data["componentPaths"].items()):
        lines.append(f"- 🔴 **{components[start]['name']}** (changed)")
        if not chains:
            lines.append("  - nothing depends on it")
        for chain in chains:
            lines.append("  - " + " → ".join(components[cid]["name"] for cid in chain))
    return lines


def legend_items(data: dict) -> str:
    items = []
    if data["effectiveMode"] == "changed" or data["changedComponents"]:
        items += ["🔴 directly changed"]
    if data["effectiveMode"] == "changed":
        items += [
            "🟠 rebuilt because a dependency changed",
            "🔵 co-built (prerequisite, or shares a product with a changed component — Maven builds per product)",
        ]
    return " · ".join(items + ["📦 bundle", "🧩 feature"])


LEGEND_NOTE = (
    "Arrows point from a dependency to the component that needs it (A → B: B depends on A, so A is built first). "
    "Solid = Import-Package / Require-Bundle · thick = feature requires feature · dotted = feature contains bundle."
)


def legend_markdown(data: dict) -> str:
    note = LEGEND_NOTE
    if data["effectiveMode"] == "changed":
        note += " Red arrows trace how a change propagates; dashed grey nodes are not rebuilt."
    return f"{legend_items(data)}\n\n_{note}_"


def summary_markdown(data: dict, artifact_url: str | None) -> str:
    changed_run = data["effectiveMode"] == "changed"
    out = ["## 🧭 OSGi build plan", "", scenario_line(data), ""]

    if not data["waves"]:
        out += ["Nothing to build for this change.", ""]
    else:
        out += ["### Build waves", "", "| Wave | Components (independent — can build in parallel) |", "|---|---|"]
        out += [f"| **{number}** | {cells} |" for number, cells in wave_rows(data)]
        out += ["", legend_markdown(data), ""]

    heading = "Dependency graph" + (" — this run (changed scenario)" if changed_run else " — full build")
    out += [f"### {heading}", "", "```mermaid", mermaid_graph(data, highlight=True).rstrip(), "```", ""]
    out += [f"**Product build order:** {product_order(data)}", ""]

    if changed_run and data["changedComponents"]:
        out += [
            "<details><summary>Dependency path of every changed component</summary>", "",
            *path_lines(data), "", "</details>", "",
        ]
        out += [
            "<details><summary>Full-build dependency graph (all components)</summary>", "",
            "```mermaid", mermaid_graph(data, highlight=False).rstrip(), "```", "", "</details>", "",
        ]
    out += [
        f"<details><summary>Product consistency findings ({len(data['findings'])})</summary>", "",
        findings_table(data) if data["findings"] else "No findings.", "", "</details>", "",
    ]
    if artifact_url:
        out += [f"📎 [Full HTML report]({artifact_url}) is attached to this run as an artifact.", ""]
    else:
        out += ["📎 The full HTML report is attached to this run as an artifact.", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #

HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Northwind OMS – dependency report</title>
<style>
:root{--bg:#fff;--fg:#0f172a;--muted:#64748b;--card:#f8fafc;--line:#e2e8f0;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b1220;--fg:#e2e8f0;--muted:#94a3b8;--card:#111a2e;--line:#243049;--accent:#60a5fa}}
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:24px;max-width:1280px;margin-inline:auto}
h1{margin:0 0 4px}h2{margin-top:32px;border-bottom:1px solid var(--line);padding-bottom:6px}
.muted{color:var(--muted)}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;min-width:120px}
.card b{display:block;font-size:24px}
table{border-collapse:collapse;width:100%;margin:8px 0}th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--card)}code,pre{font:13px ui-monospace,Menlo,monospace}
.graph{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px}
details{margin:10px 0}summary{cursor:pointer;font-weight:600}
ul{margin:4px 0;padding-left:22px}
</style></head><body>
<h1>Northwind OMS – dependency report</h1>
<div class="muted">%%SCENARIO%%</div>
<div class="cards">%%CARDS%%</div>
<p>%%LEGEND%%</p>
<h2>Build waves</h2>
%%WAVES%%
<p><b>Product build order:</b> %%ORDER%%</p>
%%GRAPHS%%
%%PATHS%%
<h2>Product consistency findings (%%FINDING_COUNT%%)</h2>
%%FINDINGS%%
<h2>All dependencies</h2>
<p class="muted">Each row: <i>dependent</i> needs <i>dependency</i>.</p>
%%EDGES%%
<h2>Changed files</h2>
%%FILES%%
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
if (window.mermaid) {
  mermaid.initialize({startOnLoad:true, securityLevel:'loose', theme:'default', flowchart:{useMaxWidth:false}});
} else {
  document.querySelectorAll('.graph').forEach(g => g.insertAdjacentHTML('afterbegin',
    '<p class="muted">Mermaid could not be loaded (offline?). The graph source is below.</p>'));
}
</script></body></html>
"""


def esc(value: object) -> str:
    return html.escape(str(value))


def html_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def html_report(data: dict) -> str:
    components = data["components"]
    names = lambda ids: ", ".join(esc(components[i]["name"]) for i in ids)  # noqa: E731
    built = sum(1 for c in components.values() if c["status"] != "skipped")
    cards = [
        ("Components", len(components)), ("Built", built), ("Waves", len(data["waves"])),
        ("Directly changed", len(data["changedComponents"])), ("Impacted", len(data["impactedComponents"])),
        ("Findings", len(data["findings"])),
    ]
    graphs = []
    sections = [("This run" + (" (changed scenario)" if data["effectiveMode"] == "changed" else " (full build)"), True)]
    if data["effectiveMode"] == "changed":
        sections.append(("Full build (all components)", False))
    for title, highlight in sections:
        source = mermaid_graph(data, highlight)
        graphs.append(
            f"<h2>Dependency graph – {esc(title)}</h2><div class='graph'><pre class='mermaid'>{esc(source)}</pre></div>"
            f"<details><summary>Mermaid source</summary><pre>{esc(source)}</pre></details>"
        )

    paths = ""
    if data["componentPaths"]:
        items = []
        for start, chains in sorted(data["componentPaths"].items()):
            inner = "".join("<li>" + " → ".join(esc(components[c]["name"]) for c in chain) + "</li>" for chain in chains)
            items.append(f"<li>🔴 <b>{esc(components[start]['name'])}</b><ul>{inner or '<li>nothing depends on it</li>'}</ul></li>")
        paths = f"<h2>Dependency path of every changed component</h2><ul>{''.join(items)}</ul>"

    findings = html_table(
        ["#", "Component", "Finding", "Suggested fix"],
        [[str(n), f"<code>{esc(components.get(f['subject'], {}).get('name', f['subject']))}</code>",
          esc(f["message"]), esc(f["hint"])] for n, f in enumerate(data["findings"], 1)],
    ) if data["findings"] else "<p>No findings.</p>"

    edges = html_table(
        ["Dependent", "Needs (dependency)", "Via", "Detail"],
        [[esc(components[e["from"]]["name"]), esc(components[e["to"]]["name"]),
          esc(", ".join(e["kinds"])), esc(", ".join(e["details"]))] for e in data["componentEdges"]],
    )
    files = html_table(
        ["File", "Effect"],
        [[f"<code>{esc(e['path'])}</code>", names(e["targets"]) if e["targets"] else esc(e["effect"])]
         for e in data["changedFiles"]],
    ) if data["changedFiles"] else "<p class='muted'>Full build requested – no change detection.</p>"

    waves = html_table(
        ["Wave", "Components (independent – can build in parallel)"],
        [[f"<b>{n}</b>", esc(cells)] for n, cells in wave_rows(data)],
    ) if data["waves"] else "<p>Nothing to build.</p>"

    replacements = {
        "SCENARIO": esc(f"{data['effectiveMode']} build – {data['reason']}"),
        "CARDS": "".join(f"<div class='card'><b>{v}</b>{esc(k)}</div>" for k, v in cards),
        "LEGEND": esc(legend_items(data) + ". " + LEGEND_NOTE),
        "WAVES": waves, "ORDER": esc(product_order(data)), "GRAPHS": "".join(graphs), "PATHS": paths,
        "FINDING_COUNT": str(len(data["findings"])), "FINDINGS": findings, "EDGES": edges, "FILES": files,
    }
    page = HTML_TEMPLATE
    for key, value in replacements.items():
        page = page.replace(f"%%{key}%%", value)
    return page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=PROJECT_DIR / "build-reports" / "dependency-graph.json")
    parser.add_argument("--summary", help="Markdown destination ('-' = stdout). Appended to, so $GITHUB_STEP_SUMMARY is safe.")
    parser.add_argument("--html", type=Path, help="Write the full HTML report here.")
    parser.add_argument("--mermaid-dir", type=Path, help="Write dependency-graph-{full,changed}.mmd here.")
    parser.add_argument("--artifact-url", help="Link to the uploaded report artifact.")
    args = parser.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))

    if args.mermaid_dir:
        args.mermaid_dir.mkdir(parents=True, exist_ok=True)
        (args.mermaid_dir / "dependency-graph-full.mmd").write_text(mermaid_graph(data, False), encoding="utf-8")
        if data["effectiveMode"] == "changed":
            (args.mermaid_dir / "dependency-graph-changed.mmd").write_text(mermaid_graph(data, True), encoding="utf-8")
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(html_report(data), encoding="utf-8")
        print(f"HTML report written to {args.html}")
    if args.summary:
        text = summary_markdown(data, args.artifact_url)
        if args.summary == "-":
            sys.stdout.write(text)
        else:
            with open(args.summary, "a", encoding="utf-8") as stream:
                stream.write(text + "\n")
            print(f"Run summary appended to {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
