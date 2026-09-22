"""Schema migration for the WhatsApp Signals add-on.

Run from the project root:

    uv run python -m addons.whatsapp_signals.migrate --status
    uv run python -m addons.whatsapp_signals.migrate

It deliberately does **not** live in ``upgrade/migrate_all.py``. That file
belongs to upstream, and adding a line to it would put a conflict in every
future pull for the sake of one entry. Run this alongside the upstream
migrations instead -- the deployment notes say so in the upgrade step.

Creating the tables is also done by the add-on's own ``init_db()`` at start-up,
so a fresh install needs nothing. This script exists for the case that one does
not cover: adding a column to a table that already has rows. ``create_all``
skips a table that exists, so a new column would otherwise never reach an
installation that has been running -- the schema would stay on the old shape
forever and the add-on would fail on the missing field.

Every step is idempotent and safe to re-run.
"""

from __future__ import annotations

import argparse
import os
import sys

# Import from the project root whether this is run as a module or a file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import inspect, text  # noqa: E402

from addons.whatsapp_signals import db  # noqa: E402

#: All tables this add-on owns. Checked for presence; missing ones are created
#: by init_db() → create_all().
_TABLES = (
    "wa_signal_group",
    "wa_signal_event",
    "wa_signal_position",
    "wa_signal_order_profile",
    "wa_signal_ai_suggestion",
)

#: Columns added after the first release, as (table, column, DDL type clause).
#: Append here when the model gains a field; never rewrite an existing entry.
#: The DDL clause is what follows the column name in SQLite ALTER TABLE syntax.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    # v2 — configurable order parameters & AI management
    ("wa_signal_group", "order_type", "VARCHAR(10) NOT NULL DEFAULT 'MARKET'"),
    ("wa_signal_group", "limit_price_offset_pct", "FLOAT"),
    ("wa_signal_group", "order_profile_id", "INTEGER"),
    ("wa_signal_group", "auto_apply_ai", "BOOLEAN NOT NULL DEFAULT 0"),
    # v3 — AI parser mode & above-price tick offset
    ("wa_signal_group", "ai_parser_mode", "BOOLEAN NOT NULL DEFAULT 0"),
    ("wa_signal_group", "above_tick_offset", "FLOAT NOT NULL DEFAULT 0.5"),
]


def _existing_tables() -> set[str]:
    return set(inspect(db.engine).get_table_names())


def _existing_columns(table: str) -> set[str]:
    return {col["name"] for col in inspect(db.engine).get_columns(table)}


def status() -> int:
    """Report what would change, and change nothing."""
    tables = _existing_tables()
    pending = []

    for table in _TABLES:
        if table not in tables:
            pending.append(f"create table {table}")

    for table, column, _ddl in _ADDED_COLUMNS:
        if table in tables and column not in _existing_columns(table):
            pending.append(f"add {table}.{column}")

    print("WhatsApp Signals schema")
    print(f"  database: {db.DATABASE_URL}")
    for table in _TABLES:
        print(f"  {table}: {'present' if table in tables else 'MISSING'}")
    if pending:
        print("\nPending:")
        for item in pending:
            print(f"  - {item}")
    else:
        print("\nUp to date. Nothing to apply.")
    return 0


def migrate() -> int:
    """Apply the schema. Idempotent."""
    before = _existing_tables()
    db.init_db()
    after = _existing_tables()

    for table in _TABLES:
        if table in after and table not in before:
            print(f"  created {table}")

    added = 0
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in after:
            continue
        if column in _existing_columns(table):
            continue
        with db.engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        print(f"  added {table}.{column}")
        added += 1

    missing = [t for t in _TABLES if t not in _existing_tables()]
    if missing:
        print(f"  FAILED: still missing {', '.join(missing)}")
        return 1

    if not added and before == after:
        print("  already up to date")
    print("WhatsApp Signals schema ready.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WhatsApp Signals schema migration")
    parser.add_argument(
        "--status", action="store_true", help="report what would change, and change nothing"
    )
    args = parser.parse_args()
    try:
        return status() if args.status else migrate()
    finally:
        db.remove_session()


if __name__ == "__main__":
    raise SystemExit(main())
