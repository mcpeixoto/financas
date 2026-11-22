"""Tickers e cotações diárias das posições, com a Yahoo simulada pelo respx."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import unquote

import httpx
import respx

from financas import store

DDL = """
CREATE TABLE IF NOT EXISTS trades(source TEXT, booked TEXT, symbol TEXT, name TEXT, isin TEXT,
    asset_class TEXT, quantity TEXT, price TEXT, currency TEXT, net_cash TEXT, fx_to_eur TEXT,
    realized_pnl_eur TEXT, ext_id TEXT, PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS tickers(source TEXT, symbol TEXT, yahoo TEXT, currency TEXT,
    PRIMARY KEY(source, symbol));
"""
BUSCA = "https://query2.finance.yahoo.com/v1/finance/search"
GRAFICO = "https://query1.finance.yahoo.com/v8/finance/chart/"


def _db(tmp_path):
    db = store.connect(tmp_path / "p.db")
    db.executescript(DDL)
    return db


def _trade(db, **extra):
    _trade.n += 1
    campos = {
        "source": "ibkr",
        "booked": "2024-03-01",
        "symbol": "AAPL",
        "name": "Apple",
        "isin": "",
        "asset_class": "acao",
        "quantity": "1",
        "price": "10",
        "currency": "USD",
        "net_cash": "-10",
        "fx_to_eur": "0.9",
        "realized_pnl_eur": None,
        "ext_id": f"t{_trade.n}",
    }
    campos.update(extra)
    db.execute(
        """INSERT INTO trades(source, booked, symbol, name, isin, asset_class, quantity, price,
               currency, net_cash, fx_to_eur, realized_pnl_eur, ext_id)
           VALUES(:source,:booked,:symbol,:name,:isin,:asset_class,:quantity,:price,
               :currency,:net_cash,:fx_to_eur,:realized_pnl_eur,:ext_id)""",
        campos,
    )


_trade.n = 0


def _ts(day: str) -> int:
    year, month, dom = (int(part) for part in day.split("-"))
    return int(datetime(year, month, dom, tzinfo=timezone.utc).timestamp())


def _grafico(symbol: str):
    moedas = {"AAPL": "USD", "EURUSD=X": "USD", "EURGBP=X": "GBP", "BTC-USD": "USD", "IWDA.AS": "EUR"}
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": moedas.get(symbol, "EUR")},
                    "timestamp": [_ts("2024-03-01"), _ts("2024-03-04")],
                    "indicators": {
                        "adjclose": [{"adjclose": ["12.5", None]}],
                        "quote": [{"close": ["99", "98"]}],
                    },
                }
            ]
        }
    }


def _simbolo(request: httpx.Request) -> str:
    return unquote(request.url.path.rstrip("/").split("/")[-1])


@respx.mock
def test_isin_resolve_o_ticker_e_a_repeticao_nao_volta_a_procurar(tmp_path):
    from financas.precos import atualizar_precos_posicoes

    db = _db(tmp_path)
    _trade(db, isin="US0378331005", symbol="AAPL")
    _trade(db, isin="EU000FX", symbol="EURUSD", asset_class="fx", ext_id="fx")
    db.commit()

    def procura(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "Mozilla/5.0"
        assert request.url.params["q"] == "US0378331005"
        assert request.url.params["quotesCount"] == "6"
        assert request.url.params["newsCount"] == "0"
        return httpx.Response(200, json={"quotes": [{"symbol": "AAPL", "currency": "USD"}, {"symbol": "OUTRO"}]})

    def grafico(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "Mozilla/5.0"
        assert request.url.params["interval"] == "1d"
        assert request.url.params["period1"] == str(_ts("2024-03-01"))
        period2 = int(request.url.params["period2"])
        assert abs(period2 - int(datetime.now(timezone.utc).timestamp())) < 120
        return httpx.Response(200, json=_grafico(_simbolo(request)))

    busca = respx.get(url__startswith=BUSCA).mock(side_effect=procura)
    respx.get(url__startswith=GRAFICO).mock(side_effect=grafico)

    assert atualizar_precos_posicoes(db) == []
    assert busca.call_count == 1
    assert db.execute("SELECT yahoo, currency FROM tickers WHERE source='ibkr' AND symbol='AAPL'").fetchone() == (
        "AAPL",
        "USD",
    )
    assert db.execute("SELECT COUNT(*) FROM tickers WHERE symbol='EURUSD'").fetchone()[0] == 0
    precos = db.execute("SELECT symbol, day, close, currency FROM prices ORDER BY symbol, day").fetchall()
    assert precos == [
        ("AAPL", "2024-03-01", "12.5", "USD"),
        ("EURGBP=X", "2024-03-01", "12.5", "GBP"),
        ("EURUSD=X", "2024-03-01", "12.5", "USD"),
    ]
    assert Decimal(precos[0][2]) == Decimal("12.5")

    assert atualizar_precos_posicoes(db) == []
    assert busca.call_count == 1


@respx.mock
def test_isin_vazio_grava_ticker_em_branco_e_nao_repete(tmp_path):
    from financas.precos import atualizar_precos_posicoes

    db = _db(tmp_path)
    _trade(db, isin="XX0000000000", symbol="DESCONHECIDO")
    db.commit()
    busca = respx.get(url__startswith=BUSCA).mock(return_value=httpx.Response(200, json={"quotes": []}))
    respx.get(url__startswith=GRAFICO).mock(return_value=httpx.Response(200, json=_grafico("EURUSD=X")))

    avisos = atualizar_precos_posicoes(db)
    assert any("DESCONHECIDO" in aviso for aviso in avisos)
    assert db.execute("SELECT yahoo FROM tickers").fetchone() == ("",)

    assert atualizar_precos_posicoes(db) == []
    assert busca.call_count == 1


@respx.mock
def test_erro_de_rede_na_procura_avisa_e_nao_grava(tmp_path):
    from financas.precos import atualizar_precos_posicoes

    db = _db(tmp_path)
    _trade(db, isin="US0378331005")
    db.commit()
    respx.get(url__startswith=BUSCA).mock(side_effect=httpx.ConnectError("rede"))
    respx.get(url__startswith=GRAFICO).mock(return_value=httpx.Response(200, json=_grafico("EURUSD=X")))

    avisos = atualizar_precos_posicoes(db)

    assert avisos
    assert db.execute("SELECT COUNT(*) FROM tickers").fetchone()[0] == 0


@respx.mock
def test_cotacao_em_falta_avisa_e_continua(tmp_path):
    from financas.precos import atualizar_precos_posicoes

    db = _db(tmp_path)
    _trade(db, symbol="AAPL", isin="", ext_id="a")
    _trade(db, symbol="SEMCOTACAO", isin="", ext_id="s")
    db.commit()

    def grafico(request: httpx.Request) -> httpx.Response:
        if _simbolo(request) == "SEMCOTACAO":
            return httpx.Response(404)
        return httpx.Response(200, json=_grafico(_simbolo(request)))

    respx.get(url__startswith=GRAFICO).mock(side_effect=grafico)

    avisos = atualizar_precos_posicoes(db)

    assert any("SEMCOTACAO" in aviso for aviso in avisos)
    simbolos = {linha[0] for linha in db.execute("SELECT symbol FROM prices")}
    assert "AAPL" in simbolos
    assert "EURUSD=X" in simbolos
    assert "SEMCOTACAO" not in simbolos
    assert db.execute("SELECT yahoo FROM tickers WHERE symbol='SEMCOTACAO'").fetchone() == ("SEMCOTACAO",)


@respx.mock
def test_sem_isin_usa_o_simbolo_e_cripto_ganha_sufixo_usd(tmp_path):
    from financas.precos import atualizar_precos_posicoes

    db = _db(tmp_path)
    _trade(db, symbol="IWDA.AS", isin="", asset_class="etf", ext_id="e", currency="EUR")
    _trade(db, symbol="BTC", isin="", asset_class="cripto", ext_id="c", currency="BTC")
    db.commit()
    respx.get(url__startswith=BUSCA).mock(
        return_value=httpx.Response(200, json={"quotes": [{"symbol": "ERRADO"}]})
    )
    respx.get(url__startswith=GRAFICO).mock(side_effect=lambda request: httpx.Response(200, json=_grafico(_simbolo(request))))

    assert atualizar_precos_posicoes(db) == []
    gravados = dict(db.execute("SELECT symbol, yahoo FROM tickers"))
    assert gravados == {"IWDA.AS": "IWDA.AS", "BTC": "BTC-USD"}
