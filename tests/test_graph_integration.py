
"""Opt-in FalkorDB integration test for configurable projects."""

import os
import subprocess
import sys
import uuid
from contextlib import suppress
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_project"

LOADER = ROOT / "foreman-schema" / "load_code.py"
TRACER = ROOT / "foreman-code" / "trace_tests.py"

pytestmark = pytest.mark.skipif(
    os.environ.get("FOREMAN_RUN_GRAPH_TESTS") != "1",
    reason="Requires FalkorDB and FOREMAN_RUN_GRAPH_TESTS=1",
)


def run_script(script, *args):
    result = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"Command failed: {script}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


def test_sample_project_graph(tmp_path, monkeypatch):
    """Index, trace, load, and verify a non-TinyDB project."""

    monkeypatch.syspath_prepend(str(ROOT / "foreman-schema"))
    import schema

    # Each run gets its own graph to protect existing data.
    project_name = "ci_sample_" + uuid.uuid4().hex
    graph_name = schema.graph_name(project_name)

    config_path = tmp_path / "foreman.yaml"
    trace_path = tmp_path / "trace.json"

    config = {
        "name": project_name,
        "repo_path": str(SAMPLE.resolve()),
        "source_dirs": ["miniapp"],
        "test_dirs": ["tests"],
        "test_command": ["{python}", "-m", "pytest"],
        "timeout_seconds": 60,
    }

    config_path.write_text(
        yaml.safe_dump(config),
        encoding="utf-8",
    )

    try:
        # 1. Generate test coverage.
        run_script(
            TRACER,
            "--config", config_path,
            "--out", trace_path,
        )

        # 2. Build the graph.
        run_script(
            LOADER,
            "--config", config_path,
            "--trace", trace_path,
        )

        db = schema.connect()
        graph = db.select_graph(graph_name)

        # 3. Check node counts.
        expected_nodes = {
            "File": 4,
            "Function": 3,
            "Test": 2,
        }

        for label, expected in expected_nodes.items():
            rows = schema.q(
                graph,
                f"MATCH (n:{label}) RETURN count(n)",
            )
            assert rows[0][0] == expected

        # 4. Check relationship counts.
        expected_edges = {
            "DEFINED_IN": 3,
            "IMPORTS": 1,
            "CALLS": 1,
            "COVERS": 3,
        }

        for relationship, expected in expected_edges.items():
            rows = schema.q(
                graph,
                f"MATCH ()-[r:{relationship}]->() RETURN count(r)",
            )
            assert rows[0][0] == expected

        # 5. Verify actual coverage relationships.
        coverage = schema.q(
            graph,
            """
            MATCH (t:Test)-[:COVERS]->(f:Function)
            RETURN t.id, f.id
            """,
        )

        assert {tuple(row) for row in coverage} == {
            (
                "tests/test_service.py::test_total_with_fee",
                "miniapp.service.total_with_fee",
            ),
            (
                "tests/test_service.py::test_total_with_fee",
                "miniapp.nested.calculator.add",
            ),
            (
                "tests/test_service.py::test_multiply",
                "miniapp.nested.calculator.multiply",
            ),
        }

        # 6. Verify the recorded function call.
        calls = schema.q(
            graph,
            """
            MATCH (a:Function)-[:CALLS]->(b:Function)
            RETURN a.id, b.id
            """,
        )

        assert {tuple(row) for row in calls} == {
            (
                "miniapp.service.total_with_fee",
                "miniapp.nested.calculator.add",
            )
        }

    finally:
        # Clean up only the uniquely named test graph.
        with suppress(Exception):
            schema.connect().select_graph(graph_name).delete()
