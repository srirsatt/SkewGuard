"""Validated migration metadata, SQL coverage, and source-reference candidates."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class MigrationKey(BaseModel, frozen=True):
    """Identify a migration within its Django application."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    app_label: str = Field(min_length=1)
    name: str = Field(min_length=1)


class SourceLocation(BaseModel):
    """Source coordinates; file existence is checked when resolving a citation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1, strict=True)


class MigrationOperation(BaseModel):
    """Describe one declared operation without assigning compatibility risk.

    Indices refer to the migration's complete top-level operations list. Kind is
    open-ended so custom operations can be retained with an analysis issue.
    Details hold JSON metadata, never live Django field objects or callables.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, strict=True)
    kind: str = Field(min_length=1)
    model_name: str | None = Field(default=None, min_length=1)
    field_name: str | None = Field(default=None, min_length=1)
    old_name: str | None = Field(default=None, min_length=1)
    new_name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    source: SourceLocation | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class MigrationRecord(BaseModel):
    """One migration, its resolved graph parents, and all declared operations.

    File hashes allow later comparison to detect rewritten history. Replacement
    keys retain squash metadata for explicit handling rather than guessing that
    every newly seen filename represents a new schema change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: MigrationKey
    dependencies: list[MigrationKey]
    operations: list[MigrationOperation]
    source: SourceLocation | None = None
    file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replaces: list[MigrationKey] = Field(default_factory=list[MigrationKey])

    @model_validator(mode="after")
    def validate_operation_order(self) -> Self:
        """Reject missing, duplicated, or shuffled top-level operation indices."""
        if [operation.index for operation in self.operations] != list(
            range(len(self.operations))
        ):
            raise ValueError(
                "operation indices must preserve complete zero-based order"
            )
        return self


class AnalysisIssue(BaseModel):
    """An inspection gap or history change, independent of execution verdicts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "unsupported_operation",
        "source_unavailable",
        "rewritten_migration",
        "removed_migration",
        "replacement_migration",
        "sql_unavailable",
        "sql_partial",
        "sql_state_unavailable",
        "code_unreadable",
        "code_parse_error",
        "dynamic_reference",
        "sql_parse_error",
    ]
    message: str = Field(min_length=1)
    migration: MigrationKey | None = None
    operation_index: int | None = Field(default=None, ge=0, strict=True)
    source: SourceLocation | None = None

    @model_validator(mode="after")
    def validate_operation_identity(self) -> Self:
        if self.operation_index is not None and self.migration is None:
            raise ValueError("an operation issue requires its migration identity")
        return self


class MigrationInventory(BaseModel):
    """A release's dependency-ordered migration metadata and inspection issues."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    migrations: list[MigrationRecord]
    issues: list[AnalysisIssue] = Field(default_factory=list[AnalysisIssue])

    @model_validator(mode="after")
    def validate_migration_order(self) -> Self:
        """Require unique keys and resolved dependencies before their dependents."""
        seen: set[MigrationKey] = set()
        for migration in self.migrations:
            if migration.key in seen:
                raise ValueError("duplicate migration identity in inventory")
            if any(dependency not in seen for dependency in migration.dependencies):
                raise ValueError("resolved dependencies must precede their migration")
            seen.add(migration.key)
        return self


class MigrationAnalysis(BaseModel):
    """Two release inventories, selected additions, and retained analysis gaps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base: MigrationInventory
    head: MigrationInventory
    new_migrations: list[MigrationRecord]
    issues: list[AnalysisIssue]


class MigrationSQL(BaseModel):
    """SQL collection coverage for one migration, independent of compatibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    migration: MigrationKey
    status: Literal["collected", "partial", "unavailable"]
    sql: str | None = None
    detail: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_sql_coverage(self) -> Self:
        if self.status == "unavailable":
            if self.sql is not None or self.detail is None:
                raise ValueError("unavailable SQL requires a detail and no SQL text")
        elif self.sql is None:
            raise ValueError("collected or partial SQL requires SQL text")
        elif self.status == "partial" and self.detail is None:
            raise ValueError("partial SQL requires a coverage detail")
        elif self.status == "collected" and self.detail is not None:
            raise ValueError("collected SQL cannot contain an error detail")
        return self


class SQLExtraction(BaseModel):
    """Ordered SQL records and analysis gaps, including schema preparation errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    results: list[MigrationSQL]
    issues: list[AnalysisIssue] = Field(default_factory=list[AnalysisIssue])

    @model_validator(mode="after")
    def validate_unique_migrations(self) -> Self:
        keys = [result.migration for result in self.results]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate migration in SQL extraction results")
        return self


class ReferenceTarget(BaseModel, frozen=True):
    """Names affected by one migration; these are not resolved database columns."""

    model_config = ConfigDict(extra="forbid")

    migration: MigrationKey
    model_name: str = Field(min_length=1)
    field_names: tuple[str, ...] = ()


type FieldReferenceKind = Literal[
    "attribute", "orm_keyword", "orm_string", "ordering", "serializer_field"
]


class CodeReference(BaseModel):
    """A candidate source reference, without inferred model binding or a verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: ReferenceTarget
    source: SourceLocation
    kind: FieldReferenceKind | Literal["model", "raw_sql"]
    field_name: str | None = None
    snippet: str = Field(min_length=1)
    scope: str | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.source.line is None:
            raise ValueError("a code reference requires a source line")
        if self.kind == "model":
            if self.field_name is not None:
                raise ValueError("a model reference cannot name a field")
        elif self.field_name not in self.target.field_names:
            raise ValueError("a field reference must name a target field")
        return self


class SQLLiteral(BaseModel):
    """Original SQL text and parsed identifiers, retaining unsupported syntax."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SourceLocation
    text: str
    columns: tuple[str, ...]
    tables: tuple[str, ...]
    parse_error: str | None = Field(default=None, min_length=1)


class CodeReferenceScan(BaseModel):
    """Candidate references, SQL literals, and explicit source-analysis gaps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    references: list[CodeReference]
    sql_literals: list[SQLLiteral]
    issues: list[AnalysisIssue]
