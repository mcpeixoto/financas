"""IBKR: o erro 1003 num ano que já não é o mais recente encerra o histórico."""
from datetime import date

import httpx
import pytest
import respx

from financas.connectors import ibkr
from financas.models import NeedsUserAction

SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
STATEMENT = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"

XML_1003 = """<?xml version="1.0"?>
<FlexStatementResponse>
  <Status>Fail</Status>
  <ErrorCode>1003</ErrorCode>
  <ErrorMessage>Statement not available.</ErrorMessage>
</FlexStatementResponse>
"""

XML_1012 = """<?xml version="1.0"?>
<FlexStatementResponse>
  <Status>Fail</Status>
  <ErrorCode>1012</ErrorCode>
  <ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>
"""


def _patch(monkeypatch):
    monkeypatch.setattr(
        "financas.secrets.get",
        lambda name: "TOK" if name == "ibkr_flex_token" else None,
    )
    monkeypatch.setattr(
        "financas.config.section",
        lambda name: {"query_id": "42"} if name == "ibkr" else {},
    )
    monkeypatch.setattr("financas.connectors.ibkr._sleep", lambda _seconds: None)


def _send_ok(ref: str) -> str:
    return (
        '<?xml version="1.0"?><FlexStatementResponse><Status>Success</Status>'
        f"<ReferenceCode>{ref}</ReferenceCode></FlexStatementResponse>"
    )


def _extrato(ext_id: str, dia: str) -> str:
    return f"""<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements>
    <FlexStatement accountId="U1" fromDate="{dia}" toDate="{dia}">
      <CashTransactions>
        <CashTransaction transactionID="{ext_id}" dateTime="{dia}" amount="10"
          currency="EUR" description="Entrada" type="Deposits/Withdrawals"/>
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


@respx.mock
def test_1003_no_ano_mais_recente_propaga(monkeypatch):
    _patch(monkeypatch)
    pedidos: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        pedidos.append(request.url.params["fd"])
        return httpx.Response(200, text=XML_1003)

    respx.get(url__startswith=SEND).mock(side_effect=send)
    respx.get(url__startswith=STATEMENT).mock(
        return_value=httpx.Response(200, text=_extrato("C1", "20260115"))
    )

    with pytest.raises(NeedsUserAction, match="1003"):
        ibkr.fetch_history(date(2024, 1, 1), today=date(2026, 11, 19))
    assert pedidos == ["20260101"] * 5  # recua 5 dias antes de desistir


@respx.mock
def test_1003_depois_de_um_ano_com_atividade_para_sem_propagar(monkeypatch):
    _patch(monkeypatch)
    pedidos: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        fd = request.url.params["fd"]
        pedidos.append(fd)
        if fd == "20250101":
            return httpx.Response(200, text=XML_1003)
        return httpx.Response(200, text=_send_ok(fd))

    def statement(request: httpx.Request) -> httpx.Response:
        ref = request.url.params["q"]
        return httpx.Response(200, text=_extrato(f"C{ref}", ref))

    respx.get(url__startswith=SEND).mock(side_effect=send)
    respx.get(url__startswith=STATEMENT).mock(side_effect=statement)

    result = ibkr.fetch_history(date(2024, 6, 1), today=date(2026, 11, 19))

    assert pedidos == ["20260101", "20250101"]
    assert [tx.ext_id for tx in result.transactions] == ["C20260101"]
    assert result.warnings == []


@respx.mock
def test_outro_erro_num_ano_antigo_continua_a_propagar(monkeypatch):
    _patch(monkeypatch)

    def send(request: httpx.Request) -> httpx.Response:
        fd = request.url.params["fd"]
        if fd == "20250101":
            return httpx.Response(200, text=XML_1012)
        return httpx.Response(200, text=_send_ok(fd))

    respx.get(url__startswith=SEND).mock(side_effect=send)
    respx.get(url__startswith=STATEMENT).mock(
        side_effect=lambda request: httpx.Response(
            200, text=_extrato(f"C{request.url.params['q']}", request.url.params["q"])
        )
    )

    with pytest.raises(NeedsUserAction, match="[Tt]oken"):
        ibkr.fetch_history(date(2025, 1, 1), today=date(2026, 11, 19))


@respx.mock
def test_fetch_simples_com_1003_continua_a_propagar(monkeypatch):
    _patch(monkeypatch)
    respx.get(url__startswith=SEND).mock(return_value=httpx.Response(200, text=XML_1003))

    with pytest.raises(NeedsUserAction, match="1003"):
        ibkr.fetch(date(2026, 1, 1))
