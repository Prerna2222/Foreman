
"""Offline regression tests for configurable Foreman projects."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOADER = ROOT / "foreman-schema" / "load_code.py"
TRACER = ROOT / "foreman-code" / "trace_tests.py"
CONFIG_LOADER = ROOT / "foreman-schema" / "project_config.py"

SAMPLE_CONFIG = (
    ROOT / "examples" / "sample_project" / "foreman.yaml"
)


def run_script(script, *args):
    """Run a Foreman script using the active Python interpreter."""
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def assert_success(result):
    assert result.returncode == 0, (
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


def test_sample_project_discovery():
    """Nested Python packages should be indexed."""
    result = run_script(
        LOADER, "--config", SAMPLE_CONFIG, "--list-files"
    )

    assert_success(result)

    assert "miniapp.service" in result.stdout
    assert "miniapp.nested.calculator" in result.stdout
    assert "Found 4 files, 3 functions" in result.stdout


def test_tinydb_legacy_compatibility():
    """The YAML and legacy CLI should discover the same files."""
    config_result = run_script(
        LOADER,
        "--config", ROOT / "foreman.yaml",
        "--list-files",
    )

    legacy_result = run_script(
        LOADER,
        "--repo", ROOT / "tinydb",
        "--list-files",
    )

    assert_success(config_result)
    assert_success(legacy_result)

    def discovered_files(output):
        return {
            line.strip()
            for line in output.splitlines()
            if " -> " in line
        }

    assert discovered_files(config_result.stdout)
    assert (
        discovered_files(config_result.stdout)
        == discovered_files(legacy_result.stdout)
    )


def test_sample_project_tracing(tmp_path):
    """Test coverage IDs must match the indexed module names."""
    trace_path = tmp_path / "sample-trace.json"

    result = run_script(
        TRACER,
        "--config", SAMPLE_CONFIG,
        "--out", trace_path,
    )

    assert_success(result)
    assert trace_path.is_file()

    data = json.loads(trace_path.read_text(encoding="utf-8"))

    assert len(data) == 2
    assert all(
        test["status"] == "pass"
        for test in data.values()
    )

    expected = {
        "miniapp.service.total_with_fee",
        "miniapp.nested.calculator.add",
        "miniapp.nested.calculator.multiply",
    }

    traced = {
        function
        for test in data.values()
        for function in test["functions"]
    }

    assert expected <= traced, (
        f"Missing traced functions: {expected - traced}"
    )

    test = data["tests/test_service.py::test_total_with_fee"]

    assert [
        "miniapp.service.total_with_fee",
        "miniapp.nested.calculator.add",
        1,
    ] in test["edges"]


def test_invalid_source_path_rejected(tmp_path):
    """A configured source path must not escape the repository."""
    config_file = tmp_path / "foreman.yaml"

    config_file.write_text(
        "name: invalid\n"
        "repo_path: .\n"
        "source_dirs:\n"
        "  - ../outside\n"
        "test_dirs:\n"
        "  - tests\n"
        "test_command:\n"
        "  - pytest\n"
        "timeout_seconds: 60\n",
        encoding="utf-8",
    )

    result = run_script(
        CONFIG_LOADER, "--config", config_file
    )

    assert result.returncode != 0
    assert "Path escapes repository" in result.stderr
