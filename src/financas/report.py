"""Relatórios de património em Markdown e Excel."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from financas import config

CONNECTOR_ORDER = [
    "santander",
    "revolut",
    "traderepublic",
    "ibkr",
    "etoro",
    "aforro",
    "bitcoin",
]

DISPLAY = {
    "santander": "Santander",
    "revolut": "Revolut",
    "traderepublic": "Trade Republic",
    "ibkr": "IBKR",
    "etoro": "eToro",
    "aforro": "Certificados de Aforro",
    "bitcoin": "Bitcoin",
}

_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "BTC": "BTC"}
EUR_FORMAT = "#,##0 €"


def display_name(source: str) -> str:
    return DISPLAY.get(source, source)


def ordered_sources(sources) -> list[str]:
    rank = {name: index for index, name in enumerate(CONNECTOR_ORDER)}
    return sorted(set(sources), key=lambda name: (rank.get(name, len(rank)), name))


def _group(value: int) -> str:
    sign = "-" if value < 0 else ""
    return sign + f"{abs(value):,}".replace(",", " ")


def round0(amount: Decimal) -> int:
    return int(Decimal(amount).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_eur(amount: Decimal) -> str:
    return f"{_group(round0(amount))} €"


def format_amount(amount: Decimal, currency: str) -> str:
    code = currency.upper()
    if code == "BTC":
        return f"{Decimal(amount).quantize(Decimal('0.00000001'))} BTC".replace(".", ",")
    return f"{_group(round0(amount))} {_SYMBOLS.get(code, code)}"


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def _num(value):
    if value is None or value == "":
        return None
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def eur_totals(db, run_id: int) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for source, amount in db.execute(
        "SELECT source, amount_eur FROM balances WHERE run_id=?", (run_id,)
    ):
        totals[source] += _dec(amount)
    for source, amount in db.execute(
        "SELECT source, value_eur FROM positions WHERE run_id=?", (run_id,)
    ):
        totals[source] += _dec(amount)
    return dict(totals)


def original_totals(db, run_id: int) -> dict[str, dict[str, Decimal]]:
    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal(0)))
    for source, currency, amount in db.execute(
        "SELECT source, currency, amount FROM balances WHERE run_id=?", (run_id,)
    ):
        totals[source][currency] += _dec(amount)
    for source, currency, amount in db.execute(
        "SELECT source, currency, value FROM positions WHERE run_id=?", (run_id,)
    ):
        totals[source][currency] += _dec(amount)
    return totals


def run_statuses(db, run_id: int) -> dict[str, tuple[str, str]]:
    try:
        rows = db.execute(
            "SELECT source, status, warnings FROM run_status WHERE run_id=?", (run_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {source: (status, warnings or "") for source, status, warnings in rows}


def _day(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _note(source: str, originals, status: str) -> str:
    foreign = [
        format_amount(amount, code)
        for code, amount in sorted(originals.get(source, {}).items())
        if code.upper() != "EUR"
    ]
    parts = []
    if foreign:
        parts.append(", ".join(foreign))
    if status and status != "ok":
        parts.append(status)
    return "; ".join(parts)


def _table_row(name: str, value: str, note: str) -> str:
    if note:
        return f"| {name} | {value} | {note} |"
    return f"| {name} | {value} | |"


def write_markdown(db, run_id: int, out_dir: Path | None = None) -> Path:
    """Grava `<data>_Patrimonio_auto.md` com o património desta corrida."""
    if out_dir is None:
        out_dir = config.CONTABILIDADE / "Investimentos"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    when = _day(db.execute("SELECT at FROM runs WHERE id=?", (run_id,)).fetchone()[0])
    totals = eur_totals(db, run_id)
    originals = original_totals(db, run_id)
    statuses = run_statuses(db, run_id)
    sources = ordered_sources(set(totals) | set(statuses))

    lines = [
        f"# Património a {when.strftime('%d/%m/%Y')} (automático)",
        "",
        "| Onde | Valor | Notas |",
        "| --- | --- | --- |",
    ]
    grand = Decimal(0)
    for source in sources:
        amount = totals.get(source, Decimal(0))
        grand += amount
        status, _warnings = statuses.get(source, ("", ""))
        lines.append(_table_row(display_name(source), format_eur(amount), _note(source, originals, status)))
    lines.append(_table_row("**Total**", f"**{format_eur(grand)}**", ""))
    lines.append("")

    grouped: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
    for source, name, value_eur in db.execute(
        "SELECT source, name, value_eur FROM positions WHERE run_id=?", (run_id,)
    ):
        grouped[source].append((name, _dec(value_eur)))
    for source in ordered_sources(grouped):
        items = sorted(grouped[source], key=lambda item: (-item[1], item[0]))
        rendered = ", ".join(f"{name} {format_eur(value)}" for name, value in items)
        lines.extend([f"## {display_name(source)}: posições", "", rendered, ""])

    lines.extend(["## Avisos", ""])
    avisos: list[str] = []
    for source in sources:
        status, warnings = statuses.get(source, ("", ""))
        label = display_name(source)
        if status and status != "ok":
            avisos.append(f"- {label}: {status}")
        for warning in warnings.splitlines():
            if warning:
                avisos.append(f"- {label}: {warning}")
    lines.extend(avisos or ["- Nenhum."])

    path = out_dir / f"{when.isoformat()}_Patrimonio_auto.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _known_sources(db) -> list[str]:
    found: set[str] = set()
    for sql in (
        "SELECT DISTINCT source FROM balances",
        "SELECT DISTINCT source FROM positions",
        "SELECT DISTINCT source FROM transactions",
        "SELECT DISTINCT source FROM run_status",
    ):
        try:
            found.update(row[0] for row in db.execute(sql))
        except sqlite3.OperationalError:
            continue
    return ordered_sources(found)


def _style(ws) -> None:
    bold = Font(bold=True)
    for cell in ws[1]:
        cell.font = bold
    ws.freeze_panes = "A2"
    for index, column in enumerate(ws.columns, start=1):
        length = max((len(str(cell.value)) for cell in column if cell.value is not None), default=0)
        ws.column_dimensions[get_column_letter(index)].width = min(max(length + 2, 12), 48)


def _money_format(ws, start_col: int, end_col: int | None = None) -> None:
    end = end_col or start_col
    for row in ws.iter_rows(min_row=2, min_col=start_col, max_col=end):
        for cell in row:
            if isinstance(cell.value, (int, float)):
                cell.number_format = EUR_FORMAT


def write_excel(db, path: Path | None = None) -> Path:
    """Reconstrói o livro histórico a partir de toda a base."""
    if path is None:
        path = config.CONTABILIDADE / "Investimentos" / "Patrimonio_historico.xlsx"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    sources = _known_sources(db)
    runs = db.execute("SELECT id, at FROM runs ORDER BY id").fetchall()
    rank = {name: index for index, name in enumerate(CONNECTOR_ORDER)}

    wb = Workbook()
    resumo = wb.active
    resumo.title = "Resumo"
    resumo.append(["Data", *[display_name(source) for source in sources], "Total"])
    for run_id, at in runs:
        totals = eur_totals(db, run_id)
        amounts = [totals.get(source, Decimal(0)) for source in sources]
        grand = sum(amounts, start=Decimal(0))
        resumo.append([_day(at), *[_num(amount) for amount in amounts], _num(grand)])
    _money_format(resumo, 2, 1 + len(sources) + 1)
    _style(resumo)

    posicoes = wb.create_sheet("Posições")
    posicoes.append(["Fonte", "Nome", "Símbolo", "Quantidade", "Valor", "Moeda", "Valor €", "Custo"])
    if runs:
        latest = runs[-1][0]
        rows = db.execute(
            """SELECT source, name, symbol, quantity, value, currency, value_eur, cost_basis
               FROM positions WHERE run_id=?""",
            (latest,),
        ).fetchall()
        rows.sort(key=lambda row: (-_dec(row[6]), row[1] or ""))
        for source, name, symbol, quantity, value, currency, value_eur, cost in rows:
            posicoes.append([
                display_name(source),
                name,
                symbol,
                _num(quantity),
                _num(value),
                currency,
                _num(value_eur),
                _num(cost),
            ])
    _money_format(posicoes, 7, 7)
    _style(posicoes)

    movimentos = wb.create_sheet("Movimentos")
    movimentos.append(["Data", "Fonte", "Conta", "Descrição", "Montante", "Moeda"])
    for booked, source, account, description, amount, currency in db.execute(
        """SELECT booked, source, account, description, amount, currency
           FROM transactions ORDER BY booked DESC, rowid DESC"""
    ):
        movimentos.append([
            _day(booked),
            display_name(source),
            account,
            description,
            _num(amount),
            currency,
        ])
    _style(movimentos)

    estado = wb.create_sheet("Estado")
    estado.append(["Fonte", "Estado", "Avisos"])
    latest_status = None
    try:
        latest_status = db.execute("SELECT MAX(run_id) FROM run_status").fetchone()[0]
    except sqlite3.OperationalError:
        latest_status = None
    if latest_status is not None:
        rows = db.execute(
            "SELECT source, status, warnings FROM run_status WHERE run_id=?",
            (latest_status,),
        ).fetchall()
        rows.sort(key=lambda row: (rank.get(row[0], len(rank)), row[0]))
        for source, status, warnings in rows:
            estado.append([display_name(source), status, warnings or ""])
    _style(estado)

    wb.save(path)
    return path
