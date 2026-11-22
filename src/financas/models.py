"""Shared data model. Every connector returns a FetchResult built from these."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Balance:
    source: str          # e.g. "santander", "ibkr"
    account: str         # human label or account id (masked is fine)
    currency: str        # ISO 4217, "BTC" for bitcoin
    amount: Decimal
    kind: str = "cash"   # "cash" | "investments" | "savings" | "crypto"


@dataclass(frozen=True)
class Position:
    source: str
    account: str
    symbol: str
    name: str
    quantity: Decimal
    value: Decimal       # market value in `currency`
    currency: str
    cost_basis: Decimal | None = None   # total cost in `currency`, if known


@dataclass(frozen=True)
class Transaction:
    source: str
    account: str
    booked: date
    amount: Decimal      # negative = money out
    currency: str
    description: str
    ext_id: str          # stable id for dedup; build a hash if provider has none


@dataclass(frozen=True)
class Cashflow:
    """Fluxo de capital com a plataforma, já em euros.

    deposito > 0, levantamento < 0, dividendo e juros > 0, custo < 0.
    """

    source: str
    booked: date
    amount_eur: Decimal
    kind: str            # deposito | levantamento | dividendo | juros | custo
    description: str
    ext_id: str


@dataclass(frozen=True)
class Trade:
    """Compra ou venda de um instrumento.

    quantity > 0 é compra e < 0 é venda. price e net_cash estão na moeda
    `currency` (na compra, net_cash < 0, já com comissões). fx_to_eur é quantos
    euros vale 1 unidade dessa moeda no dia. realized_pnl_eur só nas vendas.
    """

    source: str
    booked: date
    symbol: str
    name: str
    isin: str
    asset_class: str    # acao | etf | cripto | fx | outro
    quantity: Decimal
    price: Decimal
    currency: str
    net_cash: Decimal
    fx_to_eur: Decimal
    realized_pnl_eur: Decimal | None
    ext_id: str


@dataclass
class FetchResult:
    source: str
    balances: list[Balance] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cashflows: list[Cashflow] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)


class NeedsUserAction(Exception):
    """Raised when a connector needs the human (re-consent, 2FA, missing key)."""
