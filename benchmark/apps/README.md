# Benchmark applications

## Shop

`shop/` is an in-repository Django 5.2.19 + PostgreSQL fixture. It has a product
model, a plain Django JSON view at `/products/`, deterministic ordering, a
`.values()` query, and two fixed product records in `catalog/fixtures/products.json`.
Dependency versions are pinned in `shop/requirements.txt`.

The base application uses `price`. The `shop_rename_price` case copies its
migration into the head release and updates the head model, ordering, and view
to use `amount`. The `shop_add_notes` case adds a nullable field. Case labels and
migration sources live in `benchmark/cases/`; labels are expectations, not
measured benchmark results.

SkewGuard injects its disposable database connection. For manual Django commands,
the fixture accepts `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD`.
No external applications have been added yet.
