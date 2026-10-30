"""Run a repo's test suite once and record, for every test: pass/fail, which functions it ran,
and which function called which. Output is a JSON file that load_code.py puts into the graph.

    python trace_tests.py --repo ~/Desktop/Foreman/tinydb --out trace.json

Run it from any folder; it starts pytest inside the repo. Needs pytest and pyyaml installed.
"""
import argparse
import json
import os
import subprocess
import sys

PLUGIN = '''
import json, os, sys, collections
import pytest


SOURCES = []

for folder in json.loads(os.environ["TRACE_SOURCE_DIRS"]):
    root = os.path.realpath(folder)

    import_root = (
        os.path.dirname(root)
        if os.path.isfile(os.path.join(root, "__init__.py"))
        else root
    )

    SOURCES.append((root, import_root))

OUT = os.environ["TRACE_OUT"]
tests = {}
cache = {}



def fid(code):
    """Return a function ID matching the code indexer's module IDs."""
    if code in cache:
        return cache[code]

    fn = os.path.realpath(code.co_filename)

    for source_root, import_root in SOURCES:
        if fn.startswith(source_root + os.sep) and fn.endswith(".py"):
            break
    else:
        cache[code] = None
        return None

    mod = os.path.relpath(fn, import_root)[:-3].replace(os.sep, ".")

    if mod.endswith(".__init__"):
        mod = mod[:-len(".__init__")]

    parts = []

    for part in code.co_qualname.split("."):
        if part.startswith("<"):
            break
        parts.append(part)

    cache[code] = (
        mod + "." + ".".join(parts)
        if parts else None
    )

    return cache[code]


def from_pytest(frame):
    """True if this tinydb call was started by pytest itself (for example, printing a failed
    assertion calls __repr__). Those calls only happen in failing tests, so they would look
    like clues about the bug. We skip them."""
    f = frame.f_back
    while f is not None and fid(f.f_code) is not None:
        f = f.f_back
    if f is None:
        return False
    name = f.f_code.co_filename
    return "_pytest" in name or "pluggy" in name


def make(funcs, edges):
    def prof(frame, event, arg):
        if event != "call":
            return
        callee = fid(frame.f_code)
        if callee is None or from_pytest(frame):
            return
        funcs[callee] += 1
        back = frame.f_back
        if back is not None:
            caller = fid(back.f_code)
            if caller is not None and caller != callee:
                edges[(caller, callee)] += 1
    return prof


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    funcs, edges = collections.Counter(), collections.Counter()
    sys.setprofile(make(funcs, edges))
    try:
        return (yield)
    finally:
        sys.setprofile(None)
        t = tests.setdefault(item.nodeid, {"status": "pass"})
        t["functions"] = dict(funcs)
        t["edges"] = [[a, b, n] for (a, b), n in edges.items()]


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        t = tests.setdefault(report.nodeid, {"status": "pass"})
        t["status"] = {"passed": "pass", "failed": "fail", "skipped": "skip"}[report.outcome]
        t.setdefault("functions", {})
        t.setdefault("edges", [])


def pytest_sessionfinish(session):
    json.dump(tests, open(OUT, "w"))
'''



def main():
    from pathlib import Path
    from tempfile import TemporaryDirectory

    ap = argparse.ArgumentParser()

    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--repo", help="Legacy TinyDB repository")
    target.add_argument("--config", help="Path to foreman.yaml")

    ap.add_argument("--out", default="trace.json")
    args = ap.parse_args()

    if args.config:
        # Share the existing configuration loader.
        schema_dir = Path(__file__).resolve().parents[1] / "foreman-schema"
        sys.path.insert(0, str(schema_dir))

        from project_config import load_project_config

        config = load_project_config(args.config)
        repo = config.repo_path
        source_dirs = config.source_dirs

        test_dirs = [
            str(path.relative_to(repo))
            for path in config.test_dirs
        ]

        command = [
            sys.executable if part == "{python}" else part
            for part in config.test_command
        ]

        # The tracing plugin currently supports pytest.
        if command[:3] != [sys.executable, "-m", "pytest"]:
            ap.error(
                "Tracing requires a test command starting with "
                "['{python}', '-m', 'pytest']"
            )

        timeout = config.timeout_seconds

    else:
        # Preserve the original TinyDB CLI.
        repo = Path(args.repo).expanduser().resolve()
        source_dir = repo / "tinydb"

        if not source_dir.is_dir():
            ap.error(f"TinyDB directory not found: {source_dir}")

        source_dirs = (source_dir,)
        test_dirs = ["tests"]
        command = [sys.executable, "-m", "pytest"]
        timeout = None

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # Prevent an old trace from being mistaken for a new result.
    out.unlink(missing_ok=True)

    with TemporaryDirectory(prefix="foreman_trace_") as tempdir:
        plugin_dir = Path(tempdir)

        (plugin_dir / "foreman_trace.py").write_text(
            PLUGIN, encoding="utf-8"
        )

        import_paths = [plugin_dir, repo]

        for source in source_dirs:
            import_root = (
                source.parent
                if (source / "__init__.py").is_file()
                else source
            )

            if import_root not in import_paths:
                import_paths.append(import_root)

        python_path = os.pathsep.join([
            *(str(path) for path in import_paths),
            *(
                [os.environ["PYTHONPATH"]]
                if os.environ.get("PYTHONPATH")
                else []
            ),
        ])

        env = dict(
            os.environ,
            TRACE_OUT=str(out),
            TRACE_SOURCE_DIRS=json.dumps(
                [str(path) for path in source_dirs]
            ),
            PYTHONHASHSEED="0",
            PYTHONPATH=python_path,
        )

        cmd = [
            *command,
            "-p", "foreman_trace",
            "-p", "no:cacheprovider",
            "-o", "addopts=",
            "-q", "--tb=no",
            *test_dirs,
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=repo,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            ap.error(
                f"Test tracing timed out after {timeout} seconds"
            )

    # Exit code 1 is allowed: Foreman's demo has failing tests.
    if result.returncode not in (0, 1):
        ap.error(
            f"Pytest did not complete (exit code {result.returncode})"
        )

    if not out.is_file():
        ap.error(f"No trace file was produced: {out}")

    with out.open(encoding="utf-8") as file:
        data = json.load(file)

    n_fail = sum(
        test["status"] == "fail"
        for test in data.values()
    )

    print(
        f"traced {len(data)} tests "
        f"({n_fail} failing) -> {out}"
    )


if __name__ == "__main__":
    main()
