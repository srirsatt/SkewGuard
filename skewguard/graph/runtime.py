"""Run-scoped resource locations supplied separately from checkpointed state."""

from dataclasses import dataclass, field

from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import (
    PythonEnvironment,
    backend_directory,
    validate_environment,
)
from skewguard.exec.matrix import prepare_old_snapshot
from skewguard.exec.worktree import CheckoutPair
from skewguard.graph.state import RunState


@dataclass
class RunResources:
    """Borrow prepared resources; their owning context managers handle cleanup.

    Server contains connection settings, not an open PostgreSQL connection.
    Temporary checkout/environment paths and database credentials are run-scoped
    and must be supplied again when execution resumes in a new process.
    """

    checkouts: CheckoutPair
    base: PythonEnvironment
    head: PythonEnvironment
    server: DatabaseConnection
    _scenario_snapshot: DatabaseConnection | None = field(default=None, init=False)

    def scenario_snapshot(self, state: RunState) -> DatabaseConnection:
        """Keep one seeded template for all proposal attempts in this session."""
        if self._scenario_snapshot is None:
            self._scenario_snapshot = prepare_old_snapshot(
                state.config, self.base, self.server,
                settings_module=state.settings_module, seed_fixture=state.seed_fixture,
            )
        return self._scenario_snapshot


def validate_resources(state: RunState, resources: RunResources | None) -> RunResources:
    """Require matching resource bindings before work and return the context.

    Commit IDs come from managed_worktrees. This validates their association
    with state, not checkout contents or the continued health of PostgreSQL.
    """
    if resources is None:
        raise ValueError("RunResources must be supplied as graph runtime context")
    if (
        resources.checkouts.base_sha != state.base_sha
        or resources.checkouts.head_sha != state.head_sha
    ):
        raise ValueError("runtime checkout commits must match graph state")

    for label, environment, checkout in (
        ("base", resources.base, resources.checkouts.base),
        ("head", resources.head, resources.checkouts.head),
    ):
        if environment.checkout.resolve() != checkout.resolve():
            raise ValueError(f"{label} environment must belong to its runtime checkout")
        validate_environment(environment)
        backend_directory(state.config, environment.checkout)
    return resources
