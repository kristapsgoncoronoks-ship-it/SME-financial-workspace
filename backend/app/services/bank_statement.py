"""Bank-statement transaction reader for expense imports.

Built on the SAME extraction engine as the invoice transaction reader
(`pdf_ocr`): text-layer-first with an OCR fallback, date masking, row
reconstruction and locale-aware money parsing. It is a *separate* parser because
a statement's rows differ from an invoice's — most importantly the trailing
**running-balance** column, which must not be mistaken for the transaction
amount. We disambiguate with a balance-delta check: on a genuine balance column,
`balance[i] - balance[i-1] ≈ ±amount[i]`, and the sign tells debit from credit.

Output is a *draft* set of expense items (the debits/outflows) that the employee
reviews before saving — the same advisory, human-confirmed pattern as invoices.
CSV statements (the common bank export) are parsed directly.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, DecimalException

from app.services import pdf_ocr

_CENTS = Decimal("0.01")
_BALANCE_TOL = Decimal("0.02")


@dataclass
class Txn:
    date: date
    description: str
    amount: Decimal  # positive magnitude
    direction: str  # "debit" (money out) | "credit" (money in)
    balance: Decimal | None = None
    # The currency the BANK stated for this entry, when it stated one (camt
    # carries `Amt/@Ccy`). None means the source did not say — a CSV or PDF
    # statement usually does not — and None is never filled in by guessing the
    # organisation's currency: an amount whose unit is assumed is how a USD
    # receipt silently settles a EUR invoice.
    currency: str | None = None


@dataclass
class StatementResult:
    method: str  # text-layer | ocr | csv
    transactions: list[Txn]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# PDF / OCR path (reuses pdf_ocr primitives)
# --------------------------------------------------------------------------- #
def _rows_from_text(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        date_str, rest = pdf_ocr._split_leading_date(line)
        if not date_str:
            continue  # transaction rows start with a date
        d = pdf_ocr._to_date(date_str)
        if d is None:
            continue
        masked = pdf_ocr._mask(rest)
        matches = list(pdf_ocr._MONEY_RE.finditer(masked))
        if not matches:
            continue
        desc = rest[: matches[0].start()].strip(" .\t-|:*")
        amounts = [pdf_ocr._num(m.group()) for m in matches]
        rows.append({"date": d, "description": desc or "Transaction", "amounts": amounts})
    return rows


def _looks_like_running_balance(rows: list[dict]) -> bool:
    """True if the LAST money token behaves like a running balance across rows."""
    matched = total = 0
    prev = None
    for r in rows:
        amts = r["amounts"]
        if len(amts) < 2:
            prev = None
            continue
        cur_balance = amts[-1]
        if prev is not None:
            total += 1
            if abs(abs(cur_balance - prev) - amts[-2]) <= _BALANCE_TOL:
                matched += 1
        prev = cur_balance
    return total > 0 and matched / total >= 0.6


def _transactions_from_rows(rows: list[dict], warnings: list[str]) -> list[Txn]:
    has_balance = _looks_like_running_balance(rows)
    txns: list[Txn] = []
    prev_balance: Decimal | None = None

    for r in rows:
        amts = r["amounts"]
        if has_balance and len(amts) >= 2:
            balance = amts[-1]
            amount = amts[-2]
            if prev_balance is not None:
                direction = "debit" if balance < prev_balance else "credit"
            else:
                direction = "debit"
            prev_balance = balance
        else:
            balance = None
            amount = amts[-1]
            # No running-balance column and no reliable sign → default to an
            # outflow, so the row still appears in the reviewable 'available
            # expenses' inbox (only debits are expensable). The user confirms or
            # discards. Sign-based guessing would be worse here: unsigned positive
            # amounts (the norm on such statements) would look like credits and
            # silently vanish from the inbox.
            direction = "debit"
        txns.append(
            Txn(
                date=r["date"],
                description=r["description"][:300],
                amount=abs(amount).quantize(_CENTS),
                direction=direction,
                balance=balance,
            )
        )

    if not has_balance:
        warnings.append(
            "No running-balance column detected — amounts taken as-is; verify debit/credit."
        )
    return txns


# --------------------------------------------------------------------------- #
# CSV path (common bank export)
# --------------------------------------------------------------------------- #
_DATE_COLS = ("date", "booking date", "value date", "transaction date", "posted")
_DESC_COLS = ("description", "details", "payee", "reference", "narrative", "memo", "counterparty")
_AMOUNT_COLS = ("amount", "value")
_DEBIT_COLS = ("debit", "withdrawal", "paid out", "money out", "out")
_CREDIT_COLS = ("credit", "deposit", "paid in", "money in", "in")
_CURRENCY_COLS = ("currency", "ccy", "transaction currency")
_CSV_ROW_WARNING_LIMIT = 20
# BankLine.amount and ExpenseTransaction.amount both use Numeric(14, 2).
_CSV_MAX_AMOUNT = Decimal("999999999999.99")


def _pick(fieldmap: dict, names) -> str | None:
    for n in names:
        if n in fieldmap:
            return fieldmap[n]
    return None


def _parse_csv(content: bytes, warnings: list[str]) -> list[Txn]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    fmap = {(f or "").strip().lower(): f for f in reader.fieldnames}
    date_c = _pick(fmap, _DATE_COLS)
    desc_c = _pick(fmap, _DESC_COLS)
    amount_c = _pick(fmap, _AMOUNT_COLS)
    debit_c = _pick(fmap, _DEBIT_COLS)
    credit_c = _pick(fmap, _CREDIT_COLS)
    currency_c = _pick(fmap, _CURRENCY_COLS)
    if not date_c or (not amount_c and not debit_c and not credit_c):
        raise ValueError("CSV must have a date column and an amount (or debit/credit) column")

    skipped_dates = 0
    skipped_amounts = 0
    row_warnings: list[str] = []

    def warn_skip(row_number: int, reason: str) -> None:
        if len(row_warnings) < _CSV_ROW_WARNING_LIMIT:
            row_warnings.append(f"CSV row {row_number} skipped: {reason}.")

    def invalid_amount(row_number: int) -> ValueError:
        return ValueError(
            f"Invalid amount on CSV row {row_number}; use a finite value within the supported range."
        )

    txns: list[Txn] = []
    for row_number, row in enumerate(reader, start=2):
        d = pdf_ocr._to_date((row.get(date_c) or "").strip())
        if d is None:
            skipped_dates += 1
            warn_skip(row_number, "invalid or missing date")
            continue
        desc = (row.get(desc_c) or "").strip() if desc_c else "Transaction"
        try:
            if amount_c:
                amt = pdf_ocr._num(row.get(amount_c) or "0")
                if not amt.is_finite():
                    raise invalid_amount(row_number)
                direction = "credit" if amt > 0 else "debit"
                amount = abs(amt)
            else:
                debit = pdf_ocr._num(row.get(debit_c) or "0") if debit_c else Decimal("0")
                credit = pdf_ocr._num(row.get(credit_c) or "0") if credit_c else Decimal("0")
                if not debit.is_finite() or not credit.is_finite():
                    raise invalid_amount(row_number)
                if debit:
                    direction, amount = "debit", abs(debit)
                elif credit:
                    direction, amount = "credit", abs(credit)
                else:
                    skipped_amounts += 1
                    warn_skip(row_number, "no nonzero amount was recognized")
                    continue
            if not amount.is_finite():
                raise invalid_amount(row_number)
        except DecimalException:
            raise invalid_amount(row_number) from None
        if amount == 0:
            skipped_amounts += 1
            warn_skip(row_number, "no nonzero amount was recognized")
            continue
        currency = (row.get(currency_c) or "").strip().upper() if currency_c else ""
        if currency and re.fullmatch(r"[A-Z]{3}", currency) is None:
            # Refuse the whole import rather than treating a stated invalid unit
            # as unknown (which the legacy matcher permits). Do not echo source data.
            raise ValueError(f"Invalid currency code on CSV row {row_number}; use three letters.")
        try:
            rounded_amount = amount.quantize(_CENTS)
        except DecimalException:
            raise invalid_amount(row_number) from None
        if not rounded_amount.is_finite() or rounded_amount > _CSV_MAX_AMOUNT:
            raise invalid_amount(row_number)
        txns.append(
            Txn(
                date=d,
                description=(desc or "Transaction")[:300],
                amount=rounded_amount,
                direction=direction,
                currency=currency or None,
            )
        )
    skipped = skipped_dates + skipped_amounts
    if skipped:
        summary = (
            f"Skipped {skipped} CSV {'row' if skipped == 1 else 'rows'}: "
            f"{skipped_dates} with an invalid or missing date; "
            f"{skipped_amounts} with no nonzero recognized amount."
        )
        omitted = skipped - len(row_warnings)
        if omitted:
            summary += f" Additional skipped rows not listed: {omitted}."
        warnings.append(summary)
        warnings.extend(row_warnings)
    return txns


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# camt.053 path (ISO 20022 bank-to-customer statement, the modern bank export)
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    """The local element name, dropping any XML namespace."""
    return tag.rsplit("}", 1)[-1]


def _looks_like_camt(content: bytes) -> bool:
    head = content[:2000].lower()
    return b"camt.053" in head or b"bktocstmrstmt" in head


def fsum_eq(priced, total: Decimal) -> bool:
    """Do the priced details account for the whole entry, exactly?

    Exactly, not approximately: a tolerance here would let a batch whose parts
    do not add up be silently rewritten into parts that do.
    """
    return sum((a for _, (a, _c) in priced), Decimal("0")) == total


def _parse_camt053(content: bytes, warnings: list[str]) -> list[Txn]:
    from defusedxml import ElementTree as DET  # XXE-hardened parse

    try:
        root = DET.fromstring(content)
    except Exception as exc:
        raise ValueError(f"Could not parse camt.053 XML: {exc}") from exc

    def child(el, name):
        for c in el:
            if _local(c.tag) == name:
                return c
        return None

    def descr(el) -> str:
        parts = [
            c.text.strip()
            for c in el.iter()
            if _local(c.tag) in ("Ustrd", "AddtlNtryInf") and c.text and c.text.strip()
        ]
        return "; ".join(parts)[:300] or "Transaction"

    def status_of(el) -> str:
        """`Sts` is a bare code in camt.053.001.02 and a nested `<Sts><Cd>` from
        .001.08 onward. Absent means booked — the statement is a booked-entry
        report — so only an explicit non-BOOK code is treated as unsettled."""
        sts = child(el, "Sts")
        if sts is None:
            return "BOOK"
        cd = child(sts, "Cd")
        raw = (cd.text if cd is not None else sts.text) or ""
        return raw.strip().upper() or "BOOK"

    def reversed_(el) -> bool:
        """`RvslInd` true means this entry REVERSES an earlier one, so a CRDT
        reversal is money going OUT. Ignoring it books the clawback as a second
        payment — the account gains twice what the bank says it did."""
        r = child(el, "RvslInd")
        return r is not None and (r.text or "").strip().lower() in ("true", "1")

    def amount_of(el) -> tuple[Decimal, str | None] | None:
        a = child(el, "Amt")
        if a is None or a.text is None:
            return None
        try:
            return Decimal(a.text.strip()).quantize(_CENTS), (a.get("Ccy") or "").strip() or None
        except (ArithmeticError, ValueError):
            return None

    txns: list[Txn] = []
    skipped_pending = 0
    saw_reversal = False
    for ntry in (e for e in root.iter() if _local(e.tag) == "Ntry"):
        status = status_of(ntry)
        if status != "BOOK":
            # PDNG (and any other non-booked code) is money the bank has not
            # settled. Importing it as cash overstates the balance and can
            # settle an invoice against a payment that may still fail.
            skipped_pending += 1
            continue
        amt = amount_of(ntry)
        cd_el = child(ntry, "CdtDbtInd")
        if amt is None or cd_el is None:
            continue
        amount, ccy = amt
        direction = "credit" if (cd_el.text or "").strip().upper() == "CRDT" else "debit"
        if reversed_(ntry):
            direction = "debit" if direction == "credit" else "credit"
            saw_reversal = True
        # NB: `X or Y` is unsafe for ElementTree elements — an element with no
        # sub-elements is FALSY — so resolve the date holder/field with `is None`.
        dt_holder = child(ntry, "BookgDt")
        if dt_holder is None:
            dt_holder = child(ntry, "ValDt")
        d: date | None = None
        if dt_holder is not None:
            dt_el = child(dt_holder, "Dt")
            if dt_el is None:
                dt_el = child(dt_holder, "DtTm")
            if dt_el is not None and dt_el.text:
                try:
                    d = date.fromisoformat(dt_el.text.strip()[:10])
                except ValueError:
                    d = None
        if d is None:
            continue  # a bookable entry must carry a date

        # A BATCHED entry (one booking covering many payments) carries a
        # `TxDtls` per underlying payment. Collapsed to the batch total it is
        # one unmatchable blob: a collection covering forty invoices arrives as
        # a single figure with forty references crammed into its description.
        #
        # The split is taken ONLY when it is provably complete — every detail
        # carries its own amount and they sum to the entry. A partial split
        # would invent a breakdown the bank did not give, so in that case the
        # entry stays whole and says so.
        details = [
            (t, amount_of(t)) for t in ntry.iter() if _local(t.tag) == "TxDtls" and t is not ntry
        ]
        priced = [(t, a) for t, a in details if a is not None]
        if len(priced) > 1 and fsum_eq(priced, amount):
            for det, (det_amount, det_ccy) in priced:
                txns.append(
                    Txn(
                        date=d,
                        description=descr(det),
                        amount=det_amount,
                        direction=direction,
                        currency=det_ccy or ccy,
                    )
                )
            continue
        if len(details) > 1:
            warnings.append(
                f"A batched entry of {amount} on {d} covers {len(details)} payments the bank did "
                "not price individually; it is imported as one line."
            )
        txns.append(
            Txn(
                date=d,
                description=descr(ntry),
                amount=amount,
                direction=direction,
                currency=ccy,
            )
        )

    if skipped_pending:
        warnings.append(
            f"{skipped_pending} "
            + ("entry was" if skipped_pending == 1 else "entries were")
            + " not booked (pending) and left out — pending money is not in the account yet."
        )
    if saw_reversal:
        warnings.append(
            "This statement contains reversal entries; each is imported in the direction that "
            "REVERSES the original booking."
        )
    seen_ccy = {t.currency for t in txns if t.currency}
    if len(seen_ccy) > 1:
        warnings.append(
            "This statement mixes currencies (" + ", ".join(sorted(seen_ccy)) + "). Amounts in "
            "different currencies are not comparable and must not be summed or matched across."
        )
    if not txns:
        raise ValueError("No bookable entries (Ntry) found in the camt.053 statement.")
    return txns


# --------------------------------------------------------------------------- #
# MT940 path (SWIFT customer statement, the classic bank export; WO-K)
# --------------------------------------------------------------------------- #

#: :61: statement line — value date YYMMDD, optional entry date MMDD, D/C mark
#: (with optional R prefix for reversals and optional funds code letter), amount
#: with a comma decimal, transaction type, then references.
_MT940_61 = re.compile(
    r"^:61:(?P<val>\d{6})(?:\d{4})?(?P<dc>R?[DC])[A-Z]?(?P<amt>\d+,\d*)", re.IGNORECASE
)


def _looks_like_mt940(content: bytes) -> bool:
    head = content[:2048]
    return b":20:" in head and b":61:" in head


def _parse_mt940(content: bytes, warnings: list[str]) -> list[Txn]:
    """Parse the :61: lines (one per booked entry), taking each entry's :86:
    information block as the description. Currency comes from the :60F:/:60M:
    opening balance when present — MT940 states it once for the whole
    statement, not per line. A reversal mark (RD/RC) flips the direction,
    matching how camt.053 reversals are handled."""
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - decode with replace cannot raise
        raise ValueError(f"Could not read the MT940 statement: {exc}") from exc

    currency: str | None = None
    m_open = re.search(r"^:60[FM]:[DC]\d{6}([A-Z]{3})", text, re.MULTILINE)
    if m_open:
        currency = m_open.group(1).upper()

    out: list[Txn] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _MT940_61.match(lines[i].strip())
        if not m:
            i += 1
            continue
        try:
            d = datetime.strptime(m.group("val"), "%y%m%d").date()
        except ValueError:
            warnings.append(f"Skipped an entry with an unreadable value date: {lines[i][:40]}")
            i += 1
            continue
        amount = Decimal(m.group("amt").replace(",", ".")).quantize(_CENTS)
        dc = m.group("dc").upper()
        reversed_ = dc.startswith("R")
        debit = dc.endswith("D")
        if reversed_:
            debit = not debit  # a reversed debit puts the money back
        # The :86: information block (possibly multi-line) that follows is the
        # human description; continuation lines run until the next :tag:.
        desc_parts: list[str] = []
        j = i + 1
        while j < len(lines) and not lines[j].startswith(":"):
            j += 1  # skip :61: sub-account continuation lines
        if j < len(lines) and lines[j].startswith(":86:"):
            desc_parts.append(lines[j][4:].strip())
            j += 1
            while j < len(lines) and not lines[j].startswith(":") and lines[j].strip() != "-":
                desc_parts.append(lines[j].strip())
                j += 1
        description = " ".join(p for p in desc_parts if p) or "MT940 entry"
        if reversed_:
            description = f"[reversal] {description}"
        out.append(
            Txn(
                date=d,
                description=description[:500],
                amount=amount,
                direction="debit" if debit else "credit",
                currency=currency,
            )
        )
        i = max(j, i + 1)
    if not out:
        raise ValueError("No statement lines (:61:) found in the MT940 file.")
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def parse(filename: str, content: bytes) -> StatementResult:
    warnings: list[str] = []
    lower = filename.lower()
    if lower.endswith(".csv"):
        return StatementResult(
            method="csv", transactions=_parse_csv(content, warnings), warnings=warnings
        )
    if lower.endswith(".xml") or _looks_like_camt(content):
        return StatementResult(
            method="camt.053", transactions=_parse_camt053(content, warnings), warnings=warnings
        )
    if lower.endswith((".940", ".sta", ".mt940")) or _looks_like_mt940(content):
        return StatementResult(
            method="mt940", transactions=_parse_mt940(content, warnings), warnings=warnings
        )
    if lower.endswith(".pdf"):
        text, method = pdf_ocr.extract_text(content)
        if not text:
            raise ValueError("Could not read any text from the statement PDF.")
        rows = _rows_from_text(text)
        if not rows:
            raise ValueError("No transaction rows recognised in the statement.")
        return StatementResult(
            method=method, transactions=_transactions_from_rows(rows, warnings), warnings=warnings
        )
    raise ValueError(
        "Unsupported file type. Upload a .pdf, .csv, camt.053 .xml, or MT940 "
        "(.940/.sta) bank statement."
    )
