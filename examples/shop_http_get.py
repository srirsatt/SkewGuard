"""Run GET /products/ on the base release of a generated shop demo repository."""

import argparse
from pathlib import Path

from skewguard.config import RunConfig
from skewguard.exec.db import clone_database
from skewguard.exec.probes import run_http_get, seed_database
from skewguard.exec.runner import run_migrations
from skewguard.exec.scenarios import Scenario
from skewguard.graph.session import prepared_resources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repo", type=Path, help="Shop repository from make_demo_repo.py"
    )
    args = parser.parse_args()
    config = RunConfig(repo_path=args.repo, base_ref="base", head_ref="breaking")
    request = Scenario(
        scenario_id="http:catalog.products:list",
        method="GET",
        path="/products/",
        kind="read",
    )
    with prepared_resources(config) as resources:
        db = clone_database(config, resources.server)
        run_migrations(config, resources.base, db, settings_module="shop.settings")
        seed_database(
            config,
            resources.base,
            db,
            settings_module="shop.settings",
            fixture="products",
        )
        outcome = run_http_get(
            config,
            resources.base,
            db,
            settings_module="shop.settings",
            scenario=request,
        )
    print(outcome.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
