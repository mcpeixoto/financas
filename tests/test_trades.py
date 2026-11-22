"""Negócios: cada compra e venda fica na tabela trades, sem duplicar."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import respx

from financas.fx import Rates
from financas.models import Balance, Cashflow, FetchResult, Trade, Transaction

SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
STATEMENT = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
ETORO = "https://public-api.etoro.com/api/v1"

FLEX = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U0000099" fromDate="20240101" toDate="20240630">
      <Trades>
        <Trade tradeID="COMPRA" tradeDate="20240115" symbol="ALFA" description="EMPRESA ALFA"
          isin="XX0000000001" assetCategory="STK" buySell="BUY" quantity="2"
          tradePrice="400" currency="USD" netCash="-800" fxRateToBase="0.92"
          fifoPnlRealized="0"/>
        <Trade tradeID="VENDA" tradeDate="20240601" symbol="ALFA" description="EMPRESA ALFA"
          isin="XX0000000001" assetCategory="STK" subCategory="COMMON" buySell="SELL"
          quantity="-2" tradePrice="450" currency="USD" netCash="880"
          fxRateToBase="0.90" fifoPnlRealized="30"/>
        <Trade tradeID="CAMBIO" tradeDate="20240301" symbol="EUR.USD" description="EUR.USD"
          assetCategory="CASH" buySell="SELL" quantity="-500" tradePrice="1.1"
          currency="USD" netCash="550" fxRateToBase="0.91" fifoPnlRealized="0"/>
        <Trade tradeID="FUNDO" tradeDate="20240201" symbol="EXETF" description="FUNDO EXEMPLO"
          isin="XX0000000002" assetCategory="STK" subCategory="ETF" buySell="BUY"
          quantity="3" tradePrice="100" currency="EUR" netCash="-301" fxRateToBase="1"
          fifoPnlRealized="0"/>
        <Trade tradeID="OPCAO" tradeDate="20240202" symbol="OPC1" description="OPCAO EXEMPLO"
          assetCategory="OPT" buySell="BUY" quantity="1" tradePrice="5"
          currency="EUR" netCash="-5" fxRateToBase="1" fifoPnlRealized="0"/>
        <Trade tradeID="ANTIGO" tradeDate="20200101" symbol="VELHO" description="ANTIGO"
          assetCategory="STK" buySell="BUY" quantity="1" tradePrice="1"
          currency="EUR" netCash="-1" fxRateToBase="1" fifoPnlRealized="0"/>
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

SEND_OK = """<?xml version="1.0"?>
<FlexStatementResponse>
  <Status>Success</Status>
  <ReferenceCode>REF1</ReferenceCode>
</FlexStatementResponse>
"""


def _negocio(**kwargs) -> Trade:
    base = dict(
        source="ibkr",
        booked=date(2024, 1, 15),
        symbol="ALFA",
        name="EMPRESA ALFA",
        isin="XX0000000001",
        asset_class="acao",
        quantity=Decimal("2"),
        price=Decimal("400"),
        currency="USD",
        net_cash=Decimal("-800"),
        fx_to_eur=Decimal("0.92"),
        realized_pnl_eur=None,
        ext_id="COMPRA",
    )
    base.update(kwargs)
    return Trade(**base)


def _patch_ibkr(monkeypatch):
    import financas.connectors.ibkr as ibkr

    monkeypatch.setattr(ibkr.secrets, "get", lambda name: "TOK" if name == "ibkr_flex_token" else None)
    monkeypatch.setattr(ibkr.config, "section", lambda name: {"query_id": "42"} if name == "ibkr" else {})
    monkeypatch.setattr(ibkr, "_sleep", lambda *_a, **_k: None)


@respx.mock
def test_ibkr_compra_venda_cambio_e_classe(monkeypatch):
    """Uma venda em dólares converte o fifoPnlRealized com fxRateToBase; CASH é câmbio."""
    import financas.connectors.ibkr as ibkr

    _patch_ibkr(monkeypatch)
    respx.get(url__startswith=SEND).mock(return_value=httpx.Response(200, text=SEND_OK))
    respx.get(url__startswith=STATEMENT).mock(return_value=httpx.Response(200, text=FLEX))

    result = ibkr.fetch(date(2024, 1, 1))
    por_id = {t.ext_id: t for t in result.trades}
    assert set(por_id) == {"COMPRA", "VENDA", "CAMBIO", "FUNDO", "OPCAO"}

    compra = por_id["COMPRA"]
    assert compra.source == "ibkr"
    assert compra.booked == date(2024, 1, 15)
    assert compra.symbol == "ALFA"
    assert compra.name == "EMPRESA ALFA"
    assert compra.isin == "XX0000000001"
    assert compra.asset_class == "acao"
    assert compra.quantity == Decimal("2")
    assert compra.price == Decimal("400")
    assert compra.currency == "USD"
    assert compra.net_cash == Decimal("-800")
    assert compra.fx_to_eur == Decimal("0.92")
    assert compra.realized_pnl_eur is None

    venda = por_id["VENDA"]
    assert venda.quantity == Decimal("-2")
    assert venda.price == Decimal("450")
    assert venda.net_cash == Decimal("880")
    assert venda.fx_to_eur == Decimal("0.90")
    assert venda.realized_pnl_eur == Decimal("27")
    assert venda.asset_class == "acao"
    assert venda.booked == date(2024, 6, 1)

    cambio = por_id["CAMBIO"]
    assert cambio.asset_class == "fx"
    assert cambio.symbol == "EUR.USD"
    assert cambio.quantity == Decimal("-500")
    assert cambio.fx_to_eur == Decimal("0.91")
    assert cambio.realized_pnl_eur == Decimal("0")

    assert por_id["FUNDO"].asset_class == "etf"
    assert por_id["FUNDO"].isin == "XX0000000002"
    assert por_id["FUNDO"].realized_pnl_eur is None
    assert por_id["OPCAO"].asset_class == "outro"

    movimentos = {t.ext_id: t for t in result.transactions}
    assert movimentos["COMPRA"].amount == Decimal("-800")
    assert movimentos["COMPRA"].description == "BUY 2 ALFA @ 400"
    assert movimentos["VENDA"].amount == Decimal("880")
    assert "ANTIGO" not in movimentos


@respx.mock
def test_ibkr_historico_junta_os_negocios_de_cada_ano(monkeypatch):
    import financas.connectors.ibkr as ibkr

    _patch_ibkr(monkeypatch)

    def send(request: httpx.Request) -> httpx.Response:
        fd = request.url.params["fd"]
        xml = (
            '<?xml version="1.0"?><FlexStatementResponse><Status>Success</Status>'
            f"<ReferenceCode>{fd}</ReferenceCode></FlexStatementResponse>"
        )
        return httpx.Response(200, text=xml)

    def statement(request: httpx.Request) -> httpx.Response:
        ref = request.url.params["q"]
        if ref == "20150101":
            corpo = ""
        else:
            corpo = f"""
            <Trades>
              <Trade tradeID="T{ref}" tradeDate="{ref}" symbol="BETA" description="BETA SA"
                assetCategory="STK" buySell="BUY" quantity="1" tradePrice="10"
                currency="EUR" netCash="-10" fxRateToBase="1" fifoPnlRealized="0"/>
            </Trades>
            """
        body = f"""<?xml version="1.0"?>
        <FlexQueryResponse>
          <FlexStatements>
            <FlexStatement accountId="U0000001">{corpo}</FlexStatement>
          </FlexStatements>
        </FlexQueryResponse>
        """
        ET.fromstring(body)
        return httpx.Response(200, text=body)

    respx.get(url__startswith=SEND).mock(side_effect=send)
    respx.get(url__startswith=STATEMENT).mock(side_effect=statement)

    result = ibkr.fetch_history(date(2015, 1, 1), today=date(2017, 6, 15))
    assert {t.ext_id for t in result.trades} == {"T20170101", "T20160101"}
    assert {t.ext_id for t in result.transactions} == {"T20170101", "T20160101"}
    assert all(t.asset_class == "acao" and t.fx_to_eur == Decimal("1") for t in result.trades)


def _patch_etoro(monkeypatch, today: date):
    import financas.connectors.etoro as etoro

    monkeypatch.setattr(
        etoro.secrets,
        "get",
        lambda name: {"etoro_api_key": "pub", "etoro_user_key": "user"}.get(name),
    )
    monkeypatch.setattr(etoro, "_today", lambda: today)
    monkeypatch.setattr(etoro.config, "section", lambda name: {})
    monkeypatch.setattr(
        Rates,
        "fetch",
        classmethod(lambda cls: Rates.from_dict({"USD": "2"})),
    )


def _rotas_etoro(estado: dict) -> None:
    def pnl(_request: httpx.Request) -> httpx.Response:
        abertas = []
        if estado["aberto"]:
            abertas.append(
                {
                    "positionID": 501,
                    "instrumentID": 100000,
                    "openTimestamp": "2024-02-01T10:00:00Z",
                    "openRate": "42000",
                    "units": "0.01",
                    "amount": "420",
                    "isBuy": True,
                }
            )
        return httpx.Response(
            200,
            json={"clientPortfolio": {"credit": "100", "positions": abertas}},
        )

    def history(_request: httpx.Request) -> httpx.Response:
        linhas = [
            {
                "positionID": 502,
                "instrumentID": 1001,
                "openTimestamp": "2023-11-01T00:00:00Z",
                "closeTimestamp": "2024-03-10T00:00:00Z",
                "openRate": "100",
                "closeRate": "120",
                "units": "2",
                "investment": "200",
                "netProfit": "38",
            },
            {
                "positionID": 503,
                "instrumentID": 3003,
                "openTimestamp": "2024-02-02T00:00:00Z",
                "closeTimestamp": "2024-03-02T00:00:00Z",
                "units": "4",
                "investment": "80",
                "netProfit": "20",
            },
        ]
        if not estado["aberto"]:
            linhas.append(
                {
                    "positionID": 501,
                    "instrumentID": 100000,
                    "openTimestamp": "2024-02-01T10:00:00Z",
                    "closeTimestamp": "2024-03-20T00:00:00Z",
                    "openRate": "42000",
                    "closeRate": "43000",
                    "units": "0.01",
                    "amount": "420",
                    "netProfit": "8",
                }
            )
        return httpx.Response(200, json={"positions": linhas})

    respx.get(f"{ETORO}/trading/info/real/pnl").mock(side_effect=pnl)
    respx.get(f"{ETORO}/trading/info/trade/history").mock(side_effect=history)
    respx.get(f"{ETORO}/market-data/instruments").mock(
        return_value=httpx.Response(
            200,
            json={
                "instrumentDisplayDatas": [
                    {
                        "instrumentID": 100000,
                        "instrumentDisplayName": "Bitcoin",
                        "symbolFull": "BTC",
                        "instrumentTypeID": 10,
                    },
                    {
                        "instrumentID": 1001,
                        "instrumentDisplayName": "Apple",
                        "symbolFull": "AAPL",
                        "instrumentTypeID": 5,
                    },
                    {
                        "instrumentID": 3003,
                        "instrumentDisplayName": "Vanguard",
                        "symbolFull": "VWCE",
                        "instrumentTypeID": 6,
                    },
                ]
            },
        )
    )


@respx.mock
def test_etoro_nomes_e_compra_que_nao_duplica(monkeypatch, tmp_path):
    import financas.connectors.etoro as etoro
    from financas import store

    _patch_etoro(monkeypatch, date(2024, 4, 1))
    estado = {"aberto": True}
    _rotas_etoro(estado)

    result = etoro.fetch(date(2024, 1, 1), _sleep=lambda _s: None)
    bitcoin = next(p for p in result.positions if p.symbol == "BTC")
    assert bitcoin.name == "Bitcoin"

    por_id = {t.ext_id: t for t in result.trades}
    assert len(result.trades) == 5
    assert set(por_id) == {"501-buy", "502-buy", "502-sell", "503-buy", "503-sell"}

    compra_aberta = por_id["501-buy"]
    assert compra_aberta.booked == date(2024, 2, 1)
    assert compra_aberta.symbol == "BTC"
    assert compra_aberta.name == "Bitcoin"
    assert compra_aberta.asset_class == "cripto"
    assert compra_aberta.quantity == Decimal("0.01")
    assert compra_aberta.price == Decimal("42000")
    assert compra_aberta.currency == "USD"
    assert compra_aberta.net_cash == Decimal("-420")
    assert compra_aberta.fx_to_eur == Decimal("0.5")
    assert compra_aberta.realized_pnl_eur is None

    assert por_id["502-buy"].booked == date(2023, 11, 1)
    assert por_id["502-buy"].symbol == "AAPL"
    assert por_id["502-buy"].name == "Apple"
    assert por_id["502-buy"].asset_class == "acao"
    assert por_id["502-buy"].quantity == Decimal("2")
    assert por_id["502-buy"].price == Decimal("100")
    assert por_id["502-buy"].net_cash == Decimal("-200")
    assert por_id["502-buy"].realized_pnl_eur is None

    venda = por_id["502-sell"]
    assert venda.booked == date(2024, 3, 10)
    assert venda.quantity == Decimal("-2")
    assert venda.price == Decimal("120")
    assert venda.net_cash == Decimal("238")
    assert venda.realized_pnl_eur == Decimal("19")
    assert venda.currency == "USD"
    assert venda.fx_to_eur == Decimal("0.5")

    assert por_id["503-buy"].price == Decimal("20")
    assert por_id["503-buy"].asset_class == "etf"
    assert por_id["503-buy"].symbol == "VWCE"
    assert por_id["503-sell"].price == Decimal("25")
    assert por_id["503-sell"].quantity == Decimal("-4")
    assert por_id["503-sell"].net_cash == Decimal("100")
    assert por_id["503-sell"].realized_pnl_eur == Decimal("10")

    db = store.connect(tmp_path / "financas.db")
    assert store.save_trades(db, result.trades) == 5
    estado["aberto"] = False
    fechada = etoro.fetch(date(2024, 1, 1), _sleep=lambda _s: None)
    ids = {t.ext_id for t in fechada.trades}
    assert "501-buy" in ids and "501-sell" in ids
    assert sum(1 for t in fechada.trades if t.ext_id == "501-buy") == 1
    assert store.save_trades(db, fechada.trades) == 1
    assert db.execute("SELECT COUNT(*) FROM trades WHERE ext_id='501-buy'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 6


@respx.mock
def test_bitcoin_recebido_e_compra_enviado_e_venda(monkeypatch):
    import financas.connectors.bitcoin as bitcoin

    zpub = (
        "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
    )
    addr0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    change0 = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
    external = "bc1qzz0000000000000000000000000000000000"
    tx_in = "aa" * 32
    tx_out = "bb" * 32

    monkeypatch.setattr(bitcoin.secrets, "get", lambda name: zpub if name == "btc_zpub" else None)
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
            assert request.url.params["currency"] == "EUR"
            ts = int(request.url.params["timestamp"])
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
    por_id = {t.ext_id: t for t in result.trades}
    assert set(por_id) == {tx_in, tx_out}

    compra = por_id[tx_in]
    assert compra.source == "bitcoin"
    assert compra.booked == date(2023, 11, 14)
    assert compra.symbol == "BTC"
    assert compra.name == "Bitcoin"
    assert compra.asset_class == "cripto"
    assert compra.quantity == Decimal("0.0015")
    assert compra.price == Decimal("30000")
    assert compra.currency == "EUR"
    assert compra.net_cash == Decimal("-45")
    assert compra.fx_to_eur == Decimal("1")
    assert compra.realized_pnl_eur is None

    venda = por_id[tx_out]
    assert venda.booked == date(2023, 11, 16)
    assert venda.quantity == Decimal("-0.0005")
    assert venda.price == Decimal("40000")
    assert venda.net_cash == Decimal("20")
    assert venda.realized_pnl_eur is None
    assert venda.currency == "EUR"
    assert venda.fx_to_eur == Decimal("1")

    fluxos = {c.ext_id: c for c in result.cashflows}
    assert fluxos[tx_in].amount_eur == Decimal("45")
    assert fluxos[tx_out].amount_eur == Decimal("-20")


def test_save_trades_ignora_repetidos_e_cria_tabelas(tmp_path):
    from financas import store

    db = store.connect(tmp_path / "financas.db")
    tabelas = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trades", "tickers"} <= tabelas
    colunas = [row[1] for row in db.execute("PRAGMA table_info(trades)")]
    assert colunas == [
        "source", "booked", "symbol", "name", "isin", "asset_class", "quantity", "price",
        "currency", "net_cash", "fx_to_eur", "realized_pnl_eur", "ext_id",
    ]
    tickers = [row[1] for row in db.execute("PRAGMA table_info(tickers)")]
    assert tickers == ["source", "symbol", "yahoo", "currency"]

    primeiro = _negocio(realized_pnl_eur=None)
    venda = _negocio(
        booked=date(2024, 6, 1),
        quantity=Decimal("-2"),
        price=Decimal("450"),
        net_cash=Decimal("880"),
        fx_to_eur=Decimal("0.90"),
        realized_pnl_eur=Decimal("27"),
        ext_id="VENDA",
    )
    outra_fonte = _negocio(source="etoro", ext_id="COMPRA")
    assert store.save_trades(db, [primeiro, venda]) == 2
    assert store.save_trades(db, [primeiro, outra_fonte]) == 1
    assert store.save_trades(db, [venda]) == 0

    linhas = db.execute(
        "SELECT source, ext_id, quantity, realized_pnl_eur FROM trades ORDER BY source, ext_id"
    ).fetchall()
    assert linhas == [
        ("etoro", "COMPRA", "2", None),
        ("ibkr", "COMPRA", "2", None),
        ("ibkr", "VENDA", "-2", "27"),
    ]


def _patch_cli(monkeypatch, tmp_path, fetch):
    from financas import cli, config

    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "CONTABILIDADE", tmp_path / "Contabilidade")
    module = SimpleNamespace(NAME="ok", SECRETS={}, fetch=fetch)
    monkeypatch.setattr(cli, "CONNECTORS", ["ok"])
    monkeypatch.setattr(cli, "load_connector", lambda _name: module)
    monkeypatch.setattr(
        Rates,
        "fetch",
        classmethod(lambda cls: Rates.from_dict({"USD": "1", "BTC_EUR": "1"})),
    )
    monkeypatch.setattr("financas.precos.atualizar_benchmark", lambda db, symbol="IWDA.UK": 0)
    monkeypatch.setattr("financas.precos.btc_eur_diario", lambda db, desde: 0)
    monkeypatch.setattr("financas.cli.subprocess.run", lambda *a, **k: None)


def _resultado_com_negocio() -> FetchResult:
    return FetchResult(
        source="ok",
        balances=[
            Balance(source="ok", account="conta", currency="EUR", amount=Decimal("8"), kind="cash")
        ],
        transactions=[
            Transaction(
                source="ok",
                account="conta",
                booked=date(2024, 2, 2),
                amount=Decimal("-1"),
                currency="EUR",
                description="pão",
                ext_id="tx-9",
            )
        ],
        cashflows=[
            Cashflow(
                source="ok",
                booked=date(2024, 2, 2),
                amount_eur=Decimal("8"),
                kind="deposito",
                description="Entrada",
                ext_id="cf-9",
            )
        ],
        trades=[_negocio(source="ok", ext_id="ng-1", currency="EUR", fx_to_eur=Decimal("1"))],
    )


def test_run_grava_negocios(monkeypatch, tmp_path, capsys):
    from financas import cli, store

    _patch_cli(monkeypatch, tmp_path, lambda since: _resultado_com_negocio())
    assert cli.main(["run", "--no-notify", "--since", "2024-02-01", "--only", "ok"]) == 0
    db = store.connect(tmp_path / "data" / "financas.db")
    assert db.execute("SELECT source, ext_id, symbol FROM trades").fetchall() == [("ok", "ng-1", "ALFA")]
    assert db.execute("SELECT ext_id, amount_eur FROM cashflows").fetchall() == [("cf-9", "8")]
    assert "Relatório:" in capsys.readouterr().out


def test_backfill_grava_negocios_sem_corrida(monkeypatch, tmp_path):
    from financas import cli, store

    _patch_cli(monkeypatch, tmp_path, lambda since: _resultado_com_negocio())
    assert cli.main(["backfill", "--fonte", "ok", "--desde", "2024-02-01"]) == 0
    db = store.connect(tmp_path / "data" / "financas.db")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert db.execute("SELECT source, ext_id, net_cash FROM trades").fetchall() == [("ok", "ng-1", "-800")]
    assert db.execute("SELECT ext_id FROM transactions").fetchall() == [("tx-9",)]
    assert db.execute("SELECT ext_id FROM cashflows").fetchall() == [("cf-9",)]
