"""IBKR Flex: parse do extrato, espera pelo 1019, e 1012 pede ação ao utilizador."""
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from financas.connectors import ibkr
from financas.models import NeedsUserAction

SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
STATEMENT = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"

FLEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="financas" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" fromDate="20241101" toDate="20241117">
      <OpenPositions>
        <OpenPosition symbol="AAPL" description="APPLE INC" position="10" markPrice="180.5"
          positionValue="1805" currency="USD" costBasisMoney="1500.25" fxRateToBase="0.92"
          assetCategory="STK" levelOfDetail="SUMMARY"/>
        <OpenPosition symbol="AAPL" description="APPLE INC LOT" position="4" markPrice="180.5"
          positionValue="722" currency="USD" costBasisMoney="600" fxRateToBase="0.92"
          assetCategory="STK" levelOfDetail="LOT"/>
        <OpenPosition symbol="VWCE" description="VWCE ETF" position="3" markPrice="100"
          positionValue="300" currency="EUR" costBasisMoney="280" assetCategory="STK"/>
      </OpenPositions>
      <CashReport>
        <CashReportCurrency currency="USD" endingCash="1000.50"/>
        <CashReportCurrency currency="EUR" endingCash="200.00"/>
        <CashReportCurrency currency="BASE_SUMMARY" endingCash="1120.46"/>
      </CashReport>
      <Trades>
        <Trade tradeID="T1" tradeDate="20241102" symbol="AAPL" buySell="BUY" quantity="10"
          tradePrice="180" netCash="-1805.20" currency="USD"/>
        <Trade tradeID="T0" tradeDate="2023-01-05" symbol="OLD" buySell="SELL" quantity="1"
          tradePrice="10" netCash="9.50" currency="EUR"/>
      </Trades>
      <CashTransactions>
        <CashTransaction transactionID="C1" dateTime="20241110;153000" amount="12.34"
          currency="USD" description="AAPL DIVIDEND" type="Dividends"/>
        <CashTransaction transactionID="C0" settleDate="20200101" amount="1.00"
          currency="EUR" description="Depósito antigo" type="Deposits/Withdrawals"/>
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

SEND_OK = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse>
  <Status>Success</Status>
  <ReferenceCode>1234567890</ReferenceCode>
</FlexStatementResponse>
"""

XML_1019 = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse>
  <Status>Warn</Status>
  <ErrorCode>1019</ErrorCode>
  <ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>
"""

XML_1012 = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse>
  <Status>Fail</Status>
  <ErrorCode>1012</ErrorCode>
  <ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>
"""


def _patch(monkeypatch, token="TOK", query_id="42"):
    def fake_get(name):
        if name == "ibkr_flex_token":
            return token
        return None

    def fake_section(name):
        if name == "ibkr" and query_id is not None:
            return {"query_id": query_id}
        return {}

    slept = []
    monkeypatch.setattr("financas.secrets.get", fake_get)
    monkeypatch.setattr("financas.config.section", fake_section)
    monkeypatch.setattr("financas.connectors.ibkr._sleep", lambda seconds: slept.append(seconds))
    return slept


def _assert_parsed(result):
    balances = {b.currency: b for b in result.balances}
    assert set(balances) == {"USD", "EUR"}
    assert balances["USD"].amount == Decimal("1000.50")
    assert balances["EUR"].amount == Decimal("200.00")
    assert all(b.kind == "cash" and b.source == "ibkr" and b.account == "U1234567" for b in result.balances)

    positions = {p.symbol: p for p in result.positions}
    assert set(positions) == {"AAPL", "VWCE"}
    aapl = positions["AAPL"]
    assert aapl.name == "APPLE INC"
    assert aapl.quantity == Decimal("10")
    assert aapl.value == Decimal("1805")
    assert aapl.currency == "USD"
    assert aapl.cost_basis == Decimal("1500.25")
    assert aapl.account == "U1234567"
    assert positions["VWCE"].value == Decimal("300")
    assert positions["VWCE"].currency == "EUR"

    txs = {t.ext_id: t for t in result.transactions}
    assert set(txs) == {"T1", "C1"}
    assert txs["T1"].amount == Decimal("-1805.20")
    assert txs["T1"].booked == date(2024, 11, 2)
    assert txs["T1"].currency == "USD"
    assert txs["T1"].description == "BUY 10 AAPL @ 180"
    assert txs["T1"].account == "U1234567"
    assert txs["C1"].amount == Decimal("12.34")
    assert txs["C1"].booked == date(2024, 11, 10)
    assert txs["C1"].description == "AAPL DIVIDEND"
    assert txs["C1"].currency == "USD"


@respx.mock
def test_fetch_interpreta_extrato(monkeypatch):
    _patch(monkeypatch)
    send = respx.get(url__startswith=SEND).mock(return_value=httpx.Response(200, text=SEND_OK))
    respx.get(url__startswith=STATEMENT).mock(return_value=httpx.Response(200, text=FLEX_XML))

    result = ibkr.fetch(date(2024, 11, 1))
    _assert_parsed(result)

    req = send.calls[0].request
    assert req.headers["user-agent"] == "financas/0.1"
    assert req.url.params["t"] == "TOK"
    assert req.url.params["q"] == "42"
    assert req.url.params["v"] == "3"


@respx.mock
def test_1019_espera_e_volta_a_pedir(monkeypatch):
    slept = _patch(monkeypatch)
    respx.get(url__startswith=SEND).mock(return_value=httpx.Response(200, text=SEND_OK))
    statement = respx.get(url__startswith=STATEMENT).mock(
        side_effect=[
            httpx.Response(200, text=XML_1019),
            httpx.Response(200, text=FLEX_XML),
        ]
    )
    result = ibkr.fetch(date(2024, 11, 1))
    _assert_parsed(result)
    assert statement.call_count == 2
    assert statement.calls[0].request.url.params["q"] == "1234567890"
    assert any(s >= 5 for s in slept)


@respx.mock
def test_1012_pede_acao(monkeypatch):
    _patch(monkeypatch)
    respx.get(url__startswith=SEND).mock(return_value=httpx.Response(200, text=XML_1012))
    statement = respx.get(url__startswith=STATEMENT).mock(
        return_value=httpx.Response(200, text=FLEX_XML)
    )
    with pytest.raises(NeedsUserAction, match="[Tt]oken"):
        ibkr.fetch(date(2024, 11, 1))
    assert statement.call_count == 0


def test_sem_token_pede_acao(monkeypatch):
    _patch(monkeypatch, token=None)
    with pytest.raises(NeedsUserAction, match="[Tt]oken"):
        ibkr.fetch(date(2024, 11, 1))


def test_sem_query_id_pede_acao(monkeypatch):
    _patch(monkeypatch, query_id=None)
    with pytest.raises(NeedsUserAction, match="[Qq]uery|consulta"):
        ibkr.fetch(date(2024, 11, 1))


def test_setup_guarda_query_id(monkeypatch):
    saved = {}

    def update(name, values):
        saved["name"] = name
        saved["values"] = values

    monkeypatch.setattr("financas.config.update_section", update)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "987654")
    ibkr.setup()
    assert saved["name"] == "ibkr"
    assert saved["values"] == {"query_id": "987654"}


def test_setup_vazio_nao_grava(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "  ")
    monkeypatch.setattr(
        "financas.config.update_section",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("não devia gravar")),
    )
    with pytest.raises(SystemExit):
        ibkr.setup()


def test_nome_e_segredo():
    assert ibkr.NAME == "ibkr"
    assert ibkr.SECRETS["ibkr_flex_token"] == "Token do Flex Web Service do IBKR"
