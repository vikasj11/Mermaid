# OSGi Workshop - Build Pipeline Assignment

## Assignment

Your task is to create a **smart build pipeline** for the Northwind Order Management System that can build the product in two different scenarios:

### Scenario 1: Build All Products
Build all modules in the correct dependency order.

### Scenario 2: Build Only Changed Products/Modules
Detect which modules have been modified, identify all dependent modules that need to be rebuilt, and build only those modules in the correct dependency order with visibility into the build order.

---



**Note:** These modules are interdependent. Changes in one module may require rebuilding other modules that depend on it.

---

## How to Test Your Solution

1. **Modify files from 3+ different modules** randomly across different functional domains
2. **Run your build pipeline** with the "changed modules only" scenario
3. **Check the build logs** to verify:
   - Which modules were detected as changed
   - The complete dependency chain resolution
   - The order in which modules were built
   - Confirmation that only necessary modules were rebuilt

### Example Test Case

Modify source files in:
- `catalog/plugins/com.northwind.oms.core/src/...`
- `orders/plugins/com.northwind.oms.gateway/src/...`  
- `payment/plugins/com.northwind.oms.payment/src/...`

Your pipeline should detect these changes, identify all dependent modules, and build them in the correct order with clear logging.

---

## Deliverable

### Part 1:
A working build pipeline (Maven configuration, build script, or custom tool) that supports both build scenarios with clear, verbose logging showing:
- Detected changes
- Module dependency chain
- Build order and sequence
### Part 2:
The pipeline must also display the product dependency graph. The graph must show:
- The dependency graph for both the full-build and changed-modules scenarios
- All product modules and their dependencies
- The direction of each dependency
- The dependency path for every changed module
- The modules selected for rebuilding in the changed-modules scenario


---

## Build pipeline and run-summary dashboard

`.github/workflows/osgi-build.yml` runs three jobs: **Test resolver** → **Plan build** → **Build**.
The **Plan build** job publishes the dependency dashboard to the workflow run's **Summary** page:

* build **waves** (bundles 📦 and features 🧩 that can build in parallel), with 🔴 directly changed / 🟠 impacted markers
* the **dependency graph** (one box per product; arrows point from a dependency to what needs it)
* the **product build order**, the **dependency path of every changed component**, and **product consistency findings**
* a link to the full HTML report, attached to the run as the `northwind-oms-dependency-graphs` artifact

| Scenario | How it is chosen |
|---|---|
| Build all | *Run workflow* → `build_mode = full` |
| Build changed only | push / pull request (automatic), or *Run workflow* → `build_mode = changed` with `base_ref` (e.g. `master` or `HEAD~3`) |

Changed mode maps each changed file to its bundle/feature, adds everything that depends on it, and builds only the
affected products in dependency order. Docs-only changes build nothing; changes to the root `pom.xml`, `scripts/` or
`.github/` (or an unknown base revision) fall back to a full build, and the summary says why.

Try it locally:

```bash
python3 scripts/build_pipeline.py --mode changed --base HEAD~1      # or: --changed-path <file> (repeatable)
python3 scripts/render_summary.py --summary - --html build-reports/dependency-report.html
python3 -m unittest discover -s scripts -p 'test_*.py'
```
