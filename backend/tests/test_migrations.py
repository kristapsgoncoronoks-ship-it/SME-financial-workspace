"""Foundation guard: Alembic migrations are the production schema authority
(create_all is dev/CI only). These tests keep migrations honest:

1. Migrations apply cleanly from an EMPTY database, and downgrade→upgrade
   round-trips — so a broken or non-reversible migration fails the build.
2. The schema produced by running all migrations MATCHES the schema the ORM
   models declare (table + column parity) — so a model change that ships without
   a matching migration is caught in CI, not discovered in production.

Portability note: we compare two databases we both build (models via create_all
vs. migrations via alembic) with the SQLAlchemy inspector, rather than relying on
Alembic autogenerate reflection (which is noisy on SQLite). This is deterministic
and dialect-robust for the table/column parity that matters most.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Tables that legitimately exist in one DB but not the other.
_IGNORE_TABLES = {"alembic_version"}


def _run_alembic(args: list[str], db_url_async: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url_async}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _schema(sync_url: str) -> dict[str, set[str]]:
    """{table_name: {column_names}} for a built database."""
    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        out: dict[str, set[str]] = {}
        for table in insp.get_table_names():
            if table in _IGNORE_TABLES:
                continue
            out[table] = {c["name"] for c in insp.get_columns(table)}
        return out
    finally:
        engine.dispose()


@pytest.mark.slow
def test_migrations_apply_and_roundtrip_from_empty(tmp_path):
    db_file = tmp_path / "mig.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    up = _run_alembic(["upgrade", "head"], async_url)
    assert up.returncode == 0, f"alembic upgrade head failed:\n{up.stdout}\n{up.stderr}"

    # Full reversibility: unwind every migration, then re-apply.
    down = _run_alembic(["downgrade", "base"], async_url)
    assert down.returncode == 0, f"alembic downgrade base failed:\n{down.stdout}\n{down.stderr}"
    reup = _run_alembic(["upgrade", "head"], async_url)
    assert reup.returncode == 0, f"alembic re-upgrade failed:\n{reup.stdout}\n{reup.stderr}"


@pytest.mark.slow
def test_wo8_fx_data_migration_corrects_multiplied_amounts(tmp_path):
    """WO-8: the data migration (b1c3e5a7f9d1) maps free-text fx_source through
    the explicit table, corrects amounts produced by the old original×rate
    multiply ON UNDECIDED reports (divide is the one convention), leaves DECIDED
    reports untouched (flagged — a business decision, docs/DECISIONS-NEEDED.md),
    and closes fx_source behind a CHECK constraint."""
    db_file = tmp_path / "wo8.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    up = _run_alembic(["upgrade", "c2d4f6a8b0e3"], async_url)  # the pre-WO-8 head
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"

    eng = create_engine(f"sqlite:///{db_file}")
    with eng.begin() as con:
        con.exec_driver_sql(
            "INSERT INTO organizations (id, name, ai_validation_enabled, "
            "human_validation_enabled, plan, status, region) "
            "VALUES ('org1', 'O', 0, 0, 'trial', 'active', 'eu')"
        )
        # These reports exercise historical FX arithmetic, not a missing
        # employee. Supply their real parent before the later global FK check.
        from tests.test_planning_ocr_migration_integration import _insert

        _insert(
            con,
            "users",
            id="u1",
            org_id="org1",
            email="synthetic-wo8@example.invalid",
            name="Synthetic WO8 employee",
            hashed_password="not-a-login-hash",
            role="user",
        )

        def report(rid, status, total):
            con.exec_driver_sql(
                "INSERT INTO expense_reports (id, org_id, employee_id, employee_name,"
                " title, status, currency, total, vat_total) VALUES "
                f"('{rid}', 'org1', 'u1', 'E', 'T', '{status}', 'EUR', {total}, 0)"
            )

        def item(iid, rid, amount, orig, fx_rate, fx_source):
            con.exec_driver_sql(
                "INSERT INTO expense_items (id, org_id, report_id, spend_date, category,"
                " description, merchant, amount, currency, original_amount, fx_rate,"
                " fx_source, vat_amount, reclaimable_tax, payment_method,"
                " customer_billable, expense_type) VALUES "
                f"('{iid}', 'org1', '{rid}', '2026-05-01', 'travel', 'X', NULL,"
                f" {amount}, 'USD', {orig}, {fx_rate}, '{fx_source}', 0, 1,"
                " 'personal', 0, 'standard')"
            )

        # Draft report: multiplied amount (100 × 1.23456 = 123.46) → corrected.
        report("r1", "draft", "123.46")
        item("i1", "r1", "123.46", "100.00", "1.23456", "ECB")
        # Approved report: same defect → FLAGGED, never restated by a migration.
        report("r2", "approved", "123.46")
        item("i2", "r2", "123.46", "100.00", "1.23456", "manual")
        # Unmappable free text → 'unknown' (never a guess).
        report("r3", "draft", "50.00")
        item("i3", "r3", "50.00", "40.00", "0.80", "totally-custom")

    up2 = _run_alembic(["upgrade", "head"], async_url)
    assert up2.returncode == 0, f"{up2.stdout}\n{up2.stderr}"
    # The migration PRINTS its reconciliation report (old → new, per bucket).
    assert "[WO-8]" in up2.stdout
    assert "corrected item i1" in up2.stdout
    assert "FLAGGED item i2" in up2.stdout

    with eng.begin() as con:
        rows = {
            r[0]: r
            for r in con.exec_driver_sql(
                "SELECT id, amount, fx_source FROM expense_items"
            ).fetchall()
        }
        # Corrected to the divide convention: 100 / 1.23456 = 81.00.
        assert float(rows["i1"][1]) == 81.00
        assert rows["i1"][2] == "ecb"  # free text mapped through the table
        # Decided report untouched (the wrong 123.46 stays until the business
        # decides — see docs/DECISIONS-NEEDED.md), provenance still mapped.
        assert float(rows["i2"][1]) == 123.46
        assert rows["i2"][2] == "stated"  # 'manual' → stated
        # 40 / 0.80 = 50.00 == stored → NOT a multiply artefact; only the
        # unmappable provenance changes.
        assert float(rows["i3"][1]) == 50.00
        assert rows["i3"][2] == "unknown"
        # The corrected report's total was recomputed from its items.
        totals = {
            r[0]: float(r[1])
            for r in con.exec_driver_sql("SELECT id, total FROM expense_reports").fetchall()
        }
        assert totals["r1"] == 81.00
        assert totals["r2"] == 123.46

        # The enum is now CLOSED: free text is refused by the CHECK constraint.
        import sqlalchemy.exc

        try:
            con.exec_driver_sql("UPDATE expense_items SET fx_source = 'banana' WHERE id = 'i1'")
            raise AssertionError("free-text fx_source must be refused after WO-8")
        except sqlalchemy.exc.IntegrityError:
            pass
    eng.dispose()


@pytest.mark.slow
def test_wo88_fx_provenance_migration_refuses_to_run_over_a_violating_row(tmp_path):
    """WO-88 (e4a7c1d92f08): the constraints close the combination
    `fx_source='unknown'` + a stored EUR figure, and a non-EUR currency with no
    provenance at all. A row that already violates the invariant cannot be
    corrected — the rate does not exist, the column is NOT NULL, and deleting
    transaction history is a business decision (§9) — so the migration prints
    the offending row and REFUSES rather than guessing or deleting.

    Both halves are asserted, because a migration that only ever ran over a
    clean database would prove nothing about the branch that matters."""
    db_file = tmp_path / "wo88.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    up = _run_alembic(["upgrade", "b3d8f1c04e97"], async_url)  # the pre-WO-88 head
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"

    eng = create_engine(f"sqlite:///{db_file}")
    with eng.begin() as con:
        con.exec_driver_sql(
            "INSERT INTO fuel_transactions (id, org_id, entity_id, supplier, period, line_seq,"
            " country, vehicle_ref, txn_date, txn_time, station, product, product_group, qty,"
            " currency, net_local, vat_local, gross_local, net_eur, vat_eur, net_eur_eff,"
            " fx_source) VALUES ('bad1', 'org1', 'ent1', 'Q8', '2026-05', 1, 'LV',"
            " 'Fleet-Test-01', '2026-05-14', '', 'Demo Station Riga', 'DIESEL', 'Diesel',"
            " 1000.000, 'PLN', 6000.00, 1380.00, 7380.00, 1400.00, 294.00, 1400.00, NULL)"
        )

    refused = _run_alembic(["upgrade", "head"], async_url)
    assert refused.returncode != 0, f"the migration must refuse:\n{refused.stdout}"
    assert "[WO-88] 1 violating rows" in refused.stdout
    assert "bad1" in refused.stdout
    assert "refusing to migrate" in refused.stdout + refused.stderr

    # …and the schema is untouched: the constraint was never created.
    with eng.begin() as con:
        ddl = con.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'fuel_transactions'"
        ).scalar_one()
        assert "ck_fuel_transactions_fx_provenance" not in ddl
        con.exec_driver_sql("DELETE FROM fuel_transactions WHERE id = 'bad1'")

    ok = _run_alembic(["upgrade", "head"], async_url)
    assert ok.returncode == 0, f"{ok.stdout}\n{ok.stderr}"
    assert "[WO-88] 0 violating rows" in ok.stdout

    with eng.begin() as con:
        import sqlalchemy.exc

        for name in (
            "ck_fuel_transactions_fx_provenance",
            "ck_vat_off_invoice_rebates_fx_source",
            "ck_vat_off_invoice_rebates_fx_provenance",
        ):
            table = "fuel_transactions" if "fuel" in name else "vat_off_invoice_rebates"
            ddl = con.exec_driver_sql(
                f"SELECT sql FROM sqlite_master WHERE name = '{table}'"
            ).scalar_one()
            assert name in ddl, name
        # The pre-existing constraints survived the SQLite table rebuild.
        ddl = con.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'fuel_transactions'"
        ).scalar_one()
        assert "ck_fuel_transactions_product_group" in ddl
        assert "ck_fuel_transactions_fx_source" in ddl
        assert "uq_fuel_transactions_natural_key" in ddl

        # And the database now refuses the row the service refuses.
        try:
            con.exec_driver_sql(
                "INSERT INTO fuel_transactions (id, org_id, entity_id, supplier, period,"
                " line_seq, country, vehicle_ref, txn_date, txn_time, station, product,"
                " product_group, qty, currency, net_local, vat_local, gross_local, net_eur,"
                " vat_eur, net_eur_eff, fx_source) VALUES ('bad2', 'org1', 'ent1', 'Q8',"
                " '2026-05', 2, 'LV', 'Fleet-Test-01', '2026-05-14', '', 'Demo Station Riga',"
                " 'DIESEL', 'Diesel', 1000.000, 'EUR', 1400.00, 294.00, 1694.00, 1400.00,"
                " 294.00, 1400.00, 'unknown')"
            )
            raise AssertionError("an unknown fx_source beside a EUR figure must be refused")
        except sqlalchemy.exc.IntegrityError:
            pass
    eng.dispose()


@pytest.mark.slow
def test_wo89_wrong_provenance_migration_refuses_to_run_over_a_violating_row(tmp_path):
    """WO-89 (a7f2e9c41b83): the widened constraints close the third
    combination — a non-EUR currency claiming `fx_source='eur'`, the identity
    provenance meaning *"the amount was already EUR"*. That row is LEGAL at the
    WO-88 head, which is what makes this test's fixture buildable at all and
    what makes the migration's pre-flight load-bearing.

    Same refusal discipline as WO-88's: the rate the row should have used cannot
    be reconstructed, the euro column is NOT NULL, and deleting transaction
    history is a business decision (§9) — so the migration prints the offending
    row and REFUSES. Both halves asserted, plus the drop-and-recreate leaving
    every other constraint on the rebuilt table intact.
    """
    db_file = tmp_path / "wo89.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    up = _run_alembic(["upgrade", "e4a7c1d92f08"], async_url)  # the WO-88 head
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"

    # Built at runtime rather than written as a literal, so no fixture in the
    # tree carries anything shaped like a real vehicle/registration identifier.
    vehicle_ref = "-".join(["Fleet", "Test", "89"])
    columns = (
        "id, org_id, entity_id, supplier, period, line_seq, country, vehicle_ref, txn_date,"
        " txn_time, station, product, product_group, qty, currency, net_local, vat_local,"
        " gross_local, net_eur, vat_eur, net_eur_eff, fx_source"
    )

    eng = create_engine(f"sqlite:///{db_file}")
    with eng.begin() as con:
        # A PLN line asserting €1,400.00 with the EUR IDENTITY provenance. This
        # INSERT succeeding at the WO-88 head is itself the finding WO-89 exists
        # to close — if a future change makes it fail here, this test is telling
        # you the fix landed one revision earlier than it claims to have.
        con.exec_driver_sql(
            f"INSERT INTO fuel_transactions ({columns}) VALUES ('bad89', 'org1', 'ent1', 'Q8',"
            f" '2026-05', 1, 'LV', '{vehicle_ref}', '2026-05-14', '', 'Demo Station Riga',"
            " 'DIESEL', 'Diesel', 1000.000, 'PLN', 6000.00, 1380.00, 7380.00, 1400.00,"
            " 294.00, 1400.00, 'eur')"
        )

    refused = _run_alembic(["upgrade", "head"], async_url)
    assert refused.returncode != 0, f"the migration must refuse:\n{refused.stdout}"
    assert "[WO-89] 1 violating rows" in refused.stdout
    assert "bad89" in refused.stdout
    assert "refusing to migrate" in refused.stdout + refused.stderr

    # …and the schema is untouched: the OLD constraint is still in place, which
    # matters more here than in WO-88 because this migration DROPS before it
    # creates. A failed run must not leave the table unprotected.
    with eng.begin() as con:
        ddl = con.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'fuel_transactions'"
        ).scalar_one()
        assert "ck_fuel_transactions_fx_provenance" in ddl
        assert "fx_source <> 'eur'" not in ddl  # the WO-88 expression, not WO-89's
        con.exec_driver_sql("DELETE FROM fuel_transactions WHERE id = 'bad89'")

    ok = _run_alembic(["upgrade", "head"], async_url)
    assert ok.returncode == 0, f"{ok.stdout}\n{ok.stderr}"
    assert "[WO-89] 0 violating rows" in ok.stdout

    with eng.begin() as con:
        import sqlalchemy.exc

        ddl = con.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'fuel_transactions'"
        ).scalar_one()
        assert "fx_source <> 'eur'" in ddl  # the third conjunct is live
        # The SQLite batch rebuild kept everything else on the table.
        for surviving in (
            "ck_fuel_transactions_product_group",
            "ck_fuel_transactions_fx_source",
            "uq_fuel_transactions_natural_key",
            "uq_fuel_transactions_org_id_id",
            "fk_fuel_transactions_entity",
            "fk_fuel_transactions_invoice",
        ):
            assert surviving in ddl, surviving

        rebate_ddl = con.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'vat_off_invoice_rebates'"
        ).scalar_one()
        assert "fx_source <> 'eur'" in rebate_ddl
        assert "ck_vat_off_invoice_rebates_fx_source" in rebate_ddl  # WO-88's, untouched
        assert "ck_vat_off_invoice_rebates_eur_positive" in rebate_ddl

        # And the database now refuses the row it accepted one revision ago.
        try:
            con.exec_driver_sql(
                f"INSERT INTO fuel_transactions ({columns}) VALUES ('bad89b', 'org1', 'ent1',"
                f" 'Q8', '2026-05', 2, 'LV', '{vehicle_ref}', '2026-05-14', '',"
                " 'Demo Station Riga', 'DIESEL', 'Diesel', 1000.000, 'PLN', 6000.00, 1380.00,"
                " 7380.00, 1400.00, 294.00, 1400.00, 'eur')"
            )
            raise AssertionError("a non-EUR line claiming the EUR identity must be refused")
        except sqlalchemy.exc.IntegrityError:
            pass
    eng.dispose()


def _constraints(sync_url: str) -> dict[str, dict[str, set]]:
    """DB-012 (audit 2026-09-05): the promises a column list does not show.

    Per table: unique column-sets (a UNIQUE constraint and a unique index are
    the same promise, so they are folded together), non-unique index column
    tuples, foreign keys as (columns, referred table, ON DELETE), CHECK
    constraint texts (whitespace-normalised), and the NOT NULL column set.
    Names are deliberately NOT compared — batch_alter_table renames them and a
    name is not a promise — but every one of these IS."""
    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        out: dict[str, dict[str, set]] = {}
        for table in insp.get_table_names():
            if table in _IGNORE_TABLES:
                continue
            uniques = {tuple(sorted(u["column_names"])) for u in insp.get_unique_constraints(table)}
            indexes = insp.get_indexes(table)
            uniques |= {tuple(sorted(i["column_names"])) for i in indexes if i["unique"]}
            out[table] = {
                "unique": uniques,
                "index": {tuple(i["column_names"]) for i in indexes if not i["unique"]},
                "foreign_key": {
                    (
                        tuple(f["constrained_columns"]),
                        f["referred_table"],
                        (f.get("options") or {}).get("ondelete"),
                    )
                    for f in insp.get_foreign_keys(table)
                },
                "check": {
                    " ".join(c["sqltext"].split()) for c in insp.get_check_constraints(table)
                },
                "not_null": {c["name"] for c in insp.get_columns(table) if not c["nullable"]},
            }
        return out
    finally:
        engine.dispose()


@pytest.mark.slow
def test_db012_models_and_migrations_agree_on_constraints_indexes_and_fk_actions(tmp_path):
    """DB-012: `test_models_match_migration_head` compares tables and columns.
    A migration that forgets `ondelete="CASCADE"`, a UNIQUE the model declares
    but no migration creates, or a CHECK that exists only in `create_all`, all
    passed it — and each is exactly the kind of drift that only shows up in
    production (a tenant purge that stops on an FK, a duplicate that a test
    database never produced). This compares the promises themselves."""
    models_file = tmp_path / "models.db"
    eng = create_engine(f"sqlite:///{models_file}")
    import app.models  # noqa: F401
    from app.models.base import Base

    Base.metadata.create_all(eng)
    eng.dispose()
    models = _constraints(f"sqlite:///{models_file}")

    mig_file = tmp_path / "migrated.db"
    res = _run_alembic(["upgrade", "head"], f"sqlite+aiosqlite:///{mig_file}")
    assert res.returncode == 0, f"alembic upgrade head failed:\n{res.stdout}\n{res.stderr}"
    migrated = _constraints(f"sqlite:///{mig_file}")

    # The comparison must be measuring something: the schema carries hundreds
    # of FKs with an ON DELETE action, dozens of UNIQUEs and CHECKs.
    assert sum(len(t["foreign_key"]) for t in models.values()) > 100
    assert any(od for t in models.values() for (_c, _r, od) in t["foreign_key"])
    assert sum(len(t["check"]) for t in models.values()) > 0
    assert sum(len(t["unique"]) for t in models.values()) > 20

    drift: list[str] = []
    for table in sorted(set(models) & set(migrated)):
        for kind in ("unique", "index", "foreign_key", "check", "not_null"):
            a, b = models[table][kind], migrated[table][kind]
            if a != b:
                drift.append(
                    f"  {table}.{kind}: declared by models but not migrated={sorted(a - b)}; "
                    f"migrated but not in models={sorted(b - a)}"
                )
    assert not drift, (
        "Model/migration constraint drift — write a migration (or fix the model) so the "
        "database keeps the promise the code relies on:\n" + "\n".join(drift)
    )


@pytest.mark.slow
def test_models_match_migration_head(tmp_path):
    # DB A: what the ORM models declare.
    models_file = tmp_path / "models.db"
    eng = create_engine(f"sqlite:///{models_file}")
    import app.models  # noqa: F401
    from app.models.base import Base

    Base.metadata.create_all(eng)
    eng.dispose()
    models_schema = _schema(f"sqlite:///{models_file}")

    # DB B: what the migrations produce.
    mig_file = tmp_path / "migrated.db"
    res = _run_alembic(["upgrade", "head"], f"sqlite+aiosqlite:///{mig_file}")
    assert res.returncode == 0, f"alembic upgrade head failed:\n{res.stdout}\n{res.stderr}"
    migrated_schema = _schema(f"sqlite:///{mig_file}")

    # Table parity.
    only_models = set(models_schema) - set(migrated_schema)
    only_migrations = set(migrated_schema) - set(models_schema)
    assert not only_models, (
        f"Tables declared by models but NOT created by any migration: {sorted(only_models)}. "
        "Generate a migration for the new model(s)."
    )
    assert not only_migrations, (
        f"Tables created by migrations but not present in the models: {sorted(only_migrations)}. "
        "Update the models or add a migration to drop the stale table."
    )

    # Column parity per shared table.
    mismatches: dict[str, dict] = {}
    for table in sorted(set(models_schema) & set(migrated_schema)):
        missing_in_migrations = models_schema[table] - migrated_schema[table]
        missing_in_models = migrated_schema[table] - models_schema[table]
        if missing_in_migrations or missing_in_models:
            mismatches[table] = {
                "declared_by_models_but_not_migrated": sorted(missing_in_migrations),
                "migrated_but_not_in_models": sorted(missing_in_models),
            }
    assert not mismatches, (
        "Model/migration column drift detected — add a migration to reconcile:\n"
        + "\n".join(f"  {t}: {d}" for t, d in mismatches.items())
    )
