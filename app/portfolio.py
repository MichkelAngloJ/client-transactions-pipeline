"""
Running portfolio value for a single client.

The exercise provides no market price feed, so the price of a stock on a
given day is the last price at which it traded in the file, on or before
that day. This is the best signal available in the data. A production
system would read from an end-of-day price table instead.
"""
from collections import defaultdict
from decimal import Decimal

CENT = Decimal("0.01")


def holdings_value(holdings: dict[str, int], prices: dict[str, Decimal]) -> Decimal:
    # Negative units mean the client sold shares bought before May. The opening
    # balance is not in the file, so the position cannot be valued and is
    # counted as zero rather than as a negative holding.
    total = sum((units * prices[code] for code, units in holdings.items() if units > 0),
                Decimal(0))
    return total.quantize(CENT)


def running_history(txns: list[dict], price_points: list[tuple]) -> list[dict]:
    """
    Build the running history for one client.

    txns:         the client's clean transactions, ordered by trade_date and ID
    price_points: (trade_date, stock_code, price) for every clean transaction in
                  the stocks this client traded, ordered by trade_date

    Returns one dict per transaction with the holdings and portfolio value
    immediately after it.
    """
    holdings: dict[str, int] = defaultdict(int)
    last_price: dict[str, Decimal] = {}
    net_invested = Decimal(0)
    i = 0
    history = []

    for t in txns:
        # Bring every stock's price up to the date of this trade.
        while i < len(price_points) and price_points[i][0] <= t["trade_date"]:
            _, code, price = price_points[i]
            last_price[code] = price
            i += 1
        # For the traded stock, the client's own fill is the most accurate price.
        last_price[t["stock_code"]] = t["price_per_unit"]

        holdings[t["stock_code"]] += t["direction"] * t["quantity"]
        net_invested += t["direction"] * t["total_value"]
        units_after = holdings[t["stock_code"]]

        history.append({
            "transaction_id": t["transaction_id"],
            "trade_date": t["trade_date"],
            "transaction_type": t["transaction_type"],
            "stock_code": t["stock_code"],
            "stock_name": t["stock_name"],
            "quantity": t["quantity"],
            "price_per_unit": t["price_per_unit"],
            "total_value": t["total_value"],
            "units_held_after": units_after,
            "opening_balance_missing": units_after < 0,
            "running_net_invested": net_invested.quantize(CENT),
            "running_portfolio_value": holdings_value(holdings, last_price),
        })

    return history
