"""SQL queries used by the API. All reporting reads from the clean_transactions view (see schema.sql)."""
from datetime import date

from app.portfolio import holdings_value, running_history

# Latest price for each stock on or before a cut-off date, used as the current price.
LATEST_PRICES_SQL = """
    SELECT DISTINCT ON (stock_code) stock_code, price_per_unit
    FROM clean_transactions
    WHERE trade_date <= %(as_of)s
    ORDER BY stock_code, trade_date DESC, transaction_id DESC
"""


def data_bounds(conn) -> dict:
    return conn.execute(
        "SELECT min(trade_date) AS first_date, max(trade_date) AS last_date "
        "FROM clean_transactions"
    ).fetchone()


def distributor_summary(conn, start: date | None, end: date | None) -> dict:
    bounds = data_bounds(conn)
    start = start or bounds["first_date"]
    end = end or bounds["last_date"]
    params = {"start": start, "end": end, "as_of": end}

    rows = conn.execute(
        f"""
        WITH period AS (
            SELECT t.*, c.distributor_id
            FROM clean_transactions t
            JOIN clients c USING (client_id)
            WHERE t.trade_date BETWEEN %(start)s AND %(end)s
        ),
        flows AS (
            SELECT
                distributor_id,
                count(DISTINCT client_id)                              AS client_count,
                count(*)                                               AS transaction_count,
                coalesce(sum(total_value) FILTER (WHERE direction = 1), 0)  AS total_invested,
                coalesce(sum(total_value) FILTER (WHERE direction = -1), 0) AS total_redeemed
            FROM period
            GROUP BY distributor_id
        ),
        positions AS (
            -- net units each client gained or lost in each stock over the period
            SELECT distributor_id, client_id, stock_code,
                   sum(direction * quantity) AS net_units
            FROM period
            GROUP BY distributor_id, client_id, stock_code
        ),
        latest_price AS ({LATEST_PRICES_SQL}),
        valuation AS (
            SELECT
                p.distributor_id,
                -- negative positions = shares held from before the file starts;
                -- can't value them without an opening balance, so they count as 0
                coalesce(sum(p.net_units * lp.price_per_unit)
                         FILTER (WHERE p.net_units > 0), 0)        AS current_value,
                count(*) FILTER (WHERE p.net_units < 0)            AS positions_missing_opening_balance
            FROM positions p
            JOIN latest_price lp USING (stock_code)
            GROUP BY p.distributor_id
        )
        SELECT
            d.name AS distributor,
            f.client_count,
            f.transaction_count,
            f.total_invested,
            f.total_redeemed,
            f.total_invested - f.total_redeemed AS net_flows,
            round(v.current_value, 2)           AS current_value,
            v.positions_missing_opening_balance
        FROM flows f
        JOIN distributors d ON d.id = f.distributor_id
        JOIN valuation v USING (distributor_id)
        ORDER BY f.total_invested DESC
        """,
        params,
    ).fetchall()

    return {
        "period": {"start": start, "end": end},
        "definitions": {
            "total_invested": "Sum of BUY and PURCHASE values in the period",
            "total_redeemed": "Sum of SELL and REDEMPTION values in the period",
            "net_flows": "total_invested - total_redeemed",
            "current_value": (
                "Net units bought in the period, valued at the latest traded price in "
                "the data up to the period end. Excludes holdings from before May 2026, "
                "which aren't in the file."
            ),
        },
        "distributors": rows,
    }


def client_history(conn, client_id: str) -> dict | None:
    client = conn.execute(
        """
        SELECT c.client_id, c.client_name, c.fund_type, d.name AS distributor
        FROM clients c JOIN distributors d ON d.id = c.distributor_id
        WHERE c.client_id = %s
        """,
        (client_id,),
    ).fetchone()
    if client is None:
        return None

    txns = conn.execute(
        """
        SELECT t.transaction_id, t.trade_date, t.transaction_type, t.direction,
               t.stock_code, s.stock_name, t.quantity, t.price_per_unit, t.total_value
        FROM clean_transactions t
        JOIN securities s USING (stock_code)
        WHERE t.client_id = %s
        ORDER BY t.trade_date, t.transaction_id
        """,
        (client_id,),
    ).fetchall()

    stocks = sorted({t["stock_code"] for t in txns})
    price_points = [
        (r["trade_date"], r["stock_code"], r["price_per_unit"])
        for r in conn.execute(
            """
            SELECT trade_date, stock_code, price_per_unit
            FROM clean_transactions
            WHERE stock_code = ANY(%s)
            ORDER BY trade_date, transaction_id
            """,
            (stocks,),
        )
    ]
    history = running_history(txns, price_points)

    # The current value applies the latest prices in the file to the final
    # holdings. It can differ from the last running value if prices moved
    # after the client's last trade.
    holdings: dict[str, int] = {}
    for t in txns:
        holdings[t["stock_code"]] = holdings.get(t["stock_code"], 0) + t["direction"] * t["quantity"]
    as_of = data_bounds(conn)["last_date"]
    latest = {r["stock_code"]: r["price_per_unit"]
              for r in conn.execute(LATEST_PRICES_SQL, {"as_of": as_of})}

    excluded = conn.execute(
        """
        SELECT transaction_id, trade_date, transaction_type, stock_code, quantity,
               exclusion_reason
        FROM transactions
        WHERE client_id = %s AND is_excluded
        ORDER BY trade_date
        """,
        (client_id,),
    ).fetchall()

    return {
        "client": client,
        "transaction_count": len(history),
        "current_value": holdings_value(holdings, latest),
        "current_value_as_of": as_of,
        "holdings": {code: units for code, units in sorted(holdings.items()) if units != 0},
        "transactions": history,
        "excluded_transactions": excluded,
    }


def data_quality_report(conn, action: str | None, issue_type: str | None) -> dict:
    filters, params = [], {}
    if action:
        filters.append("action = %(action)s")
        params["action"] = action
    if issue_type:
        filters.append("issue_type = %(issue_type)s")
        params["issue_type"] = issue_type
    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    issues = conn.execute(
        f"""
        SELECT source_row AS row_number, transaction_id, field, issue_type,
               description AS problem, action, original_value, new_value
        FROM data_quality_issues
        {where}
        ORDER BY source_row, id
        """,
        params,
    ).fetchall()

    by_type = conn.execute(
        f"""
        SELECT issue_type, action, count(*) AS count
        FROM data_quality_issues
        {where}
        GROUP BY issue_type, action
        ORDER BY count(*) DESC, issue_type
        """,
        params,
    ).fetchall()

    totals = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM raw_transactions)                    AS rows_in_file,
            (SELECT count(*) FROM clean_transactions)                  AS clean_transactions,
            (SELECT count(*) FROM transactions WHERE is_excluded)      AS excluded_but_loaded,
            (SELECT count(*) FROM raw_transactions r
              WHERE NOT EXISTS (SELECT 1 FROM transactions t
                                WHERE t.source_row = r.source_row))   AS not_loaded,
            (SELECT count(DISTINCT source_row) FROM data_quality_issues) AS rows_with_issues
        """
    ).fetchone()

    return {"totals": totals, "summary": by_type, "issue_count": len(issues), "issues": issues}
