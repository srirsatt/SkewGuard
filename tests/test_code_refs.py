"""Source candidates remain distinct from execution evidence and compatibility."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from skewguard.config import RunConfig
from skewguard.static import code_refs
from skewguard.static.code_refs import (
    find_references,
    match_field_node,
    parse_sql_literal,
    reference_targets,
    scan_code_references,
)
from skewguard.static.migrations import compare_inventories
from skewguard.static.types import (
    CodeReference,
    CodeReferenceScan,
    MigrationAnalysis,
    MigrationInventory,
    MigrationKey,
    MigrationOperation,
    MigrationRecord,
    ReferenceTarget,
    SourceLocation,
)


def change_analysis(
    operations: list[MigrationOperation] | None = None,
    *,
    migration_path: str | None = None,
) -> MigrationAnalysis:
    old = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0001_initial"),
        dependencies=[],
        operations=[],
        file_sha256="a" * 64,
    )
    new = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0002_change"),
        dependencies=[old.key],
        operations=operations
        if operations is not None
        else [
            MigrationOperation(
                index=0,
                kind="RenameField",
                model_name="product",
                old_name="price",
                new_name="amount",
            )
        ],
        source=SourceLocation(path=migration_path) if migration_path else None,
        file_sha256="b" * 64,
    )
    return compare_inventories(
        MigrationInventory(migrations=[old]),
        MigrationInventory(migrations=[old, new]),
    )


def target() -> ReferenceTarget:
    return reference_targets(change_analysis())[0]


@pytest.mark.parametrize(
    ("source", "kind", "fields"),
    [
        ("product.price", "attribute", {"price"}),
        ("product.price = 10", "attribute", {"price"}),
        ("Product.objects.filter(price__gte=3)", "orm_keyword", {"price"}),
        ("Product.objects.exclude(shop__price=3)", "orm_keyword", {"price"}),
        ("Product.objects.update(amount=3)", "orm_keyword", {"amount"}),
        ("Product(price=3)", "orm_keyword", {"price"}),
        ("Q(price__isnull=True)", "orm_keyword", {"price"}),
        (
            'Product.objects.values("price", "amount")',
            "orm_string",
            {"price", "amount"},
        ),
        ('Product.objects.values_list("price", flat=True)', "orm_string", {"price"}),
        ('Product.objects.order_by("-price")', "orm_string", {"price"}),
        ('Product.objects.only("price")', "orm_string", {"price"}),
        ('Product.objects.defer("price")', "orm_string", {"price"}),
        ('F("price")', "orm_string", {"price"}),
        ('Sum("price")', "orm_string", {"price"}),
        ('product.save(update_fields=["price"])', "orm_string", {"price"}),
        ('class Meta:\n    ordering = ["-price", "pk"]', "ordering", {"price"}),
        ('class Meta:\n    ordering: tuple = ("price",)', "ordering", {"price"}),
        ('class Meta:\n    fields = ("sku", "price")', "serializer_field", {"price"}),
    ],
)
def test_match_field_node_recognizes_supported_syntax(
    source: str, kind: str, fields: set[str]
) -> None:
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    matches = {
        match
        for node in ast.walk(tree)
        for match in match_field_node(node, target(), parents)
    }
    assert matches == {(field, kind) for field in fields}


@pytest.mark.parametrize(
    "source",
    [
        'label = "price"',
        'render(price="price")',
        "Product.objects.filter(price_total=4)",
        'Product.objects.values("surprice")',
        'class Other:\n    fields = ["price"]',
        'Product.objects.filter(description="price")',
        "Product.objects.values(dynamic_field)",
        "Product.objects.filter(**filters)",
    ],
)
def test_match_field_node_ignores_unrelated_or_unresolved_syntax(source: str) -> None:
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    assert [
        match
        for node in ast.walk(tree)
        for match in match_field_node(node, target(), parents)
    ] == []


def test_targets_group_fields_but_keep_models_and_migrations_distinct() -> None:
    analysis = change_analysis(
        [
            MigrationOperation(
                index=0,
                kind="RenameField",
                model_name="Product",
                old_name="price",
                new_name="amount",
            ),
            MigrationOperation(
                index=1, kind="AddField", model_name="product", field_name="notes"
            ),
            MigrationOperation(
                index=2,
                kind="RenameModel",
                model_name="Product",
                old_name="Product",
                new_name="Item",
            ),
            MigrationOperation(index=3, kind="DeleteModel", model_name="Coupon"),
            MigrationOperation(index=4, kind="RunSQL"),
            MigrationOperation(
                index=5,
                kind="CreateModel",
                model_name="Order",
                details={"fields": [{"name": "total"}]},
            ),
        ]
    )
    targets = reference_targets(analysis)
    assert [(item.model_name, item.field_names) for item in targets] == [
        ("coupon", ()),
        ("item", ()),
        ("order", ("total",)),
        ("product", ("amount", "notes", "price")),
    ]
    assert all(item.migration == analysis.new_migrations[0].key for item in targets)


def test_forged_analysis_selection_is_rejected() -> None:
    analysis = change_analysis().model_copy(update={"new_migrations": []})
    with pytest.raises(ValueError, match="conflicts"):
        reference_targets(analysis)


def test_scan_keeps_locations_scope_and_multiple_app_candidates() -> None:
    other = target().model_copy(
        update={
            "migration": MigrationKey(app_label="billing", name="0002_change"),
        }
    )
    source = (
        "from catalog.models import Product as Item\n"
        "def view(request):\n"
        "    return Item.objects.filter(price__gte=1).values('price')\n"
    )
    scan = find_references(source, "catalog/views.py", [target(), other])
    assert len([ref for ref in scan.references if ref.kind == "orm_string"]) == 2
    keyword = next(ref for ref in scan.references if ref.kind == "orm_keyword")
    assert keyword.source == SourceLocation(path="catalog/views.py", line=3)
    assert keyword.scope == "view"
    assert "Item.objects.filter" in keyword.snippet
    assert any(ref.kind == "model" and ref.source.line == 1 for ref in scan.references)
    assert not scan.issues
    assert CodeReferenceScan.model_validate_json(scan.model_dump_json()) == scan


@pytest.mark.parametrize(
    "text",
    [
        "SELECT p.price FROM catalog_product p WHERE p.sku = %s",
        "SELECT price FROM catalog_product WHERE sku = %(sku)s",
        "UPDATE catalog_product SET price = 10 WHERE sku = 'one'",
        "SELECT 'price' AS label, price FROM catalog_product; "
        "SELECT amount FROM invoices",
    ],
)
def test_postgres_sql_identifiers_and_placeholders(text: str) -> None:
    literal = parse_sql_literal(text, SourceLocation(path="jobs.py", line=7))
    assert "price" in literal.columns
    assert "catalog_product" in literal.tables
    assert literal.text == text
    assert literal.parse_error is None


def test_sql_string_values_do_not_become_column_references() -> None:
    literal = parse_sql_literal(
        "SELECT 'price' AS label FROM catalog_product",
        SourceLocation(path="view.py", line=1),
    )
    assert "price" not in literal.columns


@pytest.mark.parametrize("text", ["SELECT price FROM (", "VACUUM catalog_product"])
def test_sql_failures_and_opaque_commands_preserve_text(text: str) -> None:
    literal = parse_sql_literal(text, SourceLocation(path="jobs.py", line=1))
    assert literal.text == text
    assert literal.parse_error


def test_raw_queries_include_management_commands_and_assigned_literals() -> None:
    source = (
        "def handle():\n"
        "    sql = 'SELECT price FROM catalog_product WHERE sku = %s'\n"
        "    cursor.execute(sql, ['one'])\n"
        "    cursor.execute(query='SELECT amount FROM catalog_product')\n"
        "    Product.objects.raw('SELECT price FROM catalog_product', ['one'])\n"
        "    RawSQL('SELECT amount FROM catalog_product', [])\n"
    )
    scan = find_references(source, "catalog/management/commands/job.py", [target()])
    assert len(scan.sql_literals) == 4
    assert {ref.field_name for ref in scan.references if ref.kind == "raw_sql"} == {
        "price",
        "amount",
    }
    assert [issue.kind for issue in scan.issues] == ["dynamic_reference"]


@pytest.mark.parametrize(
    "expression",
    [
        'f"SELECT {column} FROM catalog_product"',
        '"SELECT " + column',
        '"SELECT {} FROM catalog_product".format(column)',
        '"SELECT %s FROM catalog_product" % column',
    ],
)
def test_dynamic_sql_is_a_gap_and_fragments_are_not_parsed(expression: str) -> None:
    scan = find_references(f"cursor.execute({expression})", "view.py", [target()])
    assert not scan.sql_literals
    assert any(issue.kind == "dynamic_reference" for issue in scan.issues)


def test_docstrings_and_sql_parameter_values_are_not_queries() -> None:
    scan = find_references(
        '"""SELECT price FROM catalog_product"""\n'
        'cursor.execute("SELECT 1", ["price", "SELECT price FROM catalog_product"])',
        "view.py",
        [target()],
    )
    assert [literal.text for literal in scan.sql_literals] == ["SELECT 1"]


def test_dynamic_keyword_expansion_is_reported() -> None:
    scan = find_references("Product.objects.filter(**criteria)", "view.py", [target()])
    assert [issue.kind for issue in scan.issues] == ["dynamic_reference"]


def test_parse_errors_are_explicit_and_dynamic_field_names_stay_unresolved() -> None:
    broken = find_references("def broken(:", "broken.py", [target()])
    assert not broken.references
    assert broken.issues[0].kind == "code_parse_error"
    assert broken.issues[0].source == SourceLocation(path="broken.py", line=1)
    scan = find_references(
        'Product.objects.values(column)\ncursor.execute("SELECT price FROM (")',
        "view.py",
        [target()],
    )
    assert {issue.kind for issue in scan.issues} == {
        "dynamic_reference",
        "sql_parse_error",
    }
    assert scan.sql_literals[0].parse_error


def test_reference_requires_real_coordinates_and_a_target_field() -> None:
    values = {
        "target": target(),
        "source": SourceLocation(path="view.py", line=1),
        "kind": "orm_string",
        "field_name": "price",
        "snippet": 'values("price")',
    }
    CodeReference.model_validate(values)
    with pytest.raises(ValidationError, match="requires a source line"):
        CodeReference.model_validate({**values, "source": SourceLocation(path="v.py")})
    with pytest.raises(ValidationError, match="target field"):
        CodeReference.model_validate({**values, "field_name": "missing"})


def config_at(root: Path, backend: str = ".") -> RunConfig:
    return RunConfig(
        repo_path=root, base_ref="base", head_ref="head", backend_path=Path(backend)
    )


def test_backend_scan_ignores_dependencies_and_migrations_without_importing(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    marker = tmp_path / "imported"
    (backend / "views.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "raise RuntimeError('must never execute')\n"
        "Product.objects.values('price')\n",
        encoding="utf-8",
    )
    for directory in ("migrations", ".venv", ".skewguard-venv", "node_modules"):
        folder = backend / directory
        folder.mkdir()
        (folder / "ignored.py").write_text(
            "Product.objects.values('price')", encoding="utf-8"
        )
    (backend / "custom_migration.py").write_text(
        "Product.objects.values('price')", encoding="utf-8"
    )
    (backend / "broken.py").write_text("def broken(:", encoding="utf-8")
    (tmp_path / "outside_backend.py").write_text(
        "Product.objects.values('price')", encoding="utf-8"
    )
    analysis = change_analysis(migration_path="backend/custom_migration.py")
    scan = scan_code_references(config_at(tmp_path, "backend"), tmp_path, analysis)
    assert {ref.source.path for ref in scan.references} == {"backend/views.py"}
    assert any(issue.kind == "code_parse_error" for issue in scan.issues)
    assert not marker.exists()
    assert scan == scan_code_references(
        config_at(tmp_path, "backend"), tmp_path, analysis
    )


def test_symlinked_files_and_directories_do_not_escape_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "view.py").write_text(
        "Product.objects.values('price')", encoding="utf-8"
    )
    (checkout / "linked.py").symlink_to(outside / "view.py")
    (checkout / "linked_dir").symlink_to(outside, target_is_directory=True)
    scan = scan_code_references(config_at(checkout), checkout, change_analysis())
    assert not scan.references
    assert any(issue.kind == "code_unreadable" for issue in scan.issues)


def test_python_encoding_cookie_is_respected_and_decode_errors_retained(
    tmp_path: Path,
) -> None:
    (tmp_path / "latin.py").write_bytes(
        b"# coding: latin-1\n# caf\xe9\nProduct.objects.values('price')\n"
    )
    (tmp_path / "invalid.py").write_bytes(b"\xff\n")
    scan = scan_code_references(config_at(tmp_path), tmp_path, change_analysis())
    assert any(ref.source.path == "latin.py" for ref in scan.references)
    assert any(issue.kind == "code_unreadable" for issue in scan.issues)


def test_shop_fixture_finds_values_and_ordering() -> None:
    root = Path(__file__).resolve().parents[1]
    scan = scan_code_references(
        config_at(root, "benchmark/apps/shop"), root, change_analysis()
    )
    assert any(
        ref.kind == "ordering" and ref.field_name == "price" for ref in scan.references
    )
    assert any(
        ref.kind == "orm_string"
        and ref.source.path.endswith("catalog/views.py")
        and ref.field_name == "price"
        for ref in scan.references
    )


def test_unreadable_source_is_retained_as_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "view.py").write_text("", encoding="utf-8")

    def fail_open(filename: object) -> None:
        raise PermissionError("source cannot be read")

    monkeypatch.setattr(code_refs.tokenize, "open", fail_open)
    scan = scan_code_references(config_at(tmp_path), tmp_path, change_analysis())
    assert scan.issues[0].kind == "code_unreadable"
    assert "source cannot be read" in scan.issues[0].message
