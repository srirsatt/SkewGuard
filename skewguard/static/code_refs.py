"""Find source candidates without importing application modules or judging safety."""

import ast
import os
import re
import tokenize
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from skewguard.config import RunConfig
from skewguard.exec.envs import backend_directory
from skewguard.static.migrations import compare_inventories
from skewguard.static.types import (
    AnalysisIssue,
    CodeReference,
    CodeReferenceScan,
    FieldReferenceKind,
    MigrationAnalysis,
    ReferenceTarget,
    SourceLocation,
    SQLLiteral,
)

ORM_STRING_CALLS = frozenset(
    {
        "values",
        "values_list",
        "order_by",
        "only",
        "defer",
        "distinct",
        "latest",
        "earliest",
        "select_related",
        "prefetch_related",
        "F",
        "OuterRef",
        "Sum",
        "Avg",
        "Min",
        "Max",
        "Count",
    }
)
ORM_KEYWORD_CALLS = frozenset(
    {
        "filter",
        "exclude",
        "get",
        "create",
        "update",
        "get_or_create",
        "update_or_create",
        "Q",
    }
)
SQL_CALLS = frozenset({"execute", "executemany", "raw", "RawSQL"})
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        ".skewguard-venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        "migrations",
        ".tox",
        ".nox",
        "build",
        "dist",
    }
)
_SQL_PREFIX = re.compile(
    r"^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*"
    r"(?:SELECT|INSERT|UPDATE|DELETE|WITH|ALTER|CREATE|DROP)\b",
    re.IGNORECASE | re.DOTALL,
)


def reference_targets(analysis: MigrationAnalysis) -> list[ReferenceTarget]:
    """Group selected operation names per migration/model, including both renames."""
    if compare_inventories(analysis.base, analysis.head) != analysis:
        raise ValueError("migration analysis conflicts with its inventories")
    targets: list[ReferenceTarget] = []
    for migration in analysis.new_migrations:
        models: dict[str, set[str]] = {}
        for operation in migration.operations:
            if operation.model_name is None:
                continue
            fields = models.setdefault(operation.model_name.casefold(), set())
            if operation.field_name:
                fields.add(operation.field_name)
            if operation.kind == "RenameField":
                fields.update(
                    name for name in (operation.old_name, operation.new_name) if name
                )
            if operation.kind == "RenameModel" and operation.new_name:
                models.setdefault(operation.new_name.casefold(), set())
            if operation.kind == "CreateModel":
                declared = operation.details.get("fields")
                if isinstance(declared, list):
                    for item in declared:
                        if isinstance(item, dict):
                            name = item.get("name")
                            if isinstance(name, str):
                                fields.add(name)
        targets.extend(
            ReferenceTarget(
                migration=migration.key,
                model_name=model,
                field_names=tuple(sorted(fields)),
            )
            for model, fields in sorted(models.items())
        )
    return targets


def call_name(node: ast.AST) -> str:
    """Read the final spelling of a callable, without claiming to resolve it."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def sql_argument(node: ast.Call) -> ast.expr | None:
    """Select the query argument, excluding parameter data passed alongside it."""
    return (
        node.args[0]
        if node.args
        else next(
            (kw.value for kw in node.keywords if kw.arg in {"sql", "query"}), None
        )
    )


def field_matches(value: str, field: str) -> bool:
    """Match exact Django lookup components, allowing an ordering prefix."""
    return field in value.removeprefix("-").split("__")


def literal_reference_kind(
    node: ast.Constant, parents: dict[ast.AST, ast.AST]
) -> FieldReferenceKind | None:
    """Classify string positions that Django commonly uses for field names."""
    child: ast.AST = node
    parent = parents.get(child)
    while isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
        child, parent = parent, parents.get(parent)
    if isinstance(parent, ast.Call):
        if child in parent.args and call_name(parent.func) in ORM_STRING_CALLS:
            return "orm_string"
    if isinstance(parent, ast.keyword) and parent.arg == "update_fields":
        owner = parents.get(parent)
        if isinstance(owner, ast.Call) and call_name(owner.func) == "save":
            return "orm_string"
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        owner = parents.get(parent)
        if isinstance(owner, ast.ClassDef) and owner.name == "Meta":
            names = (
                parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            )
            if any(
                isinstance(name, ast.Name) and name.id == "ordering" for name in names
            ):
                return "ordering"
            if any(
                isinstance(name, ast.Name) and name.id == "fields" for name in names
            ):
                return "serializer_field"
    return None


def match_field_node(
    node: ast.AST,
    target: ReferenceTarget,
    parents: dict[ast.AST, ast.AST],
) -> list[tuple[str, FieldReferenceKind]]:
    """Return sorted (field, kind) matches for one AST node and affected model.

    Recognize attributes, keywords in ORM calls/model constructors, and strings
    accepted by literal_reference_kind. Match Django lookup/ordering components
    through field_matches; ignore unrelated nodes and ordinary string labels.
    Names are candidates only: this function does not infer receiver types.
    """
    value: str 
    kind: FieldReferenceKind

    if isinstance(node, ast.Attribute):
        value, kind = node.attr, "attribute"

    elif isinstance(node, ast.keyword) and node.arg is not None:
        owner = parents.get(node)
        if not isinstance(owner, ast.Call):
            return []
        name = call_name(owner.func)
        if (name not in ORM_KEYWORD_CALLS and name.casefold() != target.model_name):
            return []

        value, kind = node.arg, "orm_keyword"
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        literal_kind = literal_reference_kind(node, parents)

        if literal_kind is None:
            return []
        
        value, kind = node.value, literal_kind
    else:
        return []
    
    return [
        (field, kind)
        for field in sorted(target.field_names)
        if field_matches(value, field)
    ]


def parse_sql_literal(text: str, source: SourceLocation) -> SQLLiteral:
    """Parse PostgreSQL identifiers, retaining original text on errors/fallbacks."""
    columns: set[str] = set()
    tables: set[str] = set()
    detail: str | None = None
    try:
        statements = sqlglot.parse(text, read="postgres")
        for statement in statements:
            if statement is None:
                continue
            if any(True for _ in statement.find_all(exp.Command)):
                detail = (
                    "SQLGlot retained an opaque command; identifiers are incomplete."
                )
            columns.update(column.name for column in statement.find_all(exp.Column))
            tables.update(table.name for table in statement.find_all(exp.Table))
    except SqlglotError as error:
        detail = str(error)
    return SQLLiteral(
        source=source,
        text=text,
        columns=tuple(sorted(columns)),
        tables=tuple(sorted(tables)),
        parse_error=detail,
    )


def is_sql_literal(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
    """Distinguish complete query literals from docstrings, data, and fragments."""
    owner = parents.get(node)
    if isinstance(owner, ast.Expr):
        return False
    while owner is not None:
        if isinstance(owner, (ast.JoinedStr, ast.BinOp, ast.Attribute)):
            return False
        if isinstance(owner, ast.Call) and call_name(owner.func) in SQL_CALLS:
            return sql_argument(owner) is node
        owner = parents.get(owner)
    return isinstance(node.value, str) and _SQL_PREFIX.match(node.value) is not None


def source_context(
    node: ast.AST, parents: dict[ast.AST, ast.AST], source: str
) -> tuple[str, str | None]:
    """Keep a bounded statement snippet and the enclosing class/function names."""
    current = node
    statement = node
    scope: list[str] = []
    found_statement = False
    while current in parents:
        current = parents[current]
        if not found_statement and isinstance(current, ast.stmt):
            statement, found_statement = current, True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope.append(current.name)
    snippet = ast.get_source_segment(source, statement) or ast.unparse(node)
    return snippet[:500], ".".join(reversed(scope)) or None


def find_references(
    source: str, path: str, targets: list[ReferenceTarget]
) -> CodeReferenceScan:
    """Inspect one source string. Parse errors are coverage gaps, not app failures."""
    references: list[CodeReference] = []
    literals: list[SQLLiteral] = []
    issues: list[AnalysisIssue] = []
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError) as error:
        line = error.lineno if isinstance(error, SyntaxError) else None
        return CodeReferenceScan(
            references=[],
            sql_literals=[],
            issues=[
                AnalysisIssue(
                    kind="code_parse_error",
                    message=str(error),
                    source=SourceLocation(path=path, line=line),
                )
            ],
        )
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    nodes = list(ast.walk(tree))
    for node in nodes:
        if not isinstance(node, (ast.expr, ast.keyword, ast.alias, ast.ClassDef)):
            continue
        location = SourceLocation(path=path, line=node.lineno)
        snippet, scope = source_context(node, parents, source)
        model_name = (
            (
                node.name
                if isinstance(node, (ast.alias, ast.ClassDef))
                else call_name(node)
            )
            .rsplit(".", 1)[-1]
            .casefold()
        )
        for target in targets:
            if model_name == target.model_name.casefold():
                references.append(
                    CodeReference(
                        target=target,
                        source=location,
                        kind="model",
                        snippet=snippet,
                        scope=scope,
                    )
                )
            for field, kind in match_field_node(node, target, parents):
                references.append(
                    CodeReference(
                        target=target,
                        source=location,
                        kind=kind,
                        field_name=field,
                        snippet=snippet,
                        scope=scope,
                    )
                )

        if isinstance(node, ast.Call):
            name = call_name(node.func)
            if name in SQL_CALLS:
                query = sql_argument(node)
                if not (
                    isinstance(query, ast.Constant) and isinstance(query.value, str)
                ):
                    issues.append(
                        AnalysisIssue(
                            kind="dynamic_reference",
                            source=location,
                            message="SQL call uses a nonliteral query; no value "
                            "propagation or dynamic SQL evaluation was attempted.",
                        )
                    )
            elif name in ORM_STRING_CALLS and any(
                not (isinstance(arg, ast.Constant) and isinstance(arg.value, str))
                for arg in node.args
            ):
                issues.append(
                    AnalysisIssue(
                        kind="dynamic_reference",
                        source=location,
                        message="ORM field arguments include nonliteral expressions; "
                        "their final field names are not resolved.",
                    )
                )
            elif name in ORM_KEYWORD_CALLS and any(
                keyword.arg is None for keyword in node.keywords
            ):
                issues.append(
                    AnalysisIssue(
                        kind="dynamic_reference",
                        source=location,
                        message="ORM call expands keyword arguments; their final "
                        "field names are not resolved.",
                    )
                )

        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if is_sql_literal(node, parents):
            literal = parse_sql_literal(node.value, location)
            literals.append(literal)
            if literal.parse_error:
                issues.append(
                    AnalysisIssue(
                        kind="sql_parse_error",
                        source=location,
                        message=literal.parse_error,
                    )
                )
            for target in targets:
                for field in target.field_names:
                    if field in literal.columns:
                        references.append(
                            CodeReference(
                                target=target,
                                source=location,
                                kind="raw_sql",
                                field_name=field,
                                snippet=snippet,
                                scope=scope,
                            )
                        )
    unique = {reference.model_dump_json(): reference for reference in references}
    return CodeReferenceScan(
        references=sorted(
            unique.values(),
            key=lambda ref: (
                ref.source.path,
                ref.source.line or 0,
                ref.kind,
                ref.field_name or "",
                ref.target.migration.app_label,
                ref.target.migration.name,
                ref.target.model_name,
            ),
        ),
        sql_literals=sorted(literals, key=lambda literal: literal.source.line or 0),
        issues=issues,
    )


def scan_code_references(
    config: RunConfig, checkout: Path, analysis: MigrationAnalysis
) -> CodeReferenceScan:
    """Scan one checkout's backend; call separately for base and head releases.

    Candidate names are not bound to model receivers, tables, or custom columns.
    Imports are not executed. Symlinks, dependencies, and migration files are not
    traversed. Custom migration paths are excluded using inventory coordinates.
    """
    targets = reference_targets(analysis)
    root = checkout.resolve()
    backend = backend_directory(config, checkout)
    excluded = {
        (root / record.source.path).resolve()
        for inventory in (analysis.base, analysis.head)
        for record in inventory.migrations
        if record.source is not None
    }
    references: list[CodeReference] = []
    literals: list[SQLLiteral] = []
    issues = list(analysis.issues)

    def unreadable(error: OSError) -> None:
        issues.append(AnalysisIssue(kind="code_unreadable", message=str(error)))

    for directory, directories, filenames in os.walk(backend, onerror=unreadable):
        included: list[str] = []
        for name in sorted(directories):
            folder = Path(directory) / name
            if name in SKIP_DIRECTORIES:
                continue
            if folder.is_symlink():
                issues.append(
                    AnalysisIssue(
                        kind="code_unreadable",
                        source=SourceLocation(path=folder.relative_to(root).as_posix()),
                        message="Symlinked source directory was excluded "
                        "from inspection.",
                    )
                )
            else:
                included.append(name)
        directories[:] = included
        for name in sorted(filenames):
            file = Path(directory) / name
            if file.suffix != ".py" or file.resolve() in excluded:
                continue
            location = SourceLocation(path=file.relative_to(root).as_posix())
            if file.is_symlink():
                issues.append(
                    AnalysisIssue(
                        kind="code_unreadable",
                        source=location,
                        message="Symlinked source file was excluded from inspection.",
                    )
                )
                continue
            try:
                with tokenize.open(file) as stream:
                    source = stream.read()
            except (OSError, UnicodeError, SyntaxError) as error:
                issues.append(
                    AnalysisIssue(
                        kind="code_unreadable",
                        source=location,
                        message=str(error),
                    )
                )
                continue
            scan = find_references(source, location.path, targets)
            references.extend(scan.references)
            literals.extend(scan.sql_literals)
            issues.extend(scan.issues)
    return CodeReferenceScan(
        references=references,
        sql_literals=literals,
        issues=issues,
    )
