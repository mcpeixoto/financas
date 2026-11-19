"""Enable Banking: se o banco recusar o date_from, encurta a janela."""
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from financas.connectors import enablebanking

BASE = "https://api.enablebanking.com"
HOJE = date(2026, 11, 19)
SINCE = date(2020, 1, 1)


class _Hoje(date):
    @classmethod
    def today(cls):
        return HOJE


def _patch(monkeypatch):
    monkeypatch.setattr(
        enablebanking.secrets,
        "get",
        lambda name: {
            "enablebanking_app_id": "app-42",
            "enablebanking_private_key": "PEM",
        }.get(name),
    )
    monkeypatch.setattr(enablebanking, "make_jwt", lambda app_id, key: f"jwt-{app_id}")
    monkeypatch.setattr(
        enablebanking.config,
        "section",
        lambda name: {"session_id": "sess-1", "valid_until": "2099-01-01T00:00:00Z"},
    )
    monkeypatch.setattr(enablebanking, "date", _Hoje)


def _candidatos() -> list[date]:
    return [
        SINCE,
        max(SINCE, HOJE - timedelta(days=730)),
        HOJE - timedelta(days=365),
        HOJE - timedelta(days=179),
        HOJE - timedelta(days=89),
    ]


def _instalar_conta():
    respx.get(f"{BASE}/sessions/sess-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "AUTHORIZED",
                "accounts": ["acc-1"],
                "access": {"valid_until": "2099-01-01T00:00:00Z"},
            },
        )
    )
    respx.get(f"{BASE}/accounts/acc-1/details").mock(
        return_value=httpx.Response(200, json={"uid": "acc-1", "name": "Ordem"})
    )
    respx.get(f"{BASE}/accounts/acc-1/balances").mock(
        return_value=httpx.Response(
            200,
            json={
                "balances": [
                    {
                        "balance_type": "CLBD",
                        "balance_amount": {"amount": "10.00", "currency": "EUR"},
                    }
                ]
            },
        )
    )


def _movimento(dia: date, referencia: str = "REF-1") -> dict:
    return {
        "entry_reference": referencia,
        "transaction_amount": {"amount": "15.00", "currency": "EUR"},
        "credit_debit_indicator": "CRDT",
        "booking_date": dia.isoformat(),
        "status": "BOOK",
        "remittance_information": ["Ordenado"],
        "debtor": {"name": "Empresa"},
    }


@respx.mock
@pytest.mark.parametrize("estado", [422, 400])
def test_recusa_do_date_from_encurta_a_janela_e_avisa(monkeypatch, estado):
    _patch(monkeypatch)
    _instalar_conta()
    vistos: list[str] = []
    candidatos = _candidatos()
    efetiva = candidatos[2]

    def transactions(request: httpx.Request) -> httpx.Response:
        vistos.append(request.url.params["date_from"])
        if len(vistos) < 3:
            return httpx.Response(estado, json={"error": "range"})
        return httpx.Response(
            200,
            json={
                "transactions": [
                    _movimento(date(2019, 1, 1), "ANTIGO"),
                    _movimento(efetiva, "REF-1"),
                ],
                "continuation_key": None,
            },
        )

    respx.get(f"{BASE}/accounts/acc-1/transactions").mock(side_effect=transactions)

    result = enablebanking.fetch_bank("santander", SINCE)

    assert vistos == [dia.isoformat() for dia in candidatos[:3]]
    assert [tx.ext_id for tx in result.transactions] == ["REF-1"]
    assert result.transactions[0].booked == efetiva
    assert result.transactions[0].amount == Decimal("15.00")
    assert result.warnings == [
        f"Santander: o banco só deu movimentos desde {efetiva.isoformat()}"
    ]


@respx.mock
def test_erro_que_nao_e_janela_nao_tenta_outras_datas(monkeypatch):
    _patch(monkeypatch)
    _instalar_conta()
    route = respx.get(f"{BASE}/accounts/acc-1/transactions").mock(
        return_value=httpx.Response(500, text="falhou")
    )

    with pytest.raises(httpx.HTTPStatusError):
        enablebanking.fetch_bank("santander", SINCE)
    assert route.call_count == 1
    assert route.calls[0].request.url.params["date_from"] == SINCE.isoformat()
