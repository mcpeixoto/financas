"""eToro public API. Só leitura da conta real."""
from __future__ import annotations

import json
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx

from financas import secrets
from financas.models import Balance, FetchResult, NeedsUserAction, Position, Transaction

NAME = "etoro"
SECRETS = {
    "etoro_api_key": "eToro API key (pública)",
    "etoro_user_key": "eToro User key",
}
BASE = "https://public-api.etoro.com/api/v1"
_PAGE_SIZE = 100


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
        for key in ("instruments", "instrumentDisplayData", "items", "data"):
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
    return FetchResult(
        source=NAME,
        balances=balances,
        positions=positions,
        transactions=transactions,
        warnings=warnings,
    )
