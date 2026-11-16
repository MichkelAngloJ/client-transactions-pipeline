"""
Answers the business question: "Which distributors had net negative flows
this month, and which of their clients redeemed?"

    python -m app.net_outflows                 # latest month in the data
    python -m app.net_outflows --month 2026-09

The same report is served at GET /reports/net-outflows (JSON, or ?format=text).

"This month" defaults to the most recent month with data. The file ends on
31 October 2026, so a report for November would contain no data, which is
unlikely to be what the business team intends.
"""
import argparse
from datetime import date

from app.db import connect
from app.formatting import inr


def _month_bounds(month: str) -> tuple[date, date]:
    year, mon = (int(x) for x in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return start, end


# Net flows per distributor for one month. When swap_ambiguous is True, the
# dates that had to be guessed (ambiguous_date in the cleaning step) are read
# the other way round, which shows whether the answer depends on that guess.
FLOWS_SQL = """
    WITH dated AS (
        SELECT t.*,
               CASE WHEN %(swap)s AND q.transaction_id IS NOT NULL
                    THEN make_date(extract(year FROM t.trade_date)::int,
                                   extract(day FROM t.trade_date)::int,
                                   extract(month FROM t.trade_date)::int)
                    ELSE t.trade_date
               END AS report_date
        FROM clean_transactions t
        LEFT JOIN data_quality_issues q
               ON q.transaction_id = t.transaction_id AND q.issue_type = 'ambiguous_date'
    )
    SELECT
        d.id   AS distributor_id,
        d.name AS distributor,
        coalesce(sum(t.total_value) FILTER (WHERE t.direction = 1), 0)  AS money_in,
        coalesce(sum(t.total_value) FILTER (WHERE t.direction = -1), 0) AS money_out,
        sum(t.direction * t.total_value)                                AS net_flow
    FROM dated t
    JOIN clients c USING (client_id)
    JOIN distributors d ON d.id = c.distributor_id
    WHERE t.report_date >= %(start)s AND t.report_date < %(end)s
    GROUP BY d.id, d.name
    ORDER BY net_flow
"""


def _monthly_flows(conn, start: date, end: date, swap_ambiguous: bool) -> list[dict]:
    return conn.execute(
        FLOWS_SQL, {"start": start, "end": end, "swap": swap_ambiguous}
    ).fetchall()


def build_report(conn, month: str | None = None) -> dict:
    latest = conn.execute("SELECT max(trade_date) AS d FROM clean_transactions").fetchone()["d"]
    defaulted = month is None
    month = month or latest.strftime("%Y-%m")
    start, end = _month_bounds(month)
    params = {"start": start, "end": end}

    flows = _monthly_flows(conn, start, end, swap_ambiguous=False)

    negative = [f for f in flows if f["net_flow"] < 0]

    redemptions = conn.execute(
        """
        SELECT c.distributor_id, c.client_id, c.client_name, t.transaction_id,
               t.trade_date, t.transaction_type, s.stock_name, t.quantity, t.total_value
        FROM clean_transactions t
        JOIN clients c USING (client_id)
        JOIN securities s USING (stock_code)
        WHERE t.trade_date >= %(start)s AND t.trade_date < %(end)s
          AND t.direction = -1
          AND c.distributor_id = ANY(%(ids)s)
        ORDER BY c.client_name, t.trade_date
        """,
        {**params, "ids": [f["distributor_id"] for f in negative]},
    ).fetchall()

    # Transactions in this month whose date had to be guessed (DD/MM or MM/DD).
    unsure_dates = conn.execute(
        """
        SELECT count(*) AS n
        FROM data_quality_issues q
        JOIN clean_transactions t USING (transaction_id)
        WHERE q.issue_type = 'ambiguous_date'
          AND t.trade_date >= %(start)s AND t.trade_date < %(end)s
        """,
        params,
    ).fetchone()["n"]

    # Check whether the list changes if the guessed dates are read as MM/DD.
    alt_negative = sorted(f["distributor"] for f in
                          _monthly_flows(conn, start, end, swap_ambiguous=True)
                          if f["net_flow"] < 0)
    answer_is_sensitive = alt_negative != sorted(f["distributor"] for f in negative)

    distributors = []
    for f in negative:
        clients: dict[str, dict] = {}
        for r in redemptions:
            if r["distributor_id"] != f["distributor_id"]:
                continue
            c = clients.setdefault(r["client_id"], {
                "client_id": r["client_id"],
                "client_name": r["client_name"],
                "total_redeemed": 0,
                "transactions": [],
            })
            c["total_redeemed"] += r["total_value"]
            c["transactions"].append({
                "transaction_id": r["transaction_id"],
                "date": r["trade_date"],
                "type": r["transaction_type"],
                "stock": r["stock_name"],
                "quantity": r["quantity"],
                "value": r["total_value"],
            })
        distributors.append({
            "distributor": f["distributor"],
            "money_in": f["money_in"],
            "money_out": f["money_out"],
            "net_flow": f["net_flow"],
            "redeeming_clients": sorted(clients.values(),
                                        key=lambda c: c["total_redeemed"], reverse=True),
        })

    label = start.strftime("%B %Y")
    if not flows:
        headline = f"No transactions in {label}."
    elif not negative:
        headline = f"No distributor had net outflows in {label}."
    else:
        headline = (f"{len(negative)} of {len(flows)} distributors active in {label} had more "
                    f"money going out than coming in: "
                    + ", ".join(d["distributor"] for d in distributors) + ".")

    return {
        "month": month,
        "month_label": label,
        "defaulted_to_latest_month": defaulted,
        "data_ends": latest,
        "headline": headline,
        "distributors_with_net_outflows": distributors,
        "all_distributor_flows": [
            {k: f[k] for k in ("distributor", "money_in", "money_out", "net_flow")}
            for f in flows
        ],
        "transactions_with_uncertain_dates": unsure_dates,
        "answer_depends_on_uncertain_dates": answer_is_sensitive,
        "net_outflow_distributors_if_dates_read_mm_dd": alt_negative,
    }


def render_text(report: dict) -> str:
    lines = [f"NET OUTFLOWS - {report['month_label'].upper()}"]
    if report["defaulted_to_latest_month"]:
        lines.append(f"(Latest month in the data - it runs to "
                     f"{report['data_ends']:%d %b %Y})")
    lines += ["", report["headline"], ""]

    for i, d in enumerate(report["distributors_with_net_outflows"], start=1):
        lines += [
            f"{i}. {d['distributor']}",
            f"   Money in:  {inr(d['money_in']):>14}",
            f"   Money out: {inr(d['money_out']):>14}",
            f"   Net flow:  {inr(d['net_flow']):>14}",
            "",
            "   Clients who sold or redeemed:",
        ]
        for c in d["redeeming_clients"]:
            lines.append(f"   - {c['client_name']} ({c['client_id']}): "
                         f"{inr(c['total_redeemed'])} out")
            for t in c["transactions"]:
                lines.append(f"       {t['date']:%d %b}  {t['type']:<10} {t['quantity']:>4} x "
                             f"{t['stock']}  ({inr(t['value'])})")
        lines.append("")

    others = [f for f in report["all_distributor_flows"] if f["net_flow"] >= 0]
    if others:
        lines.append("Distributors with net inflows this month:")
        for f in sorted(others, key=lambda f: f["net_flow"], reverse=True):
            lines.append(f"   {f['distributor']:<32} {inr(f['net_flow']):>14}")
        lines.append("")

    lines.append("Notes")
    lines.append("- 'Money in' is BUY + PURCHASE; 'money out' is SELL + REDEMPTION.")
    if report["transactions_with_uncertain_dates"]:
        lines.append(
            f"- {report['transactions_with_uncertain_dates']} transaction(s) this month had a "
            f"date that could be read as DD/MM or MM/DD. We assumed DD/MM; if that's wrong "
            f"they may belong to a different month.")
    if report["answer_depends_on_uncertain_dates"]:
        alt = ", ".join(report["net_outflow_distributors_if_dates_read_mm_dd"]) or "none"
        lines.append(
            f"- IMPORTANT: this answer depends on the dates we had to guess. Read as MM/DD, the "
            f"distributors with net outflows would be: {alt}. Worth confirming the dates "
            f"with the source team before acting on this.")
    lines.append("- Duplicates and rows we couldn't trust are left out. "
                 "See /data-quality/issues for the full list.")
    return "\n".join(lines)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Distributors with net outflows in a month")
    parser.add_argument("--month", help="YYYY-MM (default: latest month in the data)")
    args = parser.parse_args(argv)
    with connect() as conn:
        print(render_text(build_report(conn, args.month)))


if __name__ == "__main__":
    main()
