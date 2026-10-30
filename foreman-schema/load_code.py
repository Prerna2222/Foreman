"""Load a repo's code map into the graph: files, functions, who-calls-who, tests, and which tests run what.

    python load_code.py --repo ~/Desktop/Foreman/tinydb --trace trace.json [--repo-name tinydb]

--trace is the file made by trace_tests.py (a ready-made one is included).
The graph is named foreman_<repo-name>. It is deleted and rebuilt each time, because it is derived data.
"""

import argparse
import ast
import json
import tokenize
from pathlib import Path

import schema
from schema import q
from project_config import load_project_config


ap = argparse.ArgumentParser()

target = ap.add_mutually_exclusive_group(required=True)
target.add_argument(
    "--repo", help="Path to a TinyDB repository (legacy mode)"
)
target.add_argument(
    "--config", help="Path to foreman.yaml"
)

ap.add_argument("--trace", default="trace.json")
ap.add_argument("--repo-name", default=None)
ap.add_argument(
    "--list-files",
    action="store_true",
    help="List files without writing to FalkorDB",
)

args = ap.parse_args()

# Select either YAML configuration or original TinyDB behavior.
if args.config:
    config = load_project_config(args.config)
    repo = config.repo_path
    source_dirs = config.source_dirs
    repo_name = args.repo_name or config.name
else:
    repo = Path(args.repo).expanduser().resolve()
    source_dirs = (repo / "tinydb",)

    if not source_dirs[0].is_dir():
        ap.error(f"No tinydb/ directory inside {repo}")

    repo_name = args.repo_name or "tinydb"


def module_name(path: Path, source_root: Path) -> str:
    """Convert a Python source path into an importable module name."""
    import_root = (
        source_root.parent
        if (source_root / "__init__.py").is_file()
        else source_root
    )

    parts = list(path.relative_to(import_root).with_suffix("").parts)

    if parts and parts[-1] == "__init__":
        parts.pop()

    return ".".join(parts)


# ---- 1. Discover source files, functions, and internal imports ----

files, funcs, imports = [], {}, set()
seen_files = set()

for source_root in source_dirs:
    for path in sorted(source_root.rglob("*.py")):
        if not path.is_file():
            continue

        if not path.resolve().is_relative_to(repo):
            raise ValueError(f"Source file escapes repository: {path}")

        rel = path.relative_to(repo).as_posix()

        if rel in seen_files:
            continue

        seen_files.add(rel)

        mod = module_name(path, source_root)

        files.append({
            "path": rel,
            "module": mod,
        })

        with tokenize.open(path) as handle:
            tree = ast.parse(handle.read(), filename=str(path))

        def visit(body, prefix):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fid = f"{mod}.{prefix}{node.name}"

                    funcs[fid] = {
                        "id": fid,
                        "name": node.name,
                        "file": rel,
                        "start": node.lineno,
                        "end": node.end_lineno,
                    }

                elif isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")

        visit(tree.body, "")

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = mod.split(".")

                    if path.name != "__init__.py":
                        base = base[:-1]

                    base = base[:len(base) - (node.level - 1)]

                    target_mod = ".".join(
                        base + ([node.module] if node.module else [])
                    )

                    imports.add((rel, target_mod))

                elif node.module:
                    imports.add((rel, node.module))

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add((rel, alias.name))


mod_to_file = {f["module"]: f["path"] for f in files}

imports = sorted({
    (a, mod_to_file[m])
    for a, m in imports
    if m in mod_to_file and mod_to_file[m] != a
})


# Inspect discovery without needing FalkorDB or a test trace.
if args.list_files:
    print(f"Project: {repo_name}")

    for item in files:
        print(f"  {item['module']} -> {item['path']}")

    print(
        f"Found {len(files)} files, "
        f"{len(funcs)} functions, "
        f"and {len(imports)} internal imports"
    )

    raise SystemExit(0)


# ---- 2. read the test trace ---------------------------------------------------------------------
trace = json.load(open(args.trace))
unknown = {f for t in trace.values() for f in t["functions"] if f not in funcs}
if unknown:
    print(f"NOTE: {len(unknown)} traced functions are not in the source (is the repo the same version?): "
          f"{sorted(unknown)[:3]}")
calls = {}
covers = []
for tid, t in trace.items():
    for f, n in t["functions"].items():
        if f in funcs:
            covers.append({"t": tid, "f": f, "n": n})
    for a, b, n in t["edges"]:
        if a in funcs and b in funcs:
            calls[(a, b)] = calls.get((a, b), 0) + n

# ---- 3. build the graph -------------------------------------------------------------------------
db = schema.connect()
name = schema.graph_name(repo_name)
try:
    db.select_graph(name).delete()
except Exception:  # noqa: BLE001
    pass
g = db.select_graph(name)
schema.create_schema(g)


def batch(query, rows, size=400):
    for i in range(0, len(rows), size):
        q(g, query, {"rows": rows[i:i + size]})


batch("UNWIND $rows AS r CREATE (:File {path:r.path, module:r.module})", files)
batch("""UNWIND $rows AS r
         CREATE (:Function {id:r.id, name:r.name, file:r.file, start_line:r.start, end_line:r.end, test_count:0})""",
      list(funcs.values()))
batch("UNWIND $rows AS r MATCH (f:Function {id:r.id}), (x:File {path:r.file}) CREATE (f)-[:DEFINED_IN]->(x)",
      list(funcs.values()))
batch("UNWIND $rows AS r MATCH (a:File {path:r.a}), (b:File {path:r.b}) CREATE (a)-[:IMPORTS]->(b)",
      [{"a": a, "b": b} for a, b in imports])
batch("UNWIND $rows AS r MATCH (a:Function {id:r.a}), (b:Function {id:r.b}) CREATE (a)-[:CALLS {n:r.n}]->(b)",
      [{"a": a, "b": b, "n": n} for (a, b), n in calls.items()])
batch("UNWIND $rows AS r CREATE (:Test {id:r.id, status:r.status, file:r.file})",
      [{"id": tid, "status": t["status"], "file": tid.split("::")[0]} for tid, t in trace.items()])
batch("UNWIND $rows AS r MATCH (x:Test {id:r.t}), (f:Function {id:r.f}) CREATE (x)-[:COVERS {n:r.n}]->(f)", covers)
q(g, schema.SET_TEST_COUNTS)

for label in ("File", "Function", "Test"):
    print(f"{label:9s}", q(g, f"MATCH (n:{label}) RETURN count(n)")[0][0])
for rel in ("DEFINED_IN", "IMPORTS", "CALLS", "COVERS"):
    print(f"{rel:10s}", q(g, f"MATCH ()-[r:{rel}]->() RETURN count(r)")[0][0])
print(f"OK: code loaded into graph '{name}'")
