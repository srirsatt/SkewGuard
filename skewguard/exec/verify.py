"""Reproduce skew candidates from fresh database state before confirming them."""

import logging
from typing import Literal

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection, DatabaseSetupError
from skewguard.exec.diff import body_comparison_key, compare_matrix
from skewguard.exec.envs import EnvironmentSetupError, PythonEnvironment
from skewguard.exec.matrix import run_matrix
from skewguard.exec.probes import ProbeExecutionError
from skewguard.exec.runner import MigrationError, redact_database_password
from skewguard.exec.types import (
    CandidateDivergence,
    Cell,
    ComparisonPolicy,
    ComparisonResult,
    ReproductionAttempt,
    ReproductionDecision,
    VerificationResult,
)

logger = logging.getLogger(__name__)


def same_divergence(
    original: CandidateDivergence, repeated: CandidateDivergence
) -> bool:
    """Match identity and failure evidence; compare bodies only under its policy.

    Exception messages match exactly. Changing messages conservatively prevent
    confirmation rather than masking details that might identify another error.
    """
    if (
        original.cell != repeated.cell
        or original.scenario_id != repeated.scenario_id
        or original.category != repeated.category
        or original.policy != repeated.policy
    ):
        return False
    for before, after in (
        (original.baseline, repeated.baseline),
        (original.observed, repeated.observed),
    ):
        if before.model_dump(exclude={"body"}) != after.model_dump(exclude={"body"}):
            return False
        if original.policy.compare_body and body_comparison_key(
            before, original.policy
        ) != body_comparison_key(after, repeated.policy):
            return False
    return True


def reproduction_state(
    candidate: CandidateDivergence, attempt: ReproductionAttempt
) -> Literal["matched", "changed", "inconclusive"]:
    """Distinguish a missing/different failure from missing execution coverage."""
    comparison = attempt.comparison
    if comparison is None:
        return "inconclusive"
    for issue in comparison.issues:
        if issue.cell in {Cell.A0D0, candidate.cell} and (
            issue.scenario_id is None or issue.scenario_id == candidate.scenario_id
        ):
            return "inconclusive"
    for repeated in comparison.candidates:
        if (
            repeated.cell == candidate.cell
            and repeated.scenario_id == candidate.scenario_id
        ):
            return "matched" if same_divergence(candidate, repeated) else "changed"
    return "changed"


def classify_reproductions(
    initial: ComparisonResult,
    attempts: list[ReproductionAttempt],
) -> list[ReproductionDecision]:
    """Classify each original candidate after exactly two numbered replays.

    Reject incomplete or duplicate attempt numbers. An unavailable replay or
    relevant coverage gap is inconclusive. Otherwise confirm only two matching
    failures; discard disappearing or changed failures as flaky. Preserve every
    original candidate in order with a deterministic reason for its status.
    """
    raise NotImplementedError("reproduction classification is not implemented")


def verify_matrix(
    config: RunConfig,
    base: PythonEnvironment,
    head: PythonEnvironment,
    server: DatabaseConnection,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
    policies: dict[str, ComparisonPolicy] | None = None,
) -> VerificationResult:
    """Run an initial matrix and two fresh matrices if any candidates were found.

    Each run_matrix call rebuilds snapshots from the same releases and fixture.
    The server and Python environments are reused; mutated cell databases are not.
    Initial setup errors propagate. Known replay setup errors become recorded,
    inconclusive attempts; programming errors still propagate to the caller.
    """
    selected_policies = dict(policies or {})
    initial_cells = run_matrix(
        config,
        base,
        head,
        server,
        settings_module=settings_module,
        seed_fixture=seed_fixture,
    )
    initial = compare_matrix(initial_cells, policies=selected_policies)
    attempts: list[ReproductionAttempt] = []
    decisions: list[ReproductionDecision] = []
    if initial.candidates:
        for number in (1, 2):
            try:
                cells = run_matrix(
                    config,
                    base,
                    head,
                    server,
                    settings_module=settings_module,
                    seed_fixture=seed_fixture,
                )
                attempt = ReproductionAttempt(
                    number=number,
                    cells=cells,
                    comparison=compare_matrix(cells, policies=selected_policies),
                )
            except (
                DatabaseSetupError,
                EnvironmentSetupError,
                MigrationError,
                ProbeExecutionError,
            ) as error:
                detail = redact_database_password(
                    f"{type(error).__name__}: {error}", server
                )
                logger.warning("Reproduction attempt %s failed: %s", number, detail)
                attempt = ReproductionAttempt(number=number, error=detail)
            attempts.append(attempt)
        decisions = classify_reproductions(initial, attempts)
        for decision in decisions:
            if decision.status != "confirmed":
                logger.warning(
                    "%s %s %s: %s",
                    decision.candidate.cell.value,
                    decision.candidate.scenario_id,
                    decision.status,
                    decision.reason,
                )
    return VerificationResult(
        initial_cells=initial_cells,
        initial_comparison=initial,
        attempts=attempts,
        decisions=decisions,
    )
