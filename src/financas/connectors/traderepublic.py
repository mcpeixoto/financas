"""Trade Republic via pytr (read-only). A sessão fica em ~/.pytr."""
from __future__ import annotations

import csv
import hashlib
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path

from financas import config
from financas.models import Balance, Cashflow, FetchResult, NeedsUserAction, Position, Transaction

NAME = "traderepublic"
SECRETS: dict[str, str] = {}

_INSTALL = "Instala o pytr: uv tool install pytr==0.4.10"
_LOGIN = "Trade Republic: corre `pytr login --store_credentials --v2` no Terminal"
_ACCOUNT = "Trade Republic"

_PORTFOLIO_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("name", "nome"),
    "isin": ("isin",),
    "quantity": ("quantity", "shares", "quantidade"),
    "avgcost": ("avgcost", "average", "avgbuy"),
    "netvalue": ("netvalue", "marketvalue", "value", "valor"),
}
_TX_FIELDS: dict[str, tuple[str, ...]] = {
    "date": ("datum", "date", "data"),
    "type": ("type", "typ", "tipo"),
    "value": ("amount", "value", "valor", "betrag"),
    "note": ("description", "descricao", "note", "nota", "buchung"),
    "isin": ("isin",),
}
_CASH_KIND = {
    "deposit": "deposito",
    "einzahlung": "deposito",
    "removal": "levantamento",
    "auszahlung": "levantamento",
    "dividend": "dividendo",
    "interest": "juros",
    "zinsen": "juros",
}


def fetch(since: date) -> FetchResult:
    binary = _binary()
    with tempfile.TemporaryDirectory() as tmp:
        portfolio_path = Path(tmp) / "portfolio.csv"
        portfolio_proc = _run(
            [binary, "portfolio", "-o", str(portfolio_path)],
            cwd=tmp,
        )
        _raise_if_login(portfolio_proc)
        if not portfolio_path.is_file():
            raise NeedsUserAction(
                "Trade Republic: o pytr não criou o ficheiro CSV esperado."
            )
        before = {p.resolve() for p in Path(tmp).rglob("*.csv")}
        tx_proc = _run(
            [
                binary,
                "export_transactions",
                "--last_days",
                str(max(1, (date.today() - since).days + 1)),
                "--format",
                "csv",
                "--outputdir",
                tmp,
            ],
            cwd=tmp,
        )
        _raise_if_login(tx_proc)
        produced = [
            p
            for p in sorted(Path(tmp).rglob("*.csv"))
            if p.resolve() not in before
        ]
        if not produced:
            raise NeedsUserAction(
                "Trade Republic: o pytr não criou o ficheiro CSV esperado."
            )
        positions, cash = _parse_portfolio(portfolio_path.read_text(encoding="utf-8-sig"))
        if cash is None:
            cash = _stdout_cash(portfolio_proc)
        transactions, cashflows = _parse_transactions(
            "\n".join(p.read_text(encoding="utf-8-sig") for p in produced),
            since,
        )
    warnings: list[str] = []
    balances: list[Balance] = []
    if cash is None:
        warnings.append("Trade Republic: saldo de caixa em falta na carteira.")
    else:
        balances.append(
            Balance(
                source=NAME,
                account=_ACCOUNT,
                currency="EUR",
                amount=cash,
                kind="cash",
            )
        )
    return FetchResult(
        source=NAME,
        balances=balances,
        positions=positions,
        transactions=transactions,
        warnings=warnings,
        cashflows=cashflows,
    )


def _binary() -> str:
    found = shutil.which("pytr")
    if found:
        return found
    configured = str(config.section("traderepublic").get("pytr_path") or "").strip()
    if configured:
        return configured
    raise NeedsUserAction(_INSTALL)


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,  # never let pytr wait for a phone/PIN prompt
            text=True,
            timeout=300,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise NeedsUserAction("Trade Republic: o pytr excedeu o tempo limite.") from exc


_BENIGN = ("using credentials from file", "websession resumed", "trying to resume websession")
_CASH_LINE = re.compile(r"^\s*Cash\s+EUR\s+(-?[\d.,]+)\s*$", re.MULTILINE)


def _stdout_cash(proc: subprocess.CompletedProcess[str]) -> Decimal | None:
    """pytr 0.4.10 prints cash only in its summary, not in the CSV."""
    match = _CASH_LINE.search(proc.stdout or "")
    return _parse_decimal(match.group(1)) if match else None


def _raise_if_login(proc: subprocess.CompletedProcess[str]) -> None:
    lines = f"{proc.stdout or ''}\n{proc.stderr or ''}".splitlines()
    # pytr logs "Using credentials from file ..." on every successful run.
    text = "\n".join(l for l in lines if not any(b in l.casefold() for b in _BENIGN))
    folded = text.casefold()
    if proc.returncode != 0 or "login" in folded or "credentials" in folded or "401" in text:
        raise NeedsUserAction(_LOGIN)


def _parse_portfolio(text: str) -> tuple[list[Position], Decimal | None]:
    positions: list[Position] = []
    cash: Decimal | None = None
    for row in _read_table(text):
        mapped = _map_row(row, _PORTFOLIO_FIELDS)
        name = mapped.get("name", "").strip()
        isin = mapped.get("isin", "").strip()
        norm = _norm_header(name)
        is_cash = not isin and (norm in {"cash", "saldo", "guthaben"} or norm.startswith("cash"))
        value_raw = mapped.get("netvalue", "").strip()
        if is_cash:
            if value_raw:
                amount = _parse_decimal(value_raw)
                cash = amount if cash is None else cash + amount
            continue
        if not isin:
            continue
        qty_raw = mapped.get("quantity", "").strip()
        avg_raw = mapped.get("avgcost", "").strip()
        quantity = _parse_decimal(qty_raw) if qty_raw else Decimal("0")
        value = _parse_decimal(value_raw) if value_raw else Decimal("0")
        cost = _parse_decimal(avg_raw) * quantity if avg_raw else None
        positions.append(
            Position(
                source=NAME,
                account=_ACCOUNT,
                symbol=isin,
                name=name or isin,
                quantity=quantity,
                value=value,
                currency="EUR",
                cost_basis=cost,
            )
        )
    return positions, cash


def _parse_transactions(text: str, since: date) -> tuple[list[Transaction], list[Cashflow]]:
    grouped: dict[date, int] = {}
    found: list[Transaction] = []
    flows: list[Cashflow] = []
    for row in _read_table(text):
        mapped = _map_row(row, _TX_FIELDS)
        raw_date = mapped.get("date", "").strip()
        raw_value = mapped.get("value", "").strip()
        if not raw_date or not raw_value:
            continue
        booked = _parse_date(raw_date)
        amount = _parse_decimal(raw_value)
        typ = mapped.get("type", "").strip()
        note = mapped.get("note", "").strip()
        idx = grouped.get(booked, 0)
        grouped[booked] = idx + 1
        if booked < since:
            continue
        if typ and note:
            description = f"{typ} — {note}"
        else:
            description = note or typ or "Movimento"
        ext_id = hashlib.sha1(
            f"{booked.isoformat()}|{typ}|{raw_value}|{note}|{idx}".encode()
        ).hexdigest()
        found.append(
            Transaction(
                source=NAME,
                account=_ACCOUNT,
                booked=booked,
                amount=amount,
                currency="EUR",
                description=description,
                ext_id=ext_id,
            )
        )
        kind = _CASH_KIND.get(typ.casefold())
        if kind is None:
            continue
        flows.append(
            Cashflow(
                source=NAME,
                booked=booked,
                amount_eur=_com_sinal(kind, amount),
                kind=kind,
                description=description,
                ext_id=ext_id,
            )
        )
    return found, flows


def _com_sinal(kind: str, amount: Decimal) -> Decimal:
    magnitude = abs(amount)
    if kind in {"levantamento", "custo"}:
        return -magnitude
    return magnitude


def _read_table(text: str) -> list[dict[str, str]]:
    if not text.strip():
        return []
    sample = text[:8192]
    candidates: list[str] = []
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
        candidates.append(sniffed)
    except csv.Error:
        pass
    for delim in (";", ",", "\t"):
        if delim not in candidates:
            candidates.append(delim)
    best: list[dict[str, str]] = []
    best_width = 0
    for index, delim in enumerate(candidates):
        rows = _parse_delimited(text, delim)
        width = max((len(row) for row in rows), default=0)
        if index == 0 and width > 1:
            return rows
        if width > best_width:
            best = rows
            best_width = width
    return best


def _parse_delimited(text: str, delimiter: str) -> list[dict[str, str]]:
    reader = csv.DictReader(StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        return []
    reader.fieldnames = [_clean(name) for name in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for raw in reader:
        row: dict[str, str] = {}
        for key, value in raw.items():
            if key is None:
                continue
            row[_clean(key)] = (value or "").strip()
        if any(row.values()):
            rows.append(row)
    return rows


def _map_row(row: dict[str, str], fields: dict[str, tuple[str, ...]]) -> dict[str, str]:
    headers = list(row.keys())
    used: set[int] = set()
    mapped: dict[str, str] = {}
    for field, patterns in fields.items():
        idx = _pick(headers, patterns, used)
        if idx is None:
            continue
        used.add(idx)
        mapped[field] = row[headers[idx]]
    return mapped


def _pick(headers: list[str], patterns: tuple[str, ...], used: set[int]) -> int | None:
    ranked: list[tuple[tuple[int, int, int], int]] = []
    for index, header in enumerate(headers):
        if index in used:
            continue
        norm = _norm_header(header)
        if not norm:
            continue
        best: tuple[int, int, int] | None = None
        for pat in patterns:
            if norm == pat:
                score = (0, 0, len(norm))
            elif pat in norm:
                score = (1, len(norm) - len(pat), len(norm))
            else:
                continue
            if best is None or score < best:
                best = score
        if best is not None:
            ranked.append((best, index))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][1]


def _norm_header(header: str) -> str:
    return "".join(ch for ch in header.casefold() if ch.isalnum())


def _clean(value: str) -> str:
    return value.replace("\ufeff", "").strip()


def _parse_date(raw: str) -> date:
    text = raw.strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return date.fromisoformat(text[:10])


def _parse_decimal(raw: str) -> Decimal:
    text = (
        raw.strip()
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("€", "")
        .replace("EUR", "")
        .replace("%", "")
    )
    if not text or text in {".", ",", "-", "+"}:
        raise ValueError(f"número inválido: {raw!r}")
    neg = False
    if text[0] in "+-":
        neg = text[0] == "-"
        text = text[1:]
    if text.endswith("-"):
        neg = True
        text = text[:-1]
    if text.startswith("(") and text.endswith(")"):
        neg = True
        text = text[1:-1]
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    value = Decimal(text)
    return -value if neg else value
