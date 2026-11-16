from datetime import date
from decimal import Decimal as D

from app.formatting import inr
from app.portfolio import running_history


def txn(tid, day, code, direction, qty, price):
    return {
        "transaction_id": tid, "trade_date": date(2026, 7, day), "stock_code": code,
        "stock_name": code, "transaction_type": "BUY" if direction == 1 else "SELL",
        "direction": direction, "quantity": qty, "price_per_unit": D(price),
        "total_value": D(price) * qty,
    }


def test_running_value_uses_latest_known_price():
    client = [
        txn("T1", 1, "INFY", 1, 10, "1500"),
        txn("T3", 10, "TCS", 1, 5, "4000"),
    ]
    # Another client traded INFY at 1600 on the 5th.
    prices = [
        (date(2026, 7, 1), "INFY", D("1500")),
        (date(2026, 7, 5), "INFY", D("1600")),
        (date(2026, 7, 10), "TCS", D("4000")),
    ]
    history = running_history(client, prices)
    assert history[0]["running_portfolio_value"] == D("15000.00")
    # 10 INFY at 1600 plus 5 TCS at 4000
    assert history[1]["running_portfolio_value"] == D("36000.00")
    assert history[1]["running_net_invested"] == D("35000.00")


def test_selling_more_than_bought_is_flagged_not_valued_negative():
    client = [txn("T1", 1, "INFY", -1, 30, "1500")]
    history = running_history(client, [(date(2026, 7, 1), "INFY", D("1500"))])
    assert history[0]["units_held_after"] == -30
    assert history[0]["opening_balance_missing"] is True
    assert history[0]["running_portfolio_value"] == D("0.00")


def test_inr_grouping():
    assert inr(D("1234567.4")) == "₹12,34,567"
    assert inr(D("-482582.2")) == "-₹4,82,582"
    assert inr(D("999")) == "₹999"
    assert inr(D("100000")) == "₹1,00,000"
