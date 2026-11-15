"""Assemble migration, SQL, and per-release source context for scenario planning."""

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import PythonEnvironment
from skewguard.static import code_refs as code_refs
from skewguard.static import migrations as migrations
from skewguard.static import sql as sql
from skewguard.static.types import StaticContext


def collect_static_context(
    config: RunConfig,
    base: PythonEnvironment,
    head: PythonEnvironment,
    server: DatabaseConnection,
    *,
    settings_module: str,
) -> StaticContext:
    """Inspect both releases and preserve metadata, SQL coverage, and code gaps.

    Analyze migrations once and pass that analysis to SQL collection and both
    source scans. Scan base.checkout and head.checkout separately. Preserve
    unavailable SQL records and all nested issues; they are planning gaps.
    Propagate fatal inspection errors rather than fabricating an empty analysis.
    The caller owns worktrees, environments, and the PostgreSQL server lifetime.
    This function does not execute probes, compare outcomes, or assign verdicts.
    """
    # connector for all helpers, ast and SQL -> into a context file

    analysis = migrations.analyze_migrations(
        config, base, head, settings_module=settings_module
    )

    scripts = sql.collect_sql(
        config,
        base,
        head,
        server,
        analysis,
        settings_module=settings_module
    )
    base_code = code_refs.scan_code_references(
        config, base.checkout, analysis,
    )

    head_code = code_refs.scan_code_references(
        config,
        head.checkout,
        analysis,
    )

    return StaticContext(
        migrations=analysis,
        sql=scripts,
        base_code=base_code,
        head_code=head_code,
    )

