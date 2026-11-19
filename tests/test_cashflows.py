"""Fluxos de capital: depósito, levantamento, dividendo, juros e custo, em euros."""
from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import httpx
import respx

from financas.fx import Rates
from financas.models import Cashflow

FLEX = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="financas" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U0000001" fromDate="20240101" toDate="20241231">
      <CashReport>
        <CashReportCurrency currency="EUR" endingCash="10.00"/>
      </CashReport>
      <Trades>
        <Trade tradeID="T1" tradeDate="20240302" symbol="VWCE" buySell="BUY" quantity="1"
          tradePrice="100" netCash="-100.00" currency="EUR"/>
      </Trades>
      <CashTransactions>
        <CashTransaction transactionID="DEP" dateTime="20240301" amount="1000.00"
          currency="EUR" description="Entrada" type="Deposits/Withdrawals"/>
        <CashTransaction transactionID="LEV" dateTime="20240401" amount="-250.00"
          currency="EUR" description="Saída" type="Deposits/Withdrawals"/>
        <CashTransaction transactionID="DIV" dateTime="20240501" amount="10"
          currency="USD" description="VWCE dividend" type="Dividends" fxRateToBase="0.90"/>
        <CashTransaction transactionID="PIL" dateTime="20240502" amount="4"
          currency="EUR" description="PIL" type="Payment In Lieu"/>
        <CashTransaction transactionID="JUR" dateTime="20240601" amount="1.50"
          currency="EUR" description="Juro" type="Broker Interest"/>
        <CashTransaction transactionID="TAX" dateTime="20240503" amount="-2"
          currency="USD" description="IRS" type="Withholding Tax" fxRateToBase="0.90"/>
        <CashTransaction transactionID="FEE" dateTime="20240701" amount="-3"
          currency="EUR" description="Comissão" type="Other Fees"/>
        <CashTransaction transactionID="INT" dateTime="20240801" amount="50"
          currency="EUR" description="Transferência interna" type="Internal Transfer"/>
        <CashTransaction transactionID="OLD" settleDate="20200101" amount="9"
          currency="EUR" description="Antigo" type="Deposits/Withdrawals"/>
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

FLEX_SEM_CAMBIO = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U0000001">
      <CashTransactions>
        <CashTransaction transactionID="DIV2" dateTime="20240510" amount="10"
          currency="USD" description="Dividendo sem câmbio" type="Dividends"/>
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


def test_gravar_cashflows_ignora_repetidos(tmp_path):
    from financas import store

    db = store.connect(tmp_path / "f.db")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "cashflows" in tables
    assert "prices" in tables

    fluxo = Cashflow(
        source="ibkr",
        booked=date(2024, 3, 1),
        amount_eur=Decimal("1000.00"),
        kind="deposito",
        description="Entrada",
        ext_id="DEP",
    )
    outro = Cashflow(
        source="etoro",
        booked=date(2024, 3, 1),
        amount_eur=Decimal("5"),
        kind="dividendo",
        description="Outra fonte",
        ext_id="DEP",
    )
    assert store.save_cashflows(db, [fluxo, outro]) == 2
    repetido = Cashflow(
        source="ibkr",
        booked=date(2024, 3, 2),
        amount_eur=Decimal("1"),
        kind="custo",
        description="não substitui",
        ext_id="DEP",
    )
    assert store.save_cashflows(db, [repetido]) == 0
    rows = db.execute(
        "SELECT source, booked, amount_eur, kind, description, ext_id FROM cashflows ORDER BY source"
    ).fetchall()
    assert rows == [
        ("etoro", "2024-03-01", "5", "dividendo", "Outra fonte", "DEP"),
        ("ibkr", "2024-03-01", "1000.00", "deposito", "Entrada", "DEP"),
    ]


def test_ibkr_classifica_e_converte_com_fx_do_extrato():
    from financas.connectors import ibkr

    result = ibkr._parse(ET.fromstring(FLEX), date(2024, 1, 1))
    por_id = {c.ext_id: c for c in result.cashflows}
    assert set(por_id) == {"DEP", "LEV", "DIV", "PIL", "JUR", "TAX", "FEE"}
    assert por_id["DEP"].kind == "deposito"
    assert por_id["DEP"].amount_eur == Decimal("1000.00")
    assert por_id["LEV"].kind == "levantamento"
    assert por_id["LEV"].amount_eur == Decimal("-250.00")
    assert por_id["DIV"].kind == "dividendo"
    assert por_id["DIV"].amount_eur == Decimal("9.00")
    assert por_id["DIV"].description == "VWCE dividend"
    assert por_id["PIL"].kind == "dividendo"
    assert por_id["PIL"].amount_eur == Decimal("4")
    assert por_id["JUR"].kind == "juros"
    assert por_id["JUR"].amount_eur == Decimal("1.50")
    assert por_id["TAX"].kind == "custo"
    assert por_id["TAX"].amount_eur == Decimal("-1.80")
    assert por_id["FEE"].kind == "custo"
    assert por_id["FEE"].amount_eur == Decimal("-3")
    assert all(c.source == "ibkr" and c.booked.year == 2024 for c in result.cashflows)

    txs = {t.ext_id: t for t in result.transactions}
    assert "INT" in txs and "INT" not in por_id
    assert "OLD" not in txs and "OLD" not in por_id
    assert txs["DIV"].currency == "USD"
    assert txs["DIV"].amount == Decimal("10")
    assert txs["T1"].amount == Decimal("-100.00")
    assert result.balances[0].amount == Decimal("10.00")


def test_ibkr_sem_cambio_no_extrato_usa_fx(monkeypatch):
    from financas.connectors import ibkr

    monkeypatch.setattr(
        Rates,
        "fetch",
        classmethod(lambda cls: Rates.from_dict({"USD": "2"})),
    )
    result = ibkr._parse(ET.fromstring(FLEX_SEM_CAMBIO), date(2024, 1, 1))
    assert len(result.cashflows) == 1
    fluxo = result.cashflows[0]
    assert fluxo.kind == "dividendo"
    assert fluxo.amount_eur == Decimal("5")
    assert fluxo.ext_id == "DIV2"
    assert result.transactions[0].currency == "USD"
    assert result.transactions[0].amount == Decimal("10")


@respx.mock
def test_etoro_cashflows_no_historico(monkeypatch):
    from financas.connectors import etoro

    monkeypatch.setattr(
        etoro.secrets,
        "get",
        lambda name: {"etoro_api_key": "pub", "etoro_user_key": "user"}.get(name),
    )
    monkeypatch.setattr(etoro, "_today", lambda: date(2024, 8, 1))
    monkeypatch.setattr(
        Rates,
        "fetch",
        classmethod(lambda cls: Rates.from_dict({"USD": "2"})),
    )
    respx.get("https://public-api.etoro.com/api/v1/trading/info/real/pnl").mock(
        return_value=httpx.Response(200, json={"clientPortfolio": {"credit": 1, "positions": []}})
    )

    def history(request: httpx.Request) -> httpx.Response:
        assert request.url.params["minDate"] == "2024-05-01"
        return httpx.Response(
            200,
            json={
                "clientPortfolio": {
                    "positions": [
                        {
                            "positionId": 77,
                            "instrumentId": 1001,
                            "units": 1,
                            "netProfit": 8,
                            "closeTimestamp": "2024-06-15T12:00:00Z",
                        }
                    ],
                    "cashflows": [
                        {
                            "id": "dep-1",
                            "type": "Deposit",
                            "amount": 10,
                            "date": "2024-05-02T08:00:00Z",
                            "description": "Transferência",
                        },
                        {
                            "id": "wd-1",
                            "type": "Withdrawal",
                            "amount": 4,
                            "date": "2024-06-01",
                            "description": "Levantamento",
                        },
                        {
                            "id": "div-1",
                            "type": "Dividend",
                            "amount": 2,
                            "date": "2024-07-01",
                            "description": "AAPL",
                        },
                        {
                            "id": "old",
                            "type": "Deposit",
                            "amount": 99,
                            "date": "2020-01-01",
                            "description": "Antigo",
                        },
                    ],
                }
            },
        )

    respx.get("https://public-api.etoro.com/api/v1/trading/info/trade/history").mock(side_effect=history)
    respx.get("https://public-api.etoro.com/api/v1/market-data/instruments").mock(
        return_value=httpx.Response(
            200,
            json=[{"instrumentID": 1001, "instrumentDisplayName": "Apple", "symbolFull": "AAPL"}],
        )
    )

    result = etoro.fetch(date(2024, 5, 1), _sleep=lambda _s: None)
    assert len(result.transactions) == 1
    assert result.transactions[0].ext_id == "close-77"
    assert result.transactions[0].currency == "USD"
    assert result.transactions[0].amount == Decimal("8")
    por_id = {c.ext_id: c for c in result.cashflows}
    assert set(por_id) == {"cash-dep-1", "cash-wd-1", "cash-div-1"}
    assert por_id["cash-dep-1"].kind == "deposito"
    assert por_id["cash-dep-1"].amount_eur == Decimal("5")
    assert por_id["cash-dep-1"].booked == date(2024, 5, 2)
    assert por_id["cash-dep-1"].description == "Transferência"
    assert por_id["cash-wd-1"].kind == "levantamento"
    assert por_id["cash-wd-1"].amount_eur == Decimal("-2")
    assert por_id["cash-div-1"].kind == "dividendo"
    assert por_id["cash-div-1"].amount_eur == Decimal("1")
    assert all(c.source == "etoro" for c in result.cashflows)


def test_traderepublic_csv_de_fluxos(monkeypatch):
    from hashlib import sha1
    from pathlib import Path

    import financas.connectors.traderepublic as tr

    csv_tx = """\
Date;Type;Value;Note
2024-01-10;Deposit;500,00;Entrada
2024-02-01;Einzahlung;100,00;Reforço
2024-02-02;Removal;20,00;Saída
2024-02-03;Auszahlung;-15,00;Para o banco
2024-03-01;Dividend;12,50;VWCE
2024-03-02;Interest;1,00;Juros
2024-03-03;Zinsen;0,50;Zins
2024-04-01;Buy;-40,00;Compra
2023-01-01;Deposit;9,00;Antigo
"""
    portfolio = """\
Name;ISIN;quantity;price;avgCost;netValue
Cash;;0;0;0;1,00
"""

    monkeypatch.setattr(tr.shutil, "which", lambda name: "/usr/bin/pytr")
    monkeypatch.setattr(tr.config, "section", lambda name: {})

    def fake_run(cmd, **kwargs):
        if cmd[1] == "portfolio":
            Path(cmd[cmd.index("-o") + 1]).write_text(portfolio, encoding="utf-8")
        else:
            out = Path(cmd[cmd.index("--outputdir") + 1])
            (out / "mov.csv").write_text(csv_tx, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    result = tr.fetch(date(2024, 1, 1))
    assert result.warnings == []
    assert any(t.description.startswith("Buy") for t in result.transactions)
    assert sorted(c.amount_eur for c in result.cashflows if c.kind == "deposito") == [
        Decimal("100.00"),
        Decimal("500.00"),
    ]
    assert sorted(c.amount_eur for c in result.cashflows if c.kind == "levantamento") == [
        Decimal("-20.00"),
        Decimal("-15.00"),
    ]
    dividendo = next(c for c in result.cashflows if c.kind == "dividendo")
    assert dividendo.amount_eur == Decimal("12.50")
    assert dividendo.ext_id == sha1("2024-03-01|Dividend|12,50|VWCE|0".encode()).hexdigest()
    assert sorted(c.amount_eur for c in result.cashflows if c.kind == "juros") == [
        Decimal("0.50"),
        Decimal("1.00"),
    ]
    assert all(c.source == "traderepublic" and c.booked >= date(2024, 1, 1) for c in result.cashflows)
    assert not any(c.booked == date(2023, 1, 1) for c in result.cashflows)
    assert not any(c.kind == "deposito" and c.amount_eur == Decimal("-40") for c in result.cashflows)


def test_aforro_subscricao_e_juros(monkeypatch, tmp_path):
    import financas.connectors.aforro as aforro

    monkeypatch.setattr(aforro.config, "DATA", tmp_path)
    lines = ["meta"]
    dia = date(2023, 1, 2)
    while dia <= date(2024, 6, 20):
        if dia.weekday() < 5:
            lines.append(f"{dia.isoformat()};3,000;")
        dia = date.fromordinal(dia.toordinal() + 1)
    (tmp_path / "euribor3m.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sub = {"serie": "F", "data": "2024-03-12", "valor": 10000}
    monkeypatch.setattr(aforro.config, "section", lambda name: {"subscricoes": [sub]})
    monkeypatch.setattr(aforro, "_today", lambda: date(2024, 6, 12))

    result = aforro.fetch(date(2024, 1, 1))
    _valor, events = aforro.value_at(sub, date(2024, 6, 12), aforro.load_euribor(today=date(2024, 6, 12)))
    depositos = [c for c in result.cashflows if c.kind == "deposito"]
    juros = [c for c in result.cashflows if c.kind == "juros"]
    assert len(depositos) == 1
    assert depositos[0].booked == date(2024, 3, 12)
    assert depositos[0].amount_eur == Decimal("10000")
    assert depositos[0].source == "aforro"
    assert depositos[0].description == "Subscrição CA"
    assert [c.booked for c in juros] == [pay for pay, _net in events]
    assert [c.amount_eur for c in juros] == [net for _pay, net in events]
    assert all(c.amount_eur > 0 for c in juros)
    assert all(t.description == "Juros líquidos CA" for t in result.transactions)
    assert not any(t.description == "Subscrição CA" for t in result.transactions)

    so_juros = aforro.fetch(date(2024, 6, 1))
    assert [c.kind for c in so_juros.cashflows] == ["juros"]


@respx.mock
def test_bitcoin_recebido_e_enviado_em_eur(monkeypatch):
    from financas.connectors import bitcoin

    # O mesmo zpub público de teste usado nos outros testes da carteira.
    zpub = (
        "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
    )
    addr0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    change0 = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
    external = "bc1qexternaladdr000000000000000000000000000"
    tx_in = "11" * 32
    tx_out = "22" * 32

    monkeypatch.setattr("financas.secrets.get", lambda name: zpub if name == "btc_zpub" else None)
    monkeypatch.setattr(bitcoin, "_sleep", lambda *_a, **_k: None)

    recebido = {
        "txid": tx_in,
        "status": {"confirmed": True, "block_time": 1700000000},
        "vin": [{"prevout": {"scriptpubkey_address": external, "value": 200000}}],
        "vout": [
            {"scriptpubkey_address": addr0, "value": 150000},
            {"scriptpubkey_address": external, "value": 40000},
        ],
    }
    enviado = {
        "txid": tx_out,
        "status": {"confirmed": True, "block_time": 1700100000},
        "vin": [{"prevout": {"scriptpubkey_address": addr0, "value": 150000}}],
        "vout": [
            {"scriptpubkey_address": change0, "value": 100000},
            {"scriptpubkey_address": external, "value": 40000},
        ],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "historical-price" in url:
            ts = int(request.url.params["timestamp"])
            assert request.url.params["currency"] == "EUR"
            eur = 30000 if ts == 1700000000 else 40000
            return httpx.Response(200, json={"prices": [{"time": ts, "EUR": eur}]})
        path = request.url.path
        if path.endswith("/txs"):
            addr = path.split("/")[-2]
            if addr == addr0:
                return httpx.Response(200, json=[recebido, enviado])
            return httpx.Response(200, json=[])
        addr = path.rstrip("/").split("/")[-1]
        if addr == addr0:
            body = {
                "chain_stats": {"funded_txo_sum": 150000, "spent_txo_sum": 150000, "tx_count": 2},
                "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
            }
        else:
            body = {
                "chain_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
            }
        return httpx.Response(200, json=body)

    respx.get(url__startswith="https://mempool.space/").mock(side_effect=handle)
    result = bitcoin.fetch(date(2023, 1, 1))
    assert {t.currency for t in result.transactions} == {"BTC"}
    assert result.balances[0].currency == "BTC"
    por_id = {c.ext_id: c for c in result.cashflows}
    assert set(por_id) == {tx_in, tx_out}
    recebido = por_id[tx_in]
    assert recebido.kind == "deposito"
    assert recebido.amount_eur == Decimal("45")
    assert recebido.booked == date(2023, 11, 14)
    assert recebido.description == "Recebido"
    enviado = por_id[tx_out]
    assert enviado.kind == "levantamento"
    assert enviado.amount_eur == Decimal("-20")
    assert enviado.description == "Enviado"
    assert all(c.source == "bitcoin" for c in result.cashflows)
