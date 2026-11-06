"""Render concise Markdown from validated execution evidence."""

import html
import json

from skewguard.exec.types import Outcome
from skewguard.report.json_report import (
    CheckStatus,
    RunReport,
    issue_observations,
    replay_only_candidates,
)

STATUS_LABELS: dict[CheckStatus, str] = {
    "break_detected": "Reproducible break detected",
    "no_break_detected": "No break detected in executed checks",
    "inconclusive": "Inconclusive",
}


def escape_markdown(value: str) -> str:
    """Keep evidence text from introducing Markdown links, HTML, or table rows."""
    escaped = html.escape(value, quote=False).replace("\\", "\\\\")
    for character in "`*_[]!|":
        escaped = escaped.replace(character, "\\" + character)
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def outcome_detail(outcome: Outcome) -> str:
    """Show bounded evidence; full outcomes remain in the JSON report."""
    if outcome.status == "exception":
        detail = f"{outcome.exception_type}: {outcome.exception_message}"
    elif outcome.status == "http_status":
        detail = f"HTTP {outcome.http_status}"
    else:
        detail = json.dumps(outcome.body, ensure_ascii=False, sort_keys=True)
    return escape_markdown(detail[:500] + ("…" if len(detail) > 500 else ""))


def render_markdown(report: RunReport) -> str:
    """Present confirmed evidence and uncertainty without declaring universal safety."""
    context = report.context
    summary = report.summary
    verification = report.verification
    lines = [
        "## SkewGuard",
        "",
        f"**{STATUS_LABELS[summary.status]}**",
        "",
        "| Scope | Result |",
        "| --- | --- |",
        f"| Deploy window (A0D1) | {STATUS_LABELS[summary.deploy_window]} |",
        f"| Rollback after new writes (A0D1w) | {STATUS_LABELS[summary.rollback]} |",
        "",
        f"Base: {escape_markdown(context.base_ref)} ({context.base_sha})  ",
        f"Head: {escape_markdown(context.head_ref)} ({context.head_sha})  ",
        f"Django settings: {escape_markdown(context.settings_module)}",
        "",
        f"Confirmed: {summary.confirmed}; flaky: {summary.flaky}; "
        f"inconclusive: {summary.inconclusive}; unverified: {summary.unverified}.  ",
        f"Issue observations: {summary.issue_observations}; "
        f"failed replays: {summary.failed_replays}.",
        "",
        "### Findings",
        "",
    ]
    if verification.decisions:
        lines.extend(
            [
                "| Cell | Check | Decision | Category | Observed evidence |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for decision in verification.decisions:
            candidate = decision.candidate
            lines.append(
                f"| {candidate.cell.value} | {escape_markdown(candidate.scenario_id)} "
                f"| {decision.status} | {candidate.category.value} "
                f"| {outcome_detail(candidate.observed)} |"
            )
        if summary.confirmed:
            lines.extend(
                [
                    "",
                    "Confirmed findings matched the initial failure "
                    "in both fresh replays.",
                ]
            )
    else:
        lines.append(
            "No old-code regression candidates were found in the initial checks."
        )

    additional = replay_only_candidates(verification)
    if additional:
        lines.extend(
            [
                "",
                "### First observed during replay",
                "",
                "These failures need their own reproduction cycle "
                "and are not confirmed.",
                "",
                "| Replay | Cell | Check | Category | Observed evidence |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for number, candidate in additional:
            lines.append(
                f"| {number} | {candidate.cell.value} "
                f"| {escape_markdown(candidate.scenario_id)} "
                f"| {candidate.category.value} | {outcome_detail(candidate.observed)} |"
            )

    issues = issue_observations(verification)
    if issues:
        lines.extend(
            [
                "",
                "### Coverage and health issues",
                "",
                "| Execution | Cell | Check | Kind | Detail |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for stage, issue in issues:
            detail = escape_markdown(issue.message)
            if issue.outcome is not None:
                detail += "<br>" + outcome_detail(issue.outcome)
            lines.append(
                f"| {stage} | {issue.cell.value} "
                f"| {escape_markdown(issue.scenario_id or '—')} "
                f"| {issue.kind.value} | {detail} |"
            )

    failed = [attempt for attempt in verification.attempts if attempt.error is not None]
    if failed:
        lines.extend(["", "### Replay execution errors", ""])
        for attempt in failed:
            lines.append(
                f"- Replay {attempt.number}: {escape_markdown(attempt.error or '')}"
            )

    lines.extend(
        [
            "",
            "Coverage: deterministic ORM probes only. "
            "Results apply to executed checks; "
            "they do not prove migration safety. Full outcomes and replay evidence are "
            "retained in the JSON report.",
            "",
        ]
    )
    return "\n".join(lines)
