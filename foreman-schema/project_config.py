
"""Load and validate a Foreman project configuration."""

import argparse
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    repo_path: Path
    source_dirs: tuple[Path, ...]
    test_dirs: tuple[Path, ...]
    test_command: tuple[str, ...]
    timeout_seconds: int


def load_project_config(config_file: str | Path) -> ProjectConfig:
    config_path = Path(config_file).expanduser().resolve()

    with config_path.open(encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if not isinstance(data, dict):
        raise ValueError("Config must be a YAML mapping")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("'name' must be a nonempty string")

    repo_value = data.get("repo_path")
    if not isinstance(repo_value, str) or not repo_value.strip():
        raise ValueError("'repo_path' must be a nonempty string")

    repo = (config_path.parent / repo_value).resolve()

    if not repo.is_dir():
        raise ValueError(f"Repository directory not found: {repo}")

    def resolve_dirs(field: str) -> tuple[Path, ...]:
        values = data.get(field)

        if not isinstance(values, list) or not values:
            raise ValueError(f"'{field}' must be a nonempty list")

        directories = []

        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid entry in '{field}'")

            if Path(value).is_absolute():
                raise ValueError(f"Absolute paths not allowed in '{field}'")

            directory = (repo / value).resolve()

            if not directory.is_relative_to(repo):
                raise ValueError(f"Path escapes repository: {value}")

            if not directory.is_dir():
                raise ValueError(f"Directory not found: {directory}")

            directories.append(directory)

        return tuple(directories)

    source_dirs = resolve_dirs("source_dirs")
    test_dirs = resolve_dirs("test_dirs")

    command = data.get("test_command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(x, str) or not x.strip() for x in command)
    ):
        raise ValueError("'test_command' must be a nonempty string list")

    timeout = data.get("timeout_seconds")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("'timeout_seconds' must be a positive integer")

    return ProjectConfig(
        name=name,
        repo_path=repo,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        test_command=tuple(command),
        timeout_seconds=timeout,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="foreman.yaml")
    args = parser.parse_args()

    config = load_project_config(args.config)

    print(f"Project: {config.name}")
    print(f"Repository: {config.repo_path}")
    print(f"Source directories: {config.source_dirs}")
    print(f"Test directories: {config.test_dirs}")
    print(f"Test command: {config.test_command}")
    print(f"Timeout: {config.timeout_seconds}s")
    print("Configuration loaded successfully!")
