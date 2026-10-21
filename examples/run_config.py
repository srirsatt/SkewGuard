"""Create and print a configuration: uv run python examples/run_config.py."""

from pathlib import Path

from skewguard.config import RunConfig


def main() -> None:
    config = RunConfig(
        repo_path=Path.cwd(),
        base_ref="main",
        head_ref="HEAD",
    )
    print(config.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
