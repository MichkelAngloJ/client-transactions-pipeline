from datetime import date
from decimal import Decimal

import pytest

from app.cleaning import clean_rows, parse_trade_date


def row(**overrides):
    base = {
        "transaction_id": "TXN-1",
        "date": "2026-07-01",
        "client_id": "PMS-001",
        "client_name": "Asha Rao",
        "fund_type": "PMS",
        "distributor": "Sanctum Wealth",
        "stock_code": "INFY",
        "stock_name": "Infosys Ltd",
        "transaction_type": "BUY",
        "quantity": "10",
        "price_per_unit": "1500.00",
        "total_value": "15000.00",
    }
    base.update(overrides)
    return base


def clean(*rows):
    return clean_rows([(n, r) for n, r in enumerate(rows, start=2)])


def issue_types(result):
    return sorted(i.issue_type for i in result.issues)


# --- Dates

@pytest.mark.parametrize("raw, expected, status", [
    ("2026-07-23", date(2026, 7, 23), "ok"),
    ("25-07-2026", date(2026, 7, 25), "ok"),
    ("21/05/2026", date(2026, 5, 21), "ok"),           # day above 12, so DD/MM
    ("09/21/2026", date(2026, 9, 21), "ok"),           # second part above 12, so MM/DD
    ("03/08/2026", date(2026, 8, 3), "by_period"),     # 8 March is outside May-Oct
    ("10/12/2026", date(2026, 10, 12), "by_period"),   # 10 December is outside the period
    ("05/07/2026", date(2026, 7, 5), "ambiguous"),     # 5 July or 7 May, both in the period
    ("06/06/2026", date(2026, 6, 6), "ok"),            # identical under both readings
    ("31/02/2026", None, "bad"),
    ("yesterday", None, "bad"),
])
def test_parse_trade_date(raw, expected, status):
    parsed, got_status, _ = parse_trade_date(raw)
    assert parsed == expected
    assert got_status == status


def test_date_outside_period_is_excluded():
    result = clean(row(date="2027-02-15"))
    assert result.transactions[0].excluded_reason
    assert "date_out_of_period" in issue_types(result)


# --- Duplicates

def test_exact_duplicate_id_keeps_one_copy():
    result = clean(row(), row())
    assert len(result.transactions) == 1
    assert issue_types(result) == ["duplicate_transaction_id"]


def test_duplicate_id_keeps_the_complete_copy():
    result = clean(row(client_id=""), row())
    assert len(result.transactions) == 1
    assert result.transactions[0].source_row == 3
    assert result.transactions[0].client_id == "PMS-001"


def test_same_trade_under_new_id_is_excluded_not_deleted():
    result = clean(row(transaction_id="TXN-1"), row(transaction_id="TXN-9"))
    assert len(result.transactions) == 2
    kept, copy = sorted(result.transactions, key=lambda t: t.transaction_id)
    assert kept.excluded_reason is None
    assert "TXN-1" in copy.excluded_reason


# --- Client IDs

@pytest.mark.parametrize("blank", ["", "NULL", "N/A", "  "])
def test_missing_client_id_recovered_from_other_rows(blank):
    result = clean(
        row(transaction_id="TXN-1"),
        row(transaction_id="TXN-2", client_id=blank, date="2026-08-01"),
    )
    assert [t.client_id for t in result.transactions] == ["PMS-001", "PMS-001"]


def test_missing_client_id_not_guessed_when_name_is_shared():
    # Same name, fund and distributor under two different IDs, so no guess is made.
    result = clean(
        row(transaction_id="TXN-1", client_id="PMS-001"),
        row(transaction_id="TXN-2", client_id="PMS-002", date="2026-07-02"),
        row(transaction_id="TXN-3", client_id="", date="2026-07-03"),
    )
    assert {t.transaction_id for t in result.transactions} == {"TXN-1", "TXN-2"}


def test_distributor_is_part_of_client_match():
    result = clean(
        row(transaction_id="TXN-1"),
        row(transaction_id="TXN-2", client_id="", distributor="Centrum Wealth Management"),
    )
    assert [t.transaction_id for t in result.transactions] == ["TXN-1"]


# --- Quantities and totals

def test_negative_quantity_on_sell_is_sign_convention():
    result = clean(row(transaction_type="SELL", quantity="-10", total_value="-15000"))
    t = result.transactions[0]
    assert (t.quantity, t.total_value, t.excluded_reason) == (10, Decimal("15000.00"), None)
    assert issue_types(result) == ["negative_quantity"]


def test_negative_quantity_on_buy_is_excluded():
    result = clean(row(quantity="-10", total_value="-15000"))
    assert result.transactions[0].excluded_reason
    assert issue_types(result) == ["negative_quantity"]


def test_wrong_total_is_recalculated():
    result = clean(row(total_value="99999.99"))
    assert result.transactions[0].total_value == Decimal("15000.00")
    assert issue_types(result) == ["total_mismatch"]


# --- Names and securities

def test_whitespace_and_distributor_alias():
    result = clean(row(client_name="  Asha Rao  ", distributor="A. Rathi Wealth  "))
    t = result.transactions[0]
    assert (t.client_name, t.distributor) == ("Asha Rao", "Anand Rathi Wealth")
    assert issue_types(result) == ["distributor_alias", "whitespace"]


def test_code_name_and_price_disagree_is_excluded():
    normal = [row(transaction_id=f"TXN-{i}", date=f"2026-07-{i:02d}") for i in range(1, 6)]
    odd = row(transaction_id="TXN-99", stock_name="HDFC Bank Ltd",
              price_per_unit="6700.00", total_value="67000.00")
    result = clean(*normal, odd)
    bad = next(t for t in result.transactions if t.transaction_id == "TXN-99")
    assert bad.excluded_reason
    assert "security_mismatch" in issue_types(result)


def test_wrong_name_with_plausible_price_keeps_the_code():
    normal = [row(transaction_id=f"TXN-{i}", date=f"2026-07-{i:02d}") for i in range(1, 6)]
    result = clean(*normal, row(transaction_id="TXN-99", stock_name="Wipro Ltd", date="2026-07-20"))
    fixed = next(t for t in result.transactions if t.transaction_id == "TXN-99")
    assert fixed.excluded_reason is None
    assert fixed.stock_name == "Infosys Ltd"
