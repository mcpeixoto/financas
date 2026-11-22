"""eToro public API. Só leitura da conta real."""
from __future__ import annotations

import json
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx

from financas import config, secrets
from financas.fx import Rates
from financas.models import Balance, Cashflow, FetchResult, NeedsUserAction, Position, Trade, Transaction

NAME = "etoro"
SECRETS = {
    "etoro_api_key": "eToro API key (pública)",
    "etoro_user_key": "eToro User key",
}
BASE = "https://public-api.etoro.com/api/v1"
_PAGE_SIZE = 100
_AVISO_DEPOSITOS = (
    "eToro: sem depósitos configurados; a rentabilidade do eToro não pode ser calculada "
    "(config etoro.depositos)"
)


def _today() -> date:
    return date.today()


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError(value)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value).replace(" ", "").replace(",", "."))


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _id_key(value) -> str:
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return str(value)


def _ci(obj, key: str, default=None):
    if not isinstance(obj, dict):
        return default
    want = key.lower()
    for name, value in obj.items():
        if str(name).lower() == want:
            return value
    return default


def _loads(text: str):
    if not text or not text.strip():
        return {}
    return json.loads(text, parse_float=Decimal)


def _portfolio(data) -> dict:
    if isinstance(data, dict):
        nested = _ci(data, "clientPortfolio")
        if isinstance(nested, dict):
            return nested
        return data
    return {}


def _rows(data, key: str) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    nested = _ci(data, "clientPortfolio")
    if isinstance(nested, dict):
        found = _ci(nested, key)
        if isinstance(found, list):
            return found
    found = _ci(data, key)
    return found if isinstance(found, list) else []


def _instrument_rows(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("instruments", "instrumentDisplayDatas", "instrumentDisplayData", "items", "data"):
            found = _ci(data, key)
            if isinstance(found, list):
                return found
    return []


def _pnl(position: dict) -> Decimal:
    raw = _ci(position, "unrealizedPnL")
    if isinstance(raw, dict):
        raw = _ci(raw, "pnL")
    if raw is None:
        return Decimal(0)
    return _dec(raw)


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) >= 10:
        return date.fromisoformat(text[:10])
    return None


def _windows(since: date, today: date):
    start = since
    while start <= today:
        end = min(start + timedelta(days=364), today)
        yield start, end
        if end >= today:
            break
        start = end + timedelta(days=1)


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "1")
    try:
        return float(raw)
    except ValueError:
        return 1.0


class _EToro:
    def __init__(self, api_key: str, user_key: str, sleep):
        self.api_key = api_key
        self.user_key = user_key
        self.sleep = sleep

    def get(self, path: str, params: dict | None = None, *, optional: bool = False):
        url = f"{BASE}{path}"
        for _attempt in range(5):
            headers = {
                "x-api-key": self.api_key,
                "x-user-key": self.user_key,
                "x-request-id": str(uuid.uuid4()),
            }
            with httpx.Client(timeout=30) as client:
                response = client.get(url, params=params, headers=headers)
            if response.status_code == 429:
                self.sleep(_retry_after(response))
                continue
            if response.status_code in (401, 403):
                raise NeedsUserAction(
                    "A eToro recusou o acesso. Verifica a API key e a User key no Keychain."
                )
            if response.status_code >= 400:
                if optional:
                    return None
                response.raise_for_status()
            return _loads(response.text)
        raise NeedsUserAction(
            "A eToro está a limitar os pedidos. Tenta outra vez dentro de momentos."
        )


def _names(client: _EToro, instrument_ids: list[str]) -> tuple[dict[str, dict], bool]:
    unique = sorted(set(instrument_ids))
    if not unique:
        return {}, False
    payload = client.get(
        "/market-data/instruments",
        params={"instrumentIds": ",".join(unique)},
        optional=True,
    )
    if payload is None:
        return {}, True
    found: dict[str, dict] = {}
    for item in _instrument_rows(payload):
        if isinstance(item, dict):
            found[_id_key(_ci(item, "instrumentID"))] = item
    return found, False


def _display(meta: dict | None, instrument_id: str) -> tuple[str, str]:
    if not meta:
        return instrument_id, instrument_id
    name = _ci(meta, "instrumentDisplayName") or _ci(meta, "symbolFull") or instrument_id
    symbol = _ci(meta, "symbolFull") or _ci(meta, "instrumentDisplayName") or instrument_id
    return str(symbol), str(name)


def fetch(since: date, _sleep=None) -> FetchResult:
    sleep = time.sleep if _sleep is None else _sleep
    api_key = secrets.get("etoro_api_key")
    user_key = secrets.get("etoro_user_key")
    if not api_key or not user_key:
        raise NeedsUserAction("Faltam as chaves da eToro no Keychain (API key e User key).")
    client = _EToro(api_key, user_key, sleep)
    pnl = client.get("/trading/info/real/pnl")
    portfolio = _portfolio(pnl)
    open_rows = _rows(pnl, "positions")
    open_ids = [
        _id_key(_ci(row, "instrumentID"))
        for row in open_rows
        if isinstance(row, dict) and _ci(row, "instrumentID") is not None
    ]

    closed: list[dict] = []
    cash_rows: list[dict] = []
    today = _today()
    if since <= today:
        for start, end in _windows(since, today):
            page = 1
            while True:
                payload = client.get(
                    "/trading/info/trade/history",
                    params={
                        "minDate": start.isoformat(),
                        "maxDate": end.isoformat(),
                        "page": page,
                        "pageSize": _PAGE_SIZE,
                    },
                )
                batch = [row for row in _rows(payload, "positions") if isinstance(row, dict)]
                closed.extend(batch)
                for key in ("cashflows", "moneyMovements", "accountActivity"):
                    cash_rows.extend(row for row in _rows(payload, key) if isinstance(row, dict))
                if len(batch) < _PAGE_SIZE:
                    break
                page += 1

    closed_ids = [
        _id_key(_ci(row, "instrumentID"))
        for row in closed
        if _ci(row, "instrumentID") is not None
    ]
    meta, failed = _names(client, open_ids + closed_ids)
    warnings: list[str] = []
    if failed:
        warnings.append(
            "Não foi possível obter os nomes dos instrumentos na eToro; a usar o identificador."
        )

    credit = _ci(portfolio, "credit")
    balances = [
        Balance(
            source=NAME,
            account="eToro",
            currency="USD",
            amount=_dec(credit) if credit is not None else Decimal(0),
            kind="cash",
        )
    ]
    positions: list[Position] = []
    for row in open_rows:
        if not isinstance(row, dict):
            continue
        instrument_id = _id_key(_ci(row, "instrumentID"))
        symbol, name = _display(meta.get(instrument_id), instrument_id)
        amount = _dec(_ci(row, "amount") or 0)
        units = _dec(_ci(row, "units") or 0)
        positions.append(
            Position(
                source=NAME,
                account="eToro",
                symbol=symbol,
                name=name,
                quantity=units,
                value=amount + _pnl(row),
                currency="USD",
                cost_basis=amount,
            )
        )

    transactions: list[Transaction] = []
    seen: set[str] = set()
    for row in closed:
        instrument_id = _id_key(_ci(row, "instrumentID")) if _ci(row, "instrumentID") is not None else ""
        _symbol, name = _display(meta.get(instrument_id), instrument_id or "?")
        booked = _as_date(_ci(row, "closeTimestamp") or _ci(row, "closeDate"))
        if booked is None or booked < since:
            continue
        position_id = _ci(row, "positionID")
        ext_id = f"close-{_id_key(position_id)}"
        if ext_id in seen:
            continue
        seen.add(ext_id)
        units = _dec(_ci(row, "units") or 0)
        transactions.append(
            Transaction(
                source=NAME,
                account="eToro",
                booked=booked,
                amount=_dec(_ci(row, "netProfit") or 0),
                currency="USD",
                description=f"Fechou {name} ({_plain(units)})",
                ext_id=ext_id,
            )
        )
    cashflows = _cashflows(cash_rows, since, warnings)
    manuais, aviso = _depositos_manuais(since)
    if aviso is not None and aviso not in warnings:
        warnings.append(aviso)
    cashflows.extend(manuais)
    trades = _negocios(open_rows, closed, meta, since, warnings)
    return FetchResult(
        source=NAME,
        balances=balances,
        positions=positions,
        transactions=transactions,
        warnings=warnings,
        cashflows=cashflows,
        trades=trades,
    )


_CLASSES = {10: "cripto", 5: "acao", 6: "etf"}


def _primeiro_dec(row: dict, *keys: str) -> Decimal | None:
    for key in keys:
        raw = _ci(row, key)
        if raw in (None, ""):
            continue
        try:
            return _dec(raw)
        except (TypeError, ValueError, ArithmeticError):
            continue
    return None


def _data_negocio(row: dict, *keys: str) -> date | None:
    for key in keys:
        booked = _as_date(_ci(row, key))
        if booked is not None:
            return booked
    return None


def _classe_etoro(meta: dict | None) -> str:
    if not meta:
        return "outro"
    try:
        return _CLASSES.get(int(_ci(meta, "instrumentTypeID")), "outro")
    except (TypeError, ValueError):
        return "outro"


def _instrumento(meta: dict | None, instrument_id: str) -> tuple[str, str, str, str]:
    if not meta:
        return instrument_id, instrument_id, "", "outro"
    symbol = _ci(meta, "symbolFull") or instrument_id
    name = _ci(meta, "instrumentDisplayName") or instrument_id
    isin = _ci(meta, "isin") or ""
    return str(symbol), str(name), str(isin), _classe_etoro(meta)


def _taxa(explicita: Decimal | None, dinheiro: Decimal | None, unidades: Decimal) -> Decimal | None:
    if explicita is not None:
        return explicita
    if dinheiro is not None and unidades != 0:
        return dinheiro / unidades
    return None


def _fx_usd(warnings: list[str]) -> Decimal | None:
    try:
        return Rates.fetch().to_eur(Decimal(1), "USD")
    except Exception:
        warnings.append("Não foi possível converter os negócios da eToro para euros.")
        return None


def _negocios(
    open_rows: list,
    closed: list[dict],
    meta: dict[str, dict],
    since: date,
    warnings: list[str],
) -> list[Trade]:
    """Compras das posições abertas e o par compra/venda das posições fechadas.

    fx_to_eur é a taxa atual USD→EUR do BCE (quantos euros vale 1 dólar), a mesma
    que Rates.to_eur já aplica aos movimentos. A API pública não traz o câmbio do
    dia do negócio, por isso usa-se a taxa corrente.
    """
    drafts: list[tuple[dict, Decimal | None]] = []
    seen: set[str] = set()

    def add(fields: dict, pnl_usd: Decimal | None) -> None:
        if fields["ext_id"] in seen:
            return
        seen.add(fields["ext_id"])
        drafts.append((fields, pnl_usd))

    for row in open_rows:
        if isinstance(row, dict):
            _compra(row, meta, add)
    for row in closed:
        fecho = _data_negocio(row, "closeTimestamp", "closeDate")
        if fecho is None or fecho < since:
            continue
        _compra(row, meta, add)
        _venda(row, meta, fecho, add)
    if not drafts:
        return []
    fx = _fx_usd(warnings)
    if fx is None:
        return []
    found = [
        Trade(**fields, fx_to_eur=fx, realized_pnl_eur=None if pnl is None else pnl * fx)
        for fields, pnl in drafts
    ]
    found.sort(key=lambda item: (item.booked, item.ext_id))
    return found


def _compra(row: dict, meta: dict[str, dict], add) -> None:
    position_id = _ci(row, "positionID")
    if position_id is None:
        return
    booked = _data_negocio(row, "openTimestamp", "openDate")
    units = _primeiro_dec(row, "units")
    if booked is None or units is None or units == 0:
        return
    units = abs(units)
    invested = _primeiro_dec(row, "amount", "investment")
    if invested is not None:
        invested = abs(invested)
    price = _taxa(_primeiro_dec(row, "openRate"), invested, units)
    if price is None:
        return
    if invested is None:
        invested = units * price
    instrument_id = _id_instrumento(row)
    symbol, name, isin, classe = _instrumento(meta.get(instrument_id), instrument_id)
    add(
        {
            "source": NAME,
            "booked": booked,
            "symbol": symbol,
            "name": name,
            "isin": isin,
            "asset_class": classe,
            "quantity": units,
            "price": price,
            "currency": "USD",
            "net_cash": -invested,
            "ext_id": f"{_id_key(position_id)}-buy",
        },
        None,
    )


def _venda(row: dict, meta: dict[str, dict], booked: date, add) -> None:
    position_id = _ci(row, "positionID")
    if position_id is None:
        return
    units = _primeiro_dec(row, "units")
    if units is None or units == 0:
        return
    units = abs(units)
    invested = _primeiro_dec(row, "amount", "investment")
    if invested is not None:
        invested = abs(invested)
    profit = _primeiro_dec(row, "netProfit")
    proceeds = invested + profit if invested is not None and profit is not None else None
    price = _taxa(_primeiro_dec(row, "closeRate"), proceeds, units)
    if price is None:
        return
    if proceeds is None:
        proceeds = units * price
    instrument_id = _id_instrumento(row)
    symbol, name, isin, classe = _instrumento(meta.get(instrument_id), instrument_id)
    add(
        {
            "source": NAME,
            "booked": booked,
            "symbol": symbol,
            "name": name,
            "isin": isin,
            "asset_class": classe,
            "quantity": -units,
            "price": price,
            "currency": "USD",
            "net_cash": proceeds,
            "ext_id": f"{_id_key(position_id)}-sell",
        },
        profit,
    )


def _id_instrumento(row: dict) -> str:
    raw = _ci(row, "instrumentID")
    if raw is None:
        return "?"
    return _id_key(raw)


def _depositos_manuais(since: date) -> tuple[list[Cashflow], str | None]:
    """Depósitos escritos em config, porque a API pública não os devolve."""
    raw = config.section(NAME).get("depositos")
    if not isinstance(raw, list) or not raw:
        return [], _AVISO_DEPOSITOS
    found: list[Cashflow] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or "data" not in item or "valor_eur" not in item:
            continue
        booked = _as_date(item.get("data"))
        if booked is None or booked < since:
            continue
        try:
            valor = _dec(item.get("valor_eur"))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if valor == 0:
            continue
        kind = "deposito" if valor > 0 else "levantamento"
        ext_id = f"manual-{booked.isoformat()}-{_plain(valor)}"
        if ext_id in seen:
            continue
        seen.add(ext_id)
        found.append(
            Cashflow(
                source=NAME,
                booked=booked,
                amount_eur=valor,
                kind=kind,
                description="Depósito manual" if kind == "deposito" else "Levantamento manual",
                ext_id=ext_id,
            )
        )
    return found, None


def _etoro_kind(raw: str) -> str | None:
    text = raw.casefold().strip()
    if "dividend" in text:
        return "dividendo"
    if "withdraw" in text:
        return "levantamento"
    if "deposit" in text:
        return "deposito"
    return None


def _cashflows(rows: list[dict], since: date, warnings: list[str]) -> list[Cashflow]:
    useful = []
    for row in rows:
        kind = _etoro_kind(str(_ci(row, "type") or _ci(row, "kind") or ""))
        if kind is not None:
            useful.append((row, kind))
    if not useful:
        return []
    try:
        rates = Rates.fetch()
    except Exception:
        warnings.append("Não foi possível converter os movimentos da eToro para euros.")
        return []
    found: list[Cashflow] = []
    seen: set[str] = set()
    for row, kind in useful:
        booked = _as_date(
            _ci(row, "date") or _ci(row, "occurredAt") or _ci(row, "timestamp") or _ci(row, "dateTime")
        )
        if booked is None or booked < since:
            continue
        raw_id = _ci(row, "id")
        if raw_id is None:
            raw_id = _ci(row, "cashflowId") or _ci(row, "transactionId")
        ext_id = f"cash-{_id_key(raw_id)}" if raw_id is not None else ""
        amount = abs(_dec(_ci(row, "amount") or 0))
        try:
            eur = rates.to_eur(amount, "USD")
        except Exception:
            warnings.append("Não foi possível converter os movimentos da eToro para euros.")
            continue
        if kind == "levantamento":
            eur = -eur
        if not ext_id:
            ext_id = f"cash-{booked.isoformat()}-{kind}-{eur}"
        if ext_id in seen:
            continue
        seen.add(ext_id)
        description = str(_ci(row, "description") or _ci(row, "details") or kind)
        found.append(
            Cashflow(
                source=NAME,
                booked=booked,
                amount_eur=eur,
                kind=kind,
                description=description,
                ext_id=ext_id,
            )
        )
    return found
