"""Run-scoped resource locations supplied separately from checkpointed state."""

from dataclasses import dataclass

from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import (
    PythonEnvironment,
    backend_directory,
    validate_environment,
)
from skewguard.exec.worktree import CheckoutPair
from skewguard.graph.state import RunState


@dataclass(frozen=True)
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
