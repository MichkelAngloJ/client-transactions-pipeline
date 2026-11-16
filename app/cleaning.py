"""
Cleaning rules for the client transactions export.

This module contains no database code. Every function takes plain
dictionaries and returns plain objects, so the rules can be tested without
a running Postgres instance. The README explains the reasoning behind each
rule; the comments below summarise it.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median

# The export covers May to October 2026. The period is used to resolve dates
# that can be read in two ways and to identify rows outside the file's range.
PERIOD_START = date(2026, 5, 1)
PERIOD_END = date(2026, 10, 31)

MISSING_MARKERS = {"", "NULL", "N/A", "NA", "NONE", "-"}

# Both spellings refer to the same firm. No client appears under both names,
# which suggests two source systems rather than two distributors. The README
# lists this as a point to confirm with the business.
DISTRIBUTOR_ALIASES = {
    "A. Rathi Wealth": "Anand Rathi Wealth",
}

INFLOW_TYPES = {"BUY", "PURCHASE"}
OUTFLOW_TYPES = {"SELL", "REDEMPTION"}

CLIENT_ID_RE = re.compile(r"^(PMS|AIF)-\d{3}$")

# Every security in the file is a large cap. A price more than 35% away from
# the stock's six-month median is treated as a data error, not a market move.
PRICE_TOLERANCE = Decimal("0.35")

CENT = Decimal("0.01")


@dataclass
class Issue:
    source_row: int
    transaction_id: str | None
    field: str
    issue_type: str
    description: str
    action: str  # one of: fixed, excluded, flagged
    original_value: str | None = None
    new_value: str | None = None


@dataclass
class Txn:
    source_row: int
    transaction_id: str
    trade_date: date | None
    client_id: str | None
    client_name: str
    fund_type: str
    distributor: str
    stock_code: str
    stock_name: str
    transaction_type: str
    quantity: int | None
    price: Decimal | None
    total_value: Decimal | None
    excluded_reason: str | None = None
    # A dropped row is not loaded into `transactions` and remains only in the
    # raw table. This applies when there is no reliable row to load, for
    # example a second copy of an existing transaction_id.
    dropped: bool = False

    @property
    def direction(self) -> int:
        return 1 if self.transaction_type in INFLOW_TYPES else -1

    @property
    def is_active(self) -> bool:
        return not self.dropped and self.excluded_reason is None


@dataclass
class CleanResult:
    transactions: list[Txn]
    issues: list[Issue]
    clients: dict[str, dict]       # client_id -> name, fund_type, distributor
    securities: dict[str, str]     # stock_code -> stock_name


# ---------------------------------------------------------------- helpers

def _text(value: str | None) -> str:
    # Strip the value and collapse repeated internal spaces ("Sanctum  Wealth").
    return " ".join((value or "").split())


def _decimal(value: str | None) -> Decimal | None:
    try:
        return Decimal(_text(value))
    except InvalidOperation:
        return None


def _in_period(d: date) -> bool:
    return PERIOD_START <= d <= PERIOD_END


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_trade_date(raw: str) -> tuple[date | None, str, date | None]:
    """
    Parse a trade date and return (date, status, other_reading).

    Status values:
      ok         the value has only one valid reading
      by_period  two readings exist and only one falls within May-Oct 2026
      ambiguous  two readings exist and both fall within the period; DD/MM is used
      bad        the value could not be parsed

    The file contains four formats: 2026-07-23, 25-07-2026, 21/05/2026 and
    09/21/2026. Dash-separated DD-MM-YYYY dates are always day-first (23 of
    them have a day above 12 and none has a month above 12), so only
    slash-separated dates require a tie-break.
    """
    s = _text(raw)

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        d = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        return (d, "ok", None) if d else (None, "bad", None)

    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", s)
    if m:
        d = _safe_date(int(m[3]), int(m[2]), int(m[1]))
        return (d, "ok", None) if d else (None, "bad", None)

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return None, "bad", None

    first, second, year = int(m[1]), int(m[2]), int(m[3])
    day_first = _safe_date(year, second, first)    # DD/MM/YYYY
    month_first = _safe_date(year, first, second)  # MM/DD/YYYY

    if day_first and month_first and day_first != month_first:
        in_period = [d for d in (day_first, month_first) if _in_period(d)]
        if len(in_period) == 1:
            other = month_first if in_period[0] == day_first else day_first
            return in_period[0], "by_period", other
        # Both readings fall within the period, or neither does. DD/MM is used
        # because it is the Indian convention and the majority format among
        # slash dates that can be distinguished (50 day-first, 24 month-first).
        return day_first, "ambiguous", month_first

    d = day_first or month_first
    return (d, "ok", None) if d else (None, "bad", None)


# ---------------------------------------------------------------- pass 1: row by row

def _parse_row(source_row: int, raw: dict, issues: list[Issue]) -> Txn:
    tid = _text(raw.get("transaction_id"))

    def log(field_name, issue_type, description, action, old=None, new=None):
        issues.append(Issue(source_row, tid or None, field_name, issue_type,
                            description, action, old, new))

    exclude_reasons: list[str] = []

    padded = [k for k, v in raw.items() if v and v != v.strip()]
    if padded:
        log(", ".join(padded), "whitespace", "Leading/trailing spaces trimmed", "fixed")

    fund_type = _text(raw.get("fund_type")).upper()
    transaction_type = _text(raw.get("transaction_type")).upper()
    stock_code = _text(raw.get("stock_code")).upper()

    # --- Client ID: blank values and placeholders are resolved in pass 2
    client_id = _text(raw.get("client_id")).upper()
    if client_id in MISSING_MARKERS:
        client_id = None
    elif not CLIENT_ID_RE.match(client_id):
        log("client_id", "invalid_client_id",
            f"'{raw.get('client_id')}' doesn't look like a client id", "flagged",
            raw.get("client_id"))
        client_id = None
    elif client_id[:3] != fund_type:
        log("client_id", "fund_type_mismatch",
            f"{client_id} is filed under fund_type {fund_type}", "flagged")

    # --- Distributor name
    distributor = _text(raw.get("distributor"))
    if distributor in DISTRIBUTOR_ALIASES:
        canonical = DISTRIBUTOR_ALIASES[distributor]
        log("distributor", "distributor_alias",
            f"'{distributor}' merged into '{canonical}'", "fixed", distributor, canonical)
        distributor = canonical

    # --- Trade date
    trade_date, status, other = parse_trade_date(raw.get("date", ""))
    if status == "bad":
        exclude_reasons.append("unreadable date")
        log("date", "unparseable_date", "Date couldn't be parsed", "excluded", raw.get("date"))
    elif status == "by_period":
        log("date", "date_resolved_by_period",
            f"Could be {trade_date} or {other}; only {trade_date} falls in May-Oct 2026",
            "fixed", raw.get("date"), trade_date.isoformat())
    elif status == "ambiguous":
        log("date", "ambiguous_date",
            f"Could be {trade_date} (DD/MM) or {other} (MM/DD); both are in the period. "
            f"Took DD/MM - needs confirming with the source",
            "flagged", raw.get("date"), trade_date.isoformat())

    if trade_date and not _in_period(trade_date):
        exclude_reasons.append("date outside May-Oct 2026")
        log("date", "date_out_of_period",
            f"{trade_date} is outside the period this file covers", "excluded",
            raw.get("date"))

    # --- Transaction type
    if transaction_type not in INFLOW_TYPES | OUTFLOW_TYPES:
        exclude_reasons.append("unknown transaction type")
        log("transaction_type", "unknown_transaction_type",
            f"'{transaction_type}' isn't a type we recognise", "excluded", transaction_type)

    # --- Quantity, price and total value
    qty_raw = _text(raw.get("quantity"))
    try:
        quantity = int(Decimal(qty_raw))
    except (InvalidOperation, ValueError):
        quantity = None

    price = _decimal(raw.get("price_per_unit"))
    total = _decimal(raw.get("total_value"))
    negative_qty = False

    if quantity is None or quantity == 0:
        exclude_reasons.append("missing or zero quantity")
        log("quantity", "invalid_quantity", "Quantity missing or zero", "excluded", qty_raw)
        quantity = None
    elif quantity < 0:
        negative_qty = True
        if transaction_type in OUTFLOW_TYPES:
            # A negative quantity on a sell is a sign convention; the
            # transaction type already indicates the direction of the money.
            quantity = abs(quantity)
            log("quantity", "negative_quantity",
                f"Negative quantity on a {transaction_type}; treated as a sign convention",
                "fixed", qty_raw, str(quantity))
        else:
            # A negative quantity on a buy is contradictory. It may be a
            # reversal or a typing error, and either assumption would change
            # the client's holdings.
            exclude_reasons.append("negative quantity on a buy")
            log("quantity", "negative_quantity",
                f"Negative quantity on a {transaction_type} - can't tell if it's a "
                f"reversal or a typo", "excluded", qty_raw)
            quantity = abs(quantity)

    if price is None or price <= 0:
        exclude_reasons.append("missing or non-positive price")
        log("price_per_unit", "invalid_price", "Price missing or not positive", "excluded",
            raw.get("price_per_unit"))
        price = None

    if quantity and price:
        expected = (quantity * price).quantize(CENT)
        # On these rows the total carries the same negative sign as the quantity.
        if negative_qty and total is not None:
            total = abs(total)
        if total is None or abs(total - expected) > CENT:
            # The price is within the stock's normal range and quantities are
            # always standard lot sizes, so the total is the value in error.
            log("total_value", "total_mismatch",
                f"total_value doesn't equal quantity x price ({quantity} x {price} = "
                f"{expected}); recalculated", "fixed", raw.get("total_value"), str(expected))
            total = expected

    if not tid:
        log("transaction_id", "missing_transaction_id",
            "No transaction_id - can't load or deduplicate it", "excluded")

    txn = Txn(
        source_row=source_row,
        transaction_id=tid,
        trade_date=trade_date,
        client_id=client_id,
        client_name=_text(raw.get("client_name")),
        fund_type=fund_type,
        distributor=distributor,
        stock_code=stock_code,
        stock_name=_text(raw.get("stock_name")),
        transaction_type=transaction_type,
        quantity=quantity,
        price=price,
        total_value=total,
        excluded_reason="; ".join(exclude_reasons) or None,
        dropped=not tid,
    )
    # A row without a usable date, price or quantity cannot be used downstream.
    if txn.trade_date is None or txn.price is None or txn.quantity is None:
        txn.dropped = True
    return txn


# ---------------------------------------------------------------- pass 2: across rows

def _completeness(t: Txn) -> int:
    return sum(v is not None for v in (t.client_id, t.trade_date, t.quantity, t.price))


def _dedupe_by_id(txns: list[Txn], issues: list[Issue]) -> None:
    by_id: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.transaction_id:
            by_id[t.transaction_id].append(t)

    for tid, copies in by_id.items():
        if len(copies) < 2:
            continue
        # Keep the most complete copy. On a tie, max() returns the first one.
        keeper = max(copies, key=_completeness)
        for t in copies:
            if t is keeper:
                continue
            conflicts = [
                f for f in ("trade_date", "client_id", "stock_code", "transaction_type",
                            "quantity", "price")
                if getattr(t, f) is not None and getattr(keeper, f) is not None
                and getattr(t, f) != getattr(keeper, f)
            ]
            if conflicts:
                desc = (f"transaction_id also on row {keeper.source_row} with different "
                        f"{', '.join(conflicts)}; kept row {keeper.source_row}, needs review")
            elif _completeness(t) < _completeness(keeper):
                desc = (f"Duplicate of row {keeper.source_row}, which has the missing "
                        f"fields filled in; kept that one")
            else:
                desc = f"Exact duplicate of row {keeper.source_row}"
            t.dropped = True
            issues.append(Issue(t.source_row, tid, "transaction_id", "duplicate_transaction_id",
                                desc, "excluded"))


def _fill_client_ids(txns: list[Txn], issues: list[Issue]) -> None:
    # Names are not unique (the file has two different clients named Sanjay
    # Desai and two named Lakshmi Rao), so the match uses name, fund type and
    # distributor together.
    lookup: dict[tuple, set[str]] = defaultdict(set)
    for t in txns:
        if not t.dropped and t.client_id:
            lookup[(t.client_name.lower(), t.fund_type, t.distributor)].add(t.client_id)

    for t in txns:
        if t.dropped or t.client_id:
            continue
        candidates = lookup.get((t.client_name.lower(), t.fund_type, t.distributor), set())
        if len(candidates) == 1:
            t.client_id = next(iter(candidates))
            issues.append(Issue(
                t.source_row, t.transaction_id, "client_id", "missing_client_id",
                f"client_id was blank; matched to {t.client_id} on name, fund type and "
                f"distributor", "fixed", None, t.client_id))
        else:
            why = ("more than one client matches" if candidates
                   else "no other row for this client to match against")
            t.dropped = True
            issues.append(Issue(
                t.source_row, t.transaction_id, "client_id", "missing_client_id",
                f"client_id was blank and couldn't be recovered ({why})", "excluded"))


def _check_securities(txns: list[Txn], issues: list[Issue]) -> dict[str, str]:
    live = [t for t in txns if not t.dropped]

    names: dict[str, Counter] = defaultdict(Counter)
    prices: dict[str, list[Decimal]] = defaultdict(list)
    for t in live:
        names[t.stock_code][t.stock_name] += 1
        prices[t.stock_code].append(t.price)

    canonical = {code: c.most_common(1)[0][0] for code, c in names.items()}
    medians = {code: median(p) for code, p in prices.items()}

    for t in live:
        expected_name = canonical[t.stock_code]
        name_wrong = t.stock_name != expected_name
        drift = abs(t.price - medians[t.stock_code]) / medians[t.stock_code]
        price_off = drift > PRICE_TOLERANCE

        if name_wrong and price_off:
            # The code and the name disagree and the price matches neither,
            # so the actual security cannot be determined.
            reason = "stock code, name and price don't agree"
            t.excluded_reason = "; ".join(filter(None, [t.excluded_reason, reason]))
            issues.append(Issue(
                t.source_row, t.transaction_id, "stock_code", "security_mismatch",
                f"stock_code {t.stock_code} but stock_name '{t.stock_name}', and price "
                f"{t.price} is {drift:.0%} away from {t.stock_code}'s median "
                f"{medians[t.stock_code]:.2f}", "excluded", t.stock_name))
        elif name_wrong:
            issues.append(Issue(
                t.source_row, t.transaction_id, "stock_name", "security_mismatch",
                f"Name '{t.stock_name}' doesn't match code {t.stock_code}; price fits the "
                f"code, so kept the code", "fixed", t.stock_name, expected_name))
            t.stock_name = expected_name
        elif price_off:
            reason = "price far outside the stock's range"
            t.excluded_reason = "; ".join(filter(None, [t.excluded_reason, reason]))
            issues.append(Issue(
                t.source_row, t.transaction_id, "price_per_unit", "price_outlier",
                f"{t.price} is {drift:.0%} away from {t.stock_code}'s median", "excluded"))

    return canonical


def _dedupe_by_content(txns: list[Txn], issues: list[Issue]) -> None:
    # Rows with the same client, date, stock, side, quantity and price (to the
    # paisa) but a different transaction_id. Two genuine orders filling at the
    # same price on the same day are possible but uncommon; a resent record is
    # far more likely. These rows are excluded rather than deleted, so the
    # decision is easy to reverse.
    groups: dict[tuple, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.is_active:
            key = (t.client_id, t.trade_date, t.stock_code, t.direction, t.quantity, t.price)
            groups[key].append(t)

    for copies in groups.values():
        if len(copies) < 2:
            continue
        copies.sort(key=lambda t: t.transaction_id)
        keeper = copies[0]
        for t in copies[1:]:
            t.excluded_reason = f"suspected double-post of {keeper.transaction_id}"
            issues.append(Issue(
                t.source_row, t.transaction_id, "transaction_id", "suspected_duplicate",
                f"Same client, date, stock, side, quantity and price as "
                f"{keeper.transaction_id} (row {keeper.source_row}) under a new id",
                "excluded"))


def _build_clients(txns: list[Txn], issues: list[Issue]) -> dict[str, dict]:
    clients: dict[str, dict] = {}
    for t in txns:
        if t.dropped:
            continue
        info = {"client_name": t.client_name, "fund_type": t.fund_type,
                "distributor": t.distributor}
        existing = clients.setdefault(t.client_id, info)
        if existing != info:
            issues.append(Issue(
                t.source_row, t.transaction_id, "client_id", "client_conflict",
                f"{t.client_id} appears elsewhere with different details "
                f"({existing}); kept the first", "flagged"))
    return clients


# ---------------------------------------------------------------- entry point

def clean_rows(rows: list[tuple[int, dict]]) -> CleanResult:
    """Clean the export. `rows` holds (CSV line number, row dict) pairs from csv.DictReader."""
    issues: list[Issue] = []
    txns = [_parse_row(n, raw, issues) for n, raw in rows]

    # The order of these steps matters. Repeated IDs are removed before client
    # IDs are filled, because one of the repeats is the copy with the blank ID.
    # Client IDs are filled before content duplicates are checked, because the
    # duplicate key includes the client.
    _dedupe_by_id(txns, issues)
    _fill_client_ids(txns, issues)
    securities = _check_securities(txns, issues)
    _dedupe_by_content(txns, issues)
    clients = _build_clients(txns, issues)

    issues.sort(key=lambda i: i.source_row)
    return CleanResult(
        transactions=[t for t in txns if not t.dropped],
        issues=issues,
        clients=clients,
        securities=securities,
    )
