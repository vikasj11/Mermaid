import subprocess
import tempfile
import unittest
from pathlib import Path

import render_summary
from build_pipeline import (
    build_components,
    changed_paths,
    classify_changes,
    clauses,
    consistency_findings,
    discover_graph,
    impact,
    layers,
    select_products,
)


class PlannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.products, cls.graph, cls.maven_edges, cls.osgi_edges = discover_graph()
        cls.components, cls.prerequisites, cls.edges = build_components(cls.graph, cls.osgi_edges)

    def cid(self, name):
        return next(c for c, comp in self.components.items() if comp["name"] == name)

    # -- product level ---------------------------------------------------------
    def test_changed_product_includes_dependents_and_prerequisites(self):
        selected, impacted = select_products(
            self.products, self.graph["productDependencies"], {"customer"}
        )
        self.assertEqual(selected, ["thirdparty", "catalog", "customer", "shipping", "notification"])
        self.assertEqual(impacted, {"customer", "shipping", "notification"})

    def test_manifest_package_dependency_is_in_osgi_graph(self):
        self.assertTrue(any(
            e["from"] == "com.northwind.oms.gateway" and e["to"] == "com.northwind.oms.core"
            and e["kind"] == "imports-package" for e in self.osgi_edges
        ))

    def test_feature_dependency_is_in_product_graph(self):
        self.assertIn("catalog", self.graph["productDependencies"]["orders"])

    def test_version_ranges_do_not_split_manifest_clauses(self):
        self.assertEqual(
            clauses('a.b;version="[1.0.0,2.0.0)",c.d;version="1.0"'),
            ['a.b;version="[1.0.0,2.0.0)"', 'c.d;version="1.0"'],
        )

    # -- change detection ----------------------------------------------------------
    def test_changed_file_maps_to_its_own_bundle_only(self):
        result = classify_changes(
            ["catalog/plugins/com.northwind.oms.core/src/Foo.java"], self.products, self.components
        )
        self.assertEqual(result["components"], {"com.northwind.oms.core"})
        self.assertIsNone(result["full_reason"])

    def test_product_level_file_marks_every_component_of_that_product(self):
        result = classify_changes(["catalog/pom.xml"], self.products, self.components)
        self.assertEqual(
            result["components"],
            {"com.northwind.oms.core", "com.northwind.oms.inventory", "com.northwind.oms.catalog.feature"},
        )

    def test_docs_are_ignored_and_do_not_trigger_a_build(self):
        result = classify_changes(["README.md", ".DS_Store"], self.products, self.components)
        self.assertEqual(result["components"], set())
        self.assertIsNone(result["full_reason"])

    def test_root_pom_and_pipeline_changes_force_a_full_build(self):
        for path in ("pom.xml", "scripts/build_pipeline.py", ".github/workflows/osgi-build.yml"):
            with self.subTest(path=path):
                self.assertIsNotNone(classify_changes([path], self.products, self.components)["full_reason"])

    # -- waves and impact ------------------------------------------------------------
    def test_every_component_builds_after_all_of_its_prerequisites(self):
        wave_of = {c: n for n, wave in enumerate(layers(self.prerequisites, "component"), 1) for c in wave}
        for component, needs in self.prerequisites.items():
            for need in needs:
                self.assertLess(wave_of[need], wave_of[component], f"{component} before {need}")

    def test_roots_are_in_wave_one(self):
        wave_one = layers(self.prerequisites, "component")[0]
        self.assertEqual(
            {self.components[c]["name"] for c in wave_one}, {"core", "customer", "security", "org.slf4j"}
        )

    def test_cycles_are_reported(self):
        with self.assertRaises(RuntimeError):
            layers({"a": {"b"}, "b": {"a"}}, "component")

    def test_impact_follows_dependents_with_shortest_chain(self):
        dependents = {}
        for consumer, needs in self.prerequisites.items():
            for need in needs:
                dependents.setdefault(need, set()).add(consumer)
        chains = impact({self.cid("customer")}, dependents)[self.cid("customer")]
        named = [" -> ".join(self.components[c]["name"] for c in chain) for chain in chains]
        self.assertIn("customer -> shipping", named)
        self.assertIn("customer -> customer.feature -> shipping.feature", named)

    # -- consistency -------------------------------------------------------------------
    def test_missing_feature_import_is_reported(self):
        findings = consistency_findings(self.graph, self.components, self.prerequisites, self.edges)
        flagged = {f["subject"] for f in findings if f["code"] == "feature-closure"}
        self.assertIn("com.northwind.oms.notification", flagged)


class ChangedPathsTest(unittest.TestCase):
    """Exercises the real git code path, with the project in a sub-folder like the original layout."""

    def git(self, cwd, *args):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd, check=True, capture_output=True,
        )

    def test_paths_are_relative_to_project_and_outside_files_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            project = repo / "osgi-workshop"
            (project / "catalog").mkdir(parents=True)
            (project / "catalog" / "A.java").write_text("1")
            (repo / "other.txt").write_text("1")
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-qm", "one")
            (project / "catalog" / "A.java").write_text("2")
            (repo / "other.txt").write_text("2")
            self.git(repo, "commit", "-qam", "two")

            self.assertEqual(changed_paths("HEAD~1", project), ["catalog/A.java"])
            self.assertEqual(changed_paths("HEAD", project), [])  # known and empty
            self.assertIsNone(changed_paths("does-not-exist", project))  # unknown
            self.assertIsNone(changed_paths("0" * 40, project))  # new-branch push


class RendererTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        products, graph, _, edges = discover_graph()
        components, prerequisites, component_edges = build_components(graph, edges)
        for number, wave in enumerate(layers(prerequisites, "component"), 1):
            for cid in wave:
                components[cid]["wave"] = number
                components[cid]["status"] = "changed" if cid.endswith(".core") else "impacted"
        cls.data = {
            "requestedMode": "changed", "effectiveMode": "changed", "reason": "test",
            "products": products, "changedFiles": [], "changedComponents": ["com.northwind.oms.core"],
            "impactedComponents": [], "productLevels": [["catalog"], ["orders"]],
            "waves": layers(prerequisites, "component"), "components": components,
            "componentEdges": component_edges, "componentPaths": {"com.northwind.oms.core": []},
            "findings": consistency_findings(graph, components, prerequisites, component_edges),
        }

    def test_graph_has_a_box_per_product_and_a_node_per_component(self):
        text = render_summary.mermaid_graph(self.data, highlight=True)
        self.assertTrue(text.startswith("flowchart TB"))
        self.assertEqual(text.count("subgraph "), len(self.data["products"]))
        for component in self.data["components"].values():
            self.assertIn(render_summary.node_id(component["id"]), text)

    def test_prerequisite_points_at_dependent(self):
        text = render_summary.mermaid_graph(self.data, highlight=False)
        core, gateway = (render_summary.node_id(f"com.northwind.oms.{n}") for n in ("core", "gateway"))
        self.assertIn(f"{core} --> {gateway}", text)

    def test_summary_lists_waves_graph_and_findings(self):
        text = render_summary.summary_markdown(self.data, "https://example.invalid/artifact")
        for expected in ("| **1** |", "```mermaid", "Product build order", "consistency findings (4)",
                         "&lt;import feature=", "https://example.invalid/artifact"):
            self.assertIn(expected, text)

    def test_html_report_is_escaped_and_contains_graph(self):
        page = render_summary.html_report(self.data)
        self.assertIn("class='mermaid'", page)
        self.assertNotIn("%%", page)
        self.assertNotIn('<import feature="', page)


if __name__ == "__main__":
    unittest.main()
