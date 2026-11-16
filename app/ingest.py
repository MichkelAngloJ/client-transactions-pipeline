"""
Load the transactions CSV into Postgres.

    python -m app.ingest data/client_transactions_may_oct_2026.csv

All tables are rebuilt from scratch within a single database transaction,
so a failed run leaves the previous load unchanged.
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from app.cleaning import CleanResult, clean_rows
from app.db import connect

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema.sql"

RAW_COLUMNS = [
    "transaction_id", "date", "client_id", "client_name", "fund_type", "distributor",
    "stock_code", "stock_name", "transaction_type", "quantity", "price_per_unit",
    "total_value",
]


def read_csv(path: Path) -> list[tuple[int, dict]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = set(RAW_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV is missing columns: {', '.join(sorted(missing))}")
        # Numbering starts at 2 so that source_row matches the line number shown
        # in a text editor or Excel (line 1 is the header).
        rows = [(n, row) for n, row in enumerate(reader, start=2)]
    # Skip fully blank lines, such as a trailing newline at the end of the export.
    return [(n, r) for n, r in rows if any((v or "").strip() for v in r.values())]


def load(conn, rows: list[tuple[int, dict]], result: CleanResult) -> None:
    with conn.transaction():
        conn.execute(SCHEMA_FILE.read_text())

        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO raw_transactions (source_row, {', '.join(RAW_COLUMNS)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(RAW_COLUMNS))})",
                [(n, *[row.get(c) for c in RAW_COLUMNS]) for n, row in rows],
            )

            distributor_ids = {}
            for name in sorted({c["distributor"] for c in result.clients.values()}):
                cur.execute("INSERT INTO distributors (name) VALUES (%s) RETURNING id", (name,))
                distributor_ids[name] = cur.fetchone()["id"]

            cur.executemany(
                "INSERT INTO clients (client_id, client_name, fund_type, distributor_id) "
                "VALUES (%s, %s, %s, %s)",
                [(cid, c["client_name"], c["fund_type"], distributor_ids[c["distributor"]])
                 for cid, c in sorted(result.clients.items())],
            )

            cur.executemany(
                "INSERT INTO securities (stock_code, stock_name) VALUES (%s, %s)",
                sorted(result.securities.items()),
            )

            cur.executemany(
                """
                INSERT INTO transactions (
                    transaction_id, source_row, trade_date, client_id, stock_code,
                    transaction_type, direction, quantity, price_per_unit, total_value,
                    is_excluded, exclusion_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [(t.transaction_id, t.source_row, t.trade_date, t.client_id, t.stock_code,
                  t.transaction_type, t.direction, t.quantity, t.price, t.total_value,
                  t.excluded_reason is not None, t.excluded_reason)
                 for t in result.transactions],
            )

            cur.executemany(
                """
                INSERT INTO data_quality_issues (
                    source_row, transaction_id, field, issue_type, description, action,
                    original_value, new_value
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [(i.source_row, i.transaction_id, i.field, i.issue_type, i.description,
                  i.action, i.original_value, i.new_value) for i in result.issues],
            )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args(argv)

    if not args.csv_path.exists():
        sys.exit(f"File not found: {args.csv_path}")

    rows = read_csv(args.csv_path)
    result = clean_rows(rows)

    with connect() as conn:
        load(conn, rows, result)

    loaded = len(result.transactions)
    excluded = sum(1 for t in result.transactions if t.excluded_reason)
    print(f"Read {len(rows)} rows from {args.csv_path.name}")
    print(f"  {loaded - excluded} clean transactions")
    print(f"  {excluded} loaded but excluded from reports")
    print(f"  {len(rows) - loaded} not loaded (duplicates / unrecoverable)")
    print(f"  {len(result.issues)} data quality issues logged:")
    for (issue_type, action), n in sorted(
        Counter((i.issue_type, i.action) for i in result.issues).items()
    ):
        print(f"    {issue_type:<26} {action:<9} {n}")


if __name__ == "__main__":
    main()
