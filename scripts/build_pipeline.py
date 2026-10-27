#!/usr/bin/env python3
"""Plan OSGi/Maven builds and emit the dependency data behind the run-summary dashboard.

Edge convention used throughout: ``{"from": A, "to": B}`` means *A depends on B*
(B must be built first).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]

# Files that never affect a build. Anything else outside a product folder
# (root pom.xml, scripts/, .github/, ...) can change how everything is built,
# so it forces a full build.
IGNORED_NAMES = {".DS_Store", ".gitignore", ".gitattributes"}
IGNORED_SUFFIXES = (".md",)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(element: ET.Element, name: str) -> str | None:
    return next((child.text.strip() for child in element if local_name(child.tag) == name and child.text), None)


def read_xml(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise RuntimeError(f"Unable to parse {path.relative_to(PROJECT_DIR)}: {error}") from error


def parse_manifest(path: Path) -> dict[str, str]:
    headers: dict[str, str] = {}
    current: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(f"Unable to read {path.relative_to(PROJECT_DIR)}: {error}") from error

    for line in lines:
        if line.startswith(" ") and current:
            headers[current] += line[1:]
        elif ":" in line:
            current, value = line.split(":", 1)
            headers[current] = value.strip()
        else:
            current = None
    return headers


def clauses(header: str) -> list[str]:
    """Split a manifest header on commas that are not inside quotes.

    ``version="[1.0.0,2.0.0)"`` contains a comma that must not split the clause.
    """
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in header:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def bundle_id(value: str) -> str:
    return value.split(";", 1)[0].strip()


def parse_projects(root: ET.Element, parent: str = ".") -> tuple[dict[str, dict], dict[str, list[str]]]:
    projects: dict[str, dict] = {}
    children_by_product: dict[str, list[str]] = defaultdict(list)

    def visit(pom: Path, product: str | None) -> None:
        relative = pom.parent.relative_to(PROJECT_DIR).as_posix()
        relative = "." if relative == "." else relative
        element = read_xml(pom)
        artifact = child_text(element, "artifactId") or relative
        group = child_text(element, "groupId")
        if not group:
            parent_element = next((item for item in element if local_name(item.tag) == "parent"), None)
            group = child_text(parent_element, "groupId") if parent_element is not None else None
        projects[relative] = {"artifactId": artifact, "groupId": group, "product": product}

        module_names = [
            item.text.strip()
            for item in element.iter()
            if local_name(item.tag) == "module" and item.text and item is not element
        ]
        for module_name in module_names:
            module_pom = (pom.parent / module_name / "pom.xml").resolve()
            if not module_pom.is_file():
                raise RuntimeError(f"Maven module has no POM: {module_pom}")
            module_relative = module_pom.parent.relative_to(PROJECT_DIR).as_posix()
            module_product = product or module_name
            children_by_product[module_product].append(module_relative)
            visit(module_pom, module_product)

    root_pom = PROJECT_DIR / "pom.xml"
    for module in [
        item.text.strip()
        for item in root.iter()
        if local_name(item.tag) == "module" and item.text
    ]:
        visit(root_pom.parent / module / "pom.xml", module)
    return projects, children_by_product


def discover_graph() -> tuple[list[str], dict[str, dict], list[dict], list[dict]]:
    root = read_xml(PROJECT_DIR / "pom.xml")
    products = [
        item.text.strip()
        for item in root.iter()
        if local_name(item.tag) == "module" and item.text
    ]
    projects, product_projects = parse_projects(root)
    bundles: dict[str, dict] = {}
    package_owners: dict[str, str] = {}
    features: dict[str, dict] = {}

    for path in sorted(PROJECT_DIR.glob("**/META-INF/MANIFEST.MF")):
        if "target" in path.relative_to(PROJECT_DIR).parts:
            continue  # build output, not source
        headers = parse_manifest(path)
        symbolic_name = bundle_id(headers.get("Bundle-SymbolicName", ""))
        if not symbolic_name:
            continue
        product = path.relative_to(PROJECT_DIR).parts[0]
        exports = [bundle_id(item) for item in clauses(headers.get("Export-Package", ""))]
        bundle = {
            "id": symbolic_name,
            "product": product,
            "path": path.relative_to(PROJECT_DIR).as_posix(),
            "dir": path.parent.parent.relative_to(PROJECT_DIR).as_posix(),
            "headers": headers,
            "exports": exports,
        }
        bundles[symbolic_name] = bundle
        for package in exports:
            package_owners[package] = symbolic_name

    for path in sorted(PROJECT_DIR.glob("**/feature.xml")):
        if "target" in path.relative_to(PROJECT_DIR).parts:
            continue
        element = read_xml(path)
        feature_id = element.attrib.get("id")
        if feature_id:
            features[feature_id] = {
                "id": feature_id,
                "product": path.relative_to(PROJECT_DIR).parts[0],
                "path": path.relative_to(PROJECT_DIR).as_posix(),
                "dir": path.parent.relative_to(PROJECT_DIR).as_posix(),
                "xml": element,
            }

    osgi_edges: list[dict] = []
    product_dependencies: dict[str, set[str]] = {product: set() for product in products}
    for bundle in bundles.values():
        headers = bundle["headers"]
        for package_clause in clauses(headers.get("Import-Package", "")):
            package = bundle_id(package_clause)
            provider_id = package_owners.get(package)
            if provider_id:
                provider = bundles[provider_id]
                if provider_id != bundle["id"]:
                    osgi_edges.append({
                        "from": bundle["id"],
                        "to": provider_id,
                        "kind": "imports-package",
                        "detail": package,
                        "fromProduct": bundle["product"],
                        "toProduct": provider["product"],
                    })
                    if provider["product"] != bundle["product"]:
                        product_dependencies[bundle["product"]].add(provider["product"])
        for required_clause in clauses(headers.get("Require-Bundle", "")):
            required_id = bundle_id(required_clause)
            provider = bundles.get(required_id)
            if provider:
                osgi_edges.append({
                    "from": bundle["id"],
                    "to": required_id,
                    "kind": "requires-bundle",
                    "detail": required_id,
                    "fromProduct": bundle["product"],
                    "toProduct": provider["product"],
                })
                if provider["product"] != bundle["product"]:
                    product_dependencies[bundle["product"]].add(provider["product"])

    feature_edges: list[dict] = []
    for feature in features.values():
        for item in feature["xml"].iter():
            name = local_name(item.tag)
            if name == "import" and item.attrib.get("feature") in features:
                target = features[item.attrib["feature"]]
                feature_edges.append({
                    "from": feature["id"],
                    "to": target["id"],
                    "kind": "requires-feature",
                    "fromProduct": feature["product"],
                    "toProduct": target["product"],
                })
                if feature["product"] != target["product"]:
                    product_dependencies[feature["product"]].add(target["product"])
            elif name == "plugin":
                plugin_id = item.attrib.get("id")
                plugin = bundles.get(plugin_id or "")
                if plugin:
                    feature_edges.append({
                        "from": feature["id"],
                        "to": plugin_id,
                        "kind": "contains-bundle",
                        "fromProduct": feature["product"],
                        "toProduct": plugin["product"],
                    })

    artifact_projects: dict[str, str] = {}
    for project_path, project in projects.items():
        artifact_projects[project["artifactId"]] = project_path
    maven_edges: list[dict] = []
    for project_path, project in projects.items():
        if project_path == ".":
            continue
        pom = PROJECT_DIR / project_path / "pom.xml"
        element = read_xml(pom)
        for dependency in (node for node in element.iter() if local_name(node.tag) == "dependency"):
            artifact = child_text(dependency, "artifactId")
            dependency_path = artifact_projects.get(artifact or "")
            if dependency_path and dependency_path != project_path:
                maven_edges.append({
                    "from": project_path,
                    "to": dependency_path,
                    "kind": "maven-dependency",
                })
    for product, project_paths in product_projects.items():
        product_path = product
        for project_path in project_paths:
            if project_path != product_path:
                maven_edges.append({
                    "from": product_path,
                    "to": project_path,
                    "kind": "contains-module",
                })

    return products, {
        "projects": projects,
        "productProjects": product_projects,
        "bundles": bundles,
        "features": features,
        "packageOwners": package_owners,
        "productDependencies": product_dependencies,
    }, maven_edges, osgi_edges + feature_edges


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #

def git(project_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def resolve_base(project_dir: Path, base: str) -> str | None:
    """Return a ref git can resolve (tries ``base`` then ``origin/base``)."""
    for candidate in (base, f"origin/{base}"):
        try:
            git(project_dir, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            return candidate
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def changed_paths(base: str | None, project_dir: Path = PROJECT_DIR) -> list[str] | None:
    """Changed files relative to ``project_dir``; ``None`` if the diff cannot be computed.

    ``None`` (unknown) and ``[]`` (nothing changed) are deliberately different:
    unknown means "build everything to be safe".
    """
    if not base or set(base) == {"0"}:  # empty, or the all-zero SHA of a new branch
        print(f"No usable base revision ({base!r}); cannot detect changes.", file=sys.stderr)
        return None
    resolved = resolve_base(project_dir, base)
    if resolved is None:
        print(f"Base revision {base!r} not found in this clone; cannot detect changes.", file=sys.stderr)
        return None
    try:
        top = Path(git(project_dir, "rev-parse", "--show-toplevel").strip()).resolve()
        output = git(project_dir, "diff", "--name-only", f"{resolved}...HEAD")
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"git diff against {resolved!r} failed ({error}); cannot detect changes.", file=sys.stderr)
        return None

    prefix = project_dir.resolve().relative_to(top).as_posix()
    paths: list[str] = []
    for line in (item.strip() for item in output.splitlines()):
        if not line:
            continue
        if prefix == ".":
            paths.append(line)
        elif line.startswith(prefix + "/"):
            paths.append(line[len(prefix) + 1:])
        # files outside the project folder cannot affect this build
    return paths


def classify_changes(paths: list[str], products: list[str], components: dict[str, dict]) -> dict:
    """Map changed files to the bundles/features they belong to."""
    entries: list[dict] = []
    changed: set[str] = set()
    full_triggers: list[str] = []
    for raw in paths:
        path = raw.replace("\\", "/")
        parts = path.split("/")
        if parts[-1] in IGNORED_NAMES or path.endswith(IGNORED_SUFFIXES):
            entries.append({"path": path, "effect": "ignored", "targets": []})
        elif parts[0] in products:
            owned = sorted(cid for cid, comp in components.items() if path.startswith(comp["dir"] + "/"))
            effect = "component"
            if not owned:  # e.g. catalog/pom.xml: belongs to the whole product
                owned = sorted(cid for cid, comp in components.items() if comp["product"] == parts[0])
                effect = "product"
            changed.update(owned)
            entries.append({"path": path, "effect": effect, "targets": owned})
        else:
            full_triggers.append(path)
            entries.append({"path": path, "effect": "all", "targets": []})
    reason = None
    if full_triggers:
        shown = ", ".join(sorted(full_triggers)[:3]) + (" ..." if len(full_triggers) > 3 else "")
        reason = f"root build or pipeline files changed ({shown}); building everything"
    return {"entries": entries, "components": changed, "full_reason": reason}


# --------------------------------------------------------------------------- #
# Component graph, waves, impact
# --------------------------------------------------------------------------- #

def short_name(component_id: str) -> str:
    for prefix in ("com.northwind.oms.tpcl.", "com.northwind.oms."):
        if component_id.startswith(prefix):
            return component_id[len(prefix):]
    return component_id


def build_components(graph: dict, edges: list[dict]):
    """Return (components, prerequisites, component_edges) for bundles and features."""
    components: dict[str, dict] = {}
    for kind, items in (("bundle", graph["bundles"]), ("feature", graph["features"])):
        for item in items.values():
            components[item["id"]] = {
                "id": item["id"], "name": short_name(item["id"]), "kind": kind,
                "product": item["product"], "dir": item["dir"],
            }
    prerequisites: dict[str, set[str]] = {cid: set() for cid in components}
    merged: dict[tuple[str, str], dict] = {}
    for edge in edges:
        source, target = edge["from"], edge["to"]
        if source == target or source not in components or target not in components:
            continue
        prerequisites[source].add(target)
        info = merged.setdefault((source, target), {"kinds": set(), "details": set()})
        info["kinds"].add(edge["kind"])
        if edge.get("detail"):
            info["details"].add(edge["detail"])
    component_edges = [
        {"from": source, "to": target, "kinds": sorted(info["kinds"]), "details": sorted(info["details"])}
        for (source, target), info in sorted(merged.items())
    ]
    return components, prerequisites, component_edges


def layers(prerequisites: dict[str, set[str]], what: str) -> list[list[str]]:
    """Group nodes into waves: a node's wave is 1 + the deepest wave of its prerequisites.

    Everything in one wave is independent and could build in parallel.
    """
    remaining = {node: set(items) for node, items in prerequisites.items()}
    done: set[str] = set()
    waves: list[list[str]] = []
    while remaining:
        ready = sorted(node for node, items in remaining.items() if items <= done)
        if not ready:
            raise RuntimeError(f"Circular {what} dependency among: {', '.join(sorted(remaining))}")
        waves.append(ready)
        done.update(ready)
        for node in ready:
            del remaining[node]
    return waves


def impact(changed: set[str], dependents: dict[str, set[str]]) -> dict[str, list[list[str]]]:
    """Shortest dependency chain from each changed node to every node that depends on it."""
    result: dict[str, list[list[str]]] = {}
    for start in sorted(changed):
        parent: dict[str, str | None] = {start: None}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for following in sorted(dependents.get(current, ())):
                if following not in parent:
                    parent[following] = current
                    queue.append(following)
        chains = []
        for node in sorted(parent):
            if node == start:
                continue
            chain, cursor = [], node
            while cursor is not None:
                chain.append(cursor)
                cursor = parent[cursor]
            chains.append(chain[::-1])
        result[start] = chains
    return result


def select_products(products: list[str], dependencies: dict[str, set[str]], changed: set[str]):
    """Changed products + everything that depends on them + their prerequisites, in build order."""
    dependents: dict[str, set[str]] = defaultdict(set)
    for consumer, prerequisites in dependencies.items():
        for prerequisite in prerequisites:
            dependents[prerequisite].add(consumer)

    impacted = set(changed)
    pending = list(changed)
    while pending:
        for dependent in dependents[pending.pop()]:
            if dependent not in impacted:
                impacted.add(dependent)
                pending.append(dependent)

    build = set(impacted)
    pending = list(impacted)
    while pending:
        for prerequisite in dependencies[pending.pop()]:
            if prerequisite not in build:
                build.add(prerequisite)
                pending.append(prerequisite)

    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(product: str) -> None:
        if product in visited:
            return
        if product in visiting:
            raise RuntimeError(f"Circular product dependency detected at {product}")
        visiting.add(product)
        for prerequisite in sorted(dependencies[product]):
            if prerequisite in build:
                visit(prerequisite)
        visiting.remove(product)
        visited.add(product)
        ordered.append(product)

    for product in products:
        if product in build:
            visit(product)
    return ordered, impacted


# --------------------------------------------------------------------------- #
# Consistency checks
# --------------------------------------------------------------------------- #

def consistency_findings(graph: dict, components: dict, prerequisites: dict, edges: list[dict]) -> list[dict]:
    """Checks that product metadata (features) agrees with what the bundles really need."""
    bundles, features = graph["bundles"], graph["features"]
    kinds_by_edge = {(e["from"], e["to"]): e["kinds"] for e in edges}
    contains: dict[str, set[str]] = {fid: set() for fid in features}
    requires: dict[str, set[str]] = {fid: set() for fid in features}
    findings: list[dict] = []

    for fid, feature in features.items():
        for item in feature["xml"].iter():
            tag = local_name(item.tag)
            if tag == "plugin" and item.attrib.get("id"):
                if item.attrib["id"] in bundles:
                    contains[fid].add(item.attrib["id"])
                else:
                    findings.append({
                        "code": "unknown-plugin", "subject": fid,
                        "message": f"{short_name(fid)} lists plugin {item.attrib['id']} which is not in this workspace",
                        "hint": "Remove the <plugin> entry or add the bundle.",
                    })
            elif tag == "import" and item.attrib.get("feature") in features:
                requires[fid].add(item.attrib["feature"])
            elif tag == "import" and item.attrib.get("plugin") in bundles:
                contains[fid].add(item.attrib["plugin"])

    closure_cache: dict[str, set[str]] = {}

    def closure(fid: str, trail: tuple[str, ...] = ()) -> set[str]:
        if fid in closure_cache:
            return closure_cache[fid]
        found = set(contains[fid])
        for required in requires[fid]:
            if required not in trail + (fid,):
                found |= closure(required, trail + (fid,))
        closure_cache[fid] = found
        return found

    features_of: dict[str, list[str]] = defaultdict(list)
    for fid, members in contains.items():
        for member in members:
            features_of[member].append(fid)

    for bundle_id_value in sorted(bundles):
        owning = sorted(features_of.get(bundle_id_value, []))
        if not owning:
            findings.append({
                "code": "bundle-without-feature", "subject": bundle_id_value,
                "message": f"{short_name(bundle_id_value)} is not part of any feature",
                "hint": "Add it to a feature.xml, otherwise it is not packaged into the product.",
            })
            continue
        for needed in sorted(prerequisites[bundle_id_value]):
            if needed not in bundles or any(needed in closure(fid) for fid in owning):
                continue
            via = " / ".join(
                "Require-Bundle" if kind == "requires-bundle" else "Import-Package"
                for kind in kinds_by_edge.get((bundle_id_value, needed), [])
            )
            suppliers = sorted(features_of.get(needed, []))
            findings.append({
                "code": "feature-closure", "subject": bundle_id_value,
                "message": (
                    f"{short_name(bundle_id_value)} needs {short_name(needed)} ({via}), but "
                    f"{', '.join(short_name(f) for f in owning)} neither contains it nor requires a feature that does"
                ),
                "hint": (
                    f'Add <import feature="{suppliers[0]}"/> to {owning[0]}.'
                    if suppliers else f"Add {needed} to {owning[0]}."
                ),
            })

    for bundle in bundles.values():
        for clause in clauses(bundle["headers"].get("Import-Package", "")):
            package = bundle_id(clause)
            if package.startswith("com.northwind.") and package not in graph["packageOwners"]:
                findings.append({
                    "code": "unresolved-package", "subject": bundle["id"],
                    "message": f"{short_name(bundle['id'])} imports {package}, which no bundle in this workspace exports",
                    "hint": "Export the package from its bundle or fix the import.",
                })
    return findings


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "changed"), default="full")
    parser.add_argument("--base", help="Git ref used to discover changed files.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_DIR / "build-reports")
    parser.add_argument("--changed-path", action="append", default=[],
                        help="Changed path (repeatable; intended for local use/testing).")
    args = parser.parse_args()

    products, graph, maven_edges, osgi_edges = discover_graph()
    product_dependencies = graph["productDependencies"]
    components, prerequisites, component_edges = build_components(graph, osgi_edges)
    all_waves = layers(prerequisites, "component")
    wave_of = {cid: number for number, wave in enumerate(all_waves, 1) for cid in wave}
    findings = consistency_findings(graph, components, prerequisites, component_edges)

    # ---- decide what to build ------------------------------------------------
    entries: list[dict] = []
    changed_components: set[str] = set()
    reason = "full build requested"
    full_fallback = args.mode == "full"
    if args.mode == "changed":
        paths = args.changed_path or changed_paths(args.base)
        if paths is None:
            full_fallback, reason = True, "could not detect changed files; building everything to be safe"
        else:
            result = classify_changes(paths, products, components)
            entries, changed_components = result["entries"], result["components"]
            if result["full_reason"]:
                full_fallback, reason = True, result["full_reason"]
            elif changed_components:
                reason = "changed components plus everything that depends on them"
            else:
                reason = "no buildable changes (only documentation or ignored files changed)"

    effective = "full" if full_fallback else "changed"
    impacted: set[str] = set()
    component_paths: dict[str, list[list[str]]] = {}
    product_paths: dict[str, list[list[str]]] = {}
    if effective == "full":
        selected = list(products)
    elif changed_components:
        dependents: dict[str, set[str]] = defaultdict(set)
        for consumer, items in prerequisites.items():
            for item in items:
                dependents[item].add(consumer)
        component_paths = impact(changed_components, dependents)
        impacted = {node for chains in component_paths.values() for chain in chains for node in chain} - changed_components
        changed_products = {components[cid]["product"] for cid in changed_components}
        selected, _ = select_products(products, product_dependencies, changed_products)
        product_dependents: dict[str, set[str]] = defaultdict(set)
        for consumer, items in product_dependencies.items():
            for item in items:
                product_dependents[item].add(consumer)
        product_paths = impact(changed_products, product_dependents)
    else:
        selected = []

    status: dict[str, str] = {}
    for cid, comp in components.items():
        if cid in changed_components:
            status[cid] = "changed"
        elif effective == "full":
            status[cid] = "built"
        elif cid in impacted:
            status[cid] = "impacted"
        elif comp["product"] in selected:
            status[cid] = "cobuilt"
        else:
            status[cid] = "skipped"
        comp["status"], comp["wave"] = status[cid], wave_of[cid]

    built = {cid for cid, value in status.items() if value != "skipped"}
    waves = [[cid for cid in wave if cid in built] for wave in all_waves]
    waves = [wave for wave in waves if wave]
    selected_set = set(selected)
    product_levels = layers(
        {product: product_dependencies[product] & selected_set for product in selected}, "product"
    ) if selected else []

    maven_projects: list[str] = []
    if effective == "changed":
        for product in selected:
            maven_projects.append(product)
            maven_projects.extend(graph["productProjects"].get(product, []))
        maven_projects = list(dict.fromkeys(maven_projects))

    # ---- console log (README part 1) ---------------------------------------------
    names = lambda ids: ", ".join(components[i]["name"] for i in ids)  # noqa: E731
    print(f"Requested scenario : {args.mode}")
    print(f"Effective scenario : {effective} ({reason})")
    if args.mode == "changed":
        print(f"Base revision      : {args.base or '(none)'}")
        print(f"Changed files ({len(entries)}):")
        for entry in entries:
            target = names(entry["targets"]) if entry["targets"] else entry["effect"]
            print(f"  {entry['path']}  ->  {target}")
        print(f"Directly changed components: {names(sorted(changed_components)) or '(none)'}")
        for start, chains in component_paths.items():
            for chain in chains:
                print(f"  impact: {' -> '.join(components[i]['name'] for i in chain)}")
    print("Product build order: " + (" -> ".join("[" + ", ".join(level) + "]" for level in product_levels) or "(nothing to build)"))
    for number, wave in enumerate(waves, 1):
        print(f"  Wave {number}: {names(wave)}")
    print(f"Consistency findings: {len(findings)}")
    for finding in findings:
        print(f"  warning: {finding['message']}")
    if maven_projects:
        print(f"Selected Maven reactor projects: {', '.join(maven_projects)}")

    # ---- reports -----------------------------------------------------------------
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "requestedMode": args.mode,
        "effectiveMode": effective,
        "reason": reason,
        "baseRef": args.base,
        "products": products,
        "changedFiles": entries,
        "changedComponents": sorted(changed_components),
        "impactedComponents": sorted(impacted),
        "selectedProducts": selected,
        "productLevels": product_levels,
        "waves": waves,
        "components": components,
        "componentEdges": component_edges,
        "componentPaths": component_paths,
        "dependencyPaths": product_paths,
        "findings": findings,
        "productDependencies": {key: sorted(value) for key, value in product_dependencies.items()},
        "maven": {"projects": graph["projects"], "edges": maven_edges},
        "osgi": {
            "bundles": {k: {"product": v["product"], "path": v["path"], "exports": v["exports"]}
                        for k, v in graph["bundles"].items()},
            "features": {k: {"product": v["product"], "path": v["path"]} for k, v in graph["features"].items()},
            "edges": osgi_edges,
        },
    }
    (args.output_dir / "dependency-graph.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    outputs = {
        "build_mode": effective,
        "build_required": "true" if selected else "false",
        "changed_products": ",".join(sorted({components[c]["product"] for c in changed_components})),
        "selected_products": ",".join(selected),
        "maven_projects": ",".join(maven_projects),
    }
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")
    print(f"Dependency data written to {args.output_dir / 'dependency-graph.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Build planning failed: {error}", file=sys.stderr)
        raise SystemExit(1)
