"""eToro public API: carteira, nomes e histórico por janelas."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from financas.connectors import etoro
from financas.models import NeedsUserAction

BASE = "https://public-api.etoro.com/api/v1"


def _secrets(monkeypatch) -> None:
    monkeypatch.setattr(
        etoro.secrets,
        "get",
        lambda name: {
            "etoro_api_key": "pub-key",
            "etoro_user_key": "user-key",
        }.get(name),
    )


def _pnl_body() -> dict:
    return {
        "ClientPortfolio": {
            "Positions": [
                {
                    "PositionID": 11,
                    "InstrumentID": 1001,
                    "IsBuy": True,
                    "Amount": 100,
                    "Units": 2,
                    "UnrealizedPNL": {"PNL": 12.5, "CloseRate": 50},
                },
                {
                    "positionid": 12,
                    "instrumentid": 2002,
                    "isbuy": False,
                    "amount": "40.00",
                    "units": "4",
                    "unrealizedpnl": {"pnl": -5},
                },
            ],
            "Credit": 250.75,
        }
    }


@respx.mock
def test_case_insensitive_keys_value_and_names(monkeypatch):
    _secrets(monkeypatch)
    respx.get(f"{BASE}/trading/info/real/pnl").mock(
        return_value=httpx.Response(200, json=_pnl_body())
    )

    def instruments(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["instrumentIds"]
        assert "1001" in ids and "2002" in ids
        assert request.headers["x-api-key"] == "pub-key"
        assert request.headers["x-user-key"] == "user-key"
        assert request.headers["x-request-id"]
        return httpx.Response(
            200,
            json=[
                {
                    "instrumentID": 1001,
                    "instrumentDisplayName": "Apple",
                    "symbolFull": "AAPL",
                }
            ],
        )

    respx.get(f"{BASE}/market-data/instruments").mock(side_effect=instruments)
    respx.get(f"{BASE}/trading/info/trade/history").mock(
        return_value=httpx.Response(200, json={"clientPortfolio": {"positions": []}})
    )

    result = etoro.fetch(date(2026, 11, 1), _sleep=lambda s: None)
    assert result.source == "etoro"
    assert len(result.balances) == 1
    assert result.balances[0].amount == Decimal("250.75")
    assert result.balances[0].currency == "USD"
    assert result.balances[0].kind == "cash"

    by_symbol = {p.symbol: p for p in result.positions}
    apple = by_symbol["AAPL"]
    assert apple.name == "Apple"
    assert apple.value == Decimal("112.5")
    assert apple.cost_basis == Decimal("100")
    assert apple.quantity == Decimal("2")
    assert apple.currency == "USD"

    fallback = next(p for p in result.positions if p.symbol == "2002")
    assert fallback.name == "2002"
    assert fallback.value == Decimal("35.00") or fallback.value == Decimal("35")
    assert fallback.cost_basis == Decimal("40.00") or fallback.cost_basis == Decimal("40")


@respx.mock
def test_instrument_lookup_failure_falls_back(monkeypatch):
    _secrets(monkeypatch)
    respx.get(f"{BASE}/trading/info/real/pnl").mock(
        return_value=httpx.Response(200, json=_pnl_body())
    )
    respx.get(f"{BASE}/market-data/instruments").mock(
        return_value=httpx.Response(500, json={"error": "down"})
    )
    respx.get(f"{BASE}/trading/info/trade/history").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = etoro.fetch(date(2026, 11, 1))
    names = {p.name for p in result.positions}
    assert names == {"1001", "2002"}


@respx.mock
def test_history_windows_under_365_days(monkeypatch):
    _secrets(monkeypatch)
    today = date(2026, 11, 17)
    since = today - timedelta(days=400)
    monkeypatch.setattr(etoro, "_today", lambda: today)

    respx.get(f"{BASE}/trading/info/real/pnl").mock(
        return_value=httpx.Response(
            200, json={"clientPortfolio": {"positions": [], "credit": 0}}
        )
    )
    windows: list[tuple[date, date]] = []

    def history(request: httpx.Request) -> httpx.Response:
        mind = date.fromisoformat(request.url.params["minDate"])
        maxd = date.fromisoformat(request.url.params["maxDate"])
        assert request.url.params["pageSize"] == "100"
        assert (maxd - mind).days < 365
        if request.url.params["page"] == "1":
            windows.append((mind, maxd))
            return httpx.Response(
                200,
                json={
                    "clientPortfolio": {
                        "positions": [
                            {
                                "positionId": f"{mind.isoformat()}-1",
                                "instrumentId": 1001,
                                "units": 3,
                                "netProfit": 8.5,
                                "closeTimestamp": f"{mind.isoformat()}T15:00:00Z",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"clientPortfolio": {"positions": []}})

    respx.get(f"{BASE}/trading/info/trade/history").mock(side_effect=history)
    respx.get(f"{BASE}/market-data/instruments").mock(
        return_value=httpx.Response(
            200,
            json=[{"instrumentID": 1001, "instrumentDisplayName": "Apple", "symbolFull": "AAPL"}],
        )
    )

    result = etoro.fetch(since, _sleep=lambda s: (_ for _ in ()).throw(AssertionError("sleep")))
    assert len(windows) >= 2
    assert windows[0][0] == since
    assert windows[-1][1] == today
    for i, (mind, maxd) in enumerate(windows):
        assert (maxd - mind).days <= 364
        if i:
            assert mind == windows[i - 1][1] + timedelta(days=1)
    assert result.transactions
    assert all(t.amount == Decimal("8.5") for t in result.transactions)
    assert result.transactions[0].description.startswith("Fechou Apple")
    assert "3" in result.transactions[0].description
    assert result.transactions[0].ext_id.startswith("close-")
    assert result.transactions[0].booked == since
    assert result.transactions[0].currency == "USD"


@respx.mock
def test_retry_after_429(monkeypatch):
    _secrets(monkeypatch)
    calls = {"n": 0}
    slept: list[float] = []

    def pnl(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow"})
        return httpx.Response(
            200, json={"clientPortfolio": {"positions": [], "credit": "1.00"}}
        )

    respx.get(f"{BASE}/trading/info/real/pnl").mock(side_effect=pnl)
    respx.get(f"{BASE}/trading/info/trade/history").mock(
        return_value=httpx.Response(200, json={"clientPortfolio": {"positions": []}})
    )
    result = etoro.fetch(date(2026, 11, 17), _sleep=lambda s: slept.append(float(s)))
    assert slept == [3.0]
    assert result.balances[0].amount == Decimal("1.00")
    assert calls["n"] == 2


@respx.mock
def test_401_needs_user_action(monkeypatch):
    _secrets(monkeypatch)
    respx.get(f"{BASE}/trading/info/real/pnl").mock(
        return_value=httpx.Response(401, json={"message": "nope"})
    )
    with pytest.raises(NeedsUserAction):
        etoro.fetch(date(2026, 11, 1), _sleep=lambda s: (_ for _ in ()).throw(AssertionError("sleep")))
