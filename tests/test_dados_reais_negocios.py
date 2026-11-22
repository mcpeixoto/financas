"""Falhas vistas com dados reais: extrato IBKR ao fim de semana e posições abertas do eToro."""
from datetime import date

import httpx
import respx

from financas.connectors import etoro, ibkr

SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
STATEMENT = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"

XML_1003 = (
    '<?xml version="1.0"?><FlexStatementResponse><Status>Fail</Status>'
    "<ErrorCode>1003</ErrorCode><ErrorMessage>Statement not available.</ErrorMessage>"
    "</FlexStatementResponse>"
)
EXTRATO = """<?xml version="1.0"?>
<FlexQueryResponse><FlexStatements>
<FlexStatement accountId="U1" fromDate="20260101" toDate="20261120">
<CashTransactions>
<CashTransaction transactionID="C1" dateTime="20260115" amount="10" currency="EUR"
  description="Entrada" type="Deposits/Withdrawals"/>
</CashTransactions>
</FlexStatement></FlexStatements></FlexQueryResponse>
"""


@respx.mock
def test_ibkr_recua_o_fim_do_periodo_ao_fim_de_semana(monkeypatch):
    monkeypatch.setattr("financas.secrets.get", lambda name: "TOK" if name == "ibkr_flex_token" else None)
    monkeypatch.setattr("financas.config.section", lambda name: {"query_id": "42"} if name == "ibkr" else {})
    monkeypatch.setattr("financas.connectors.ibkr._sleep", lambda _seconds: None)
    pedidos: list[tuple[str, str]] = []

    def send(request: httpx.Request) -> httpx.Response:
        fd, td = request.url.params["fd"], request.url.params["td"]
        pedidos.append((fd, td))
        if fd == "20260101" and td > "20261120":
            return httpx.Response(200, text=XML_1003)
        if fd != "20260101":
            return httpx.Response(200, text=XML_1003)
        return httpx.Response(
            200,
            text='<?xml version="1.0"?><FlexStatementResponse><Status>Success</Status>'
            "<ReferenceCode>R1</ReferenceCode></FlexStatementResponse>",
        )

    respx.get(url__startswith=SEND).mock(side_effect=send)
    respx.get(url__startswith=STATEMENT).mock(return_value=httpx.Response(200, text=EXTRATO))

    # Domingo, 22/11/2026: "ontem" é sábado e o último extrato é de sexta.
    result = ibkr.fetch_history(date(2025, 1, 1), today=date(2026, 11, 21))

    assert pedidos[:2] == [("20260101", "20261121"), ("20260101", "20261120")]
    assert [tx.ext_id for tx in result.transactions] == ["C1"]


def test_etoro_posicao_aberta_usa_open_date_time():
    vistos: list[dict] = []
    linha = {
        "positionID": 77,
        "instrumentID": 1111,
        "openDateTime": "2026-09-17T16:39:33.01Z",
        "openRate": "400",
        "units": "1.5",
        "amount": "600",
    }
    meta = {"1111": {"instrumentID": 1111, "instrumentDisplayName": "Exemplo SA", "symbolFull": "EXM", "instrumentTypeID": 5}}

    etoro._compra(linha, meta, lambda campos, pnl: vistos.append(campos))

    assert len(vistos) == 1
    assert vistos[0]["booked"] == date(2026, 9, 17)
    assert vistos[0]["ext_id"] == "77-buy"
    assert vistos[0]["symbol"] == "EXM"
