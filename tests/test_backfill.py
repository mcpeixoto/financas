"""Backfill: histórico de movimentos e fluxos, sem snapshot nem relatórios."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import respx

from financas.fx import Rates
from financas.models import Balance, Cashflow, FetchResult, Transaction

SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
STATEMENT = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"


def _patch_paths(monkeypatch, tmp_path):
    from financas import config

    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "CONTABILIDADE", tmp_path / "Contabilidade")


def _rates(monkeypatch):
    monkeypatch.setattr(
        Rates,
        "fetch",
        classmethod(lambda cls: Rates.from_dict({"USD": "1", "BTC_EUR": "1"})),
    )


def _fonte(monkeypatch, name, fetch):
    from financas import cli

    module = SimpleNamespace(NAME=name, SECRETS={}, fetch=fetch)
    monkeypatch.setattr(cli, "CONNECTORS", [name])
    monkeypatch.setattr(cli, "load_connector", lambda _name: module)
    return module


def test_backfill_grava_so_movimentos_e_fluxos(monkeypatch, tmp_path):
    from financas import cli, store

    seen = []

    def fetch(since):
        seen.append(since)
        return FetchResult(
            source="santander",
            balances=[
                Balance(
                    source="santander",
                    account="conta",
                    currency="EUR",
                    amount=Decimal("10"),
                    kind="cash",
                )
            ],
            transactions=[
                Transaction(
                    source="santander",
                    account="conta",
                    booked=date(2018, 4, 5),
                    amount=Decimal("-3"),
                    currency="EUR",
                    description="café",
                    ext_id="tx-1",
                )
            ],
            cashflows=[
                Cashflow(
                    source="santander",
                    booked=date(2018, 4, 5),
                    amount_eur=Decimal("100"),
                    kind="deposito",
                    description="Entrada",
                    ext_id="cf-1",
                )
            ],
        )

    _patch_paths(monkeypatch, tmp_path)
    _fonte(monkeypatch, "santander", fetch)
    precos = []
    monkeypatch.setattr(
        "financas.precos.atualizar_benchmark",
        lambda db, symbol="IWDA.UK": precos.append(("bench", symbol)) or 0,
    )
    monkeypatch.setattr(
        "financas.precos.btc_eur_diario",
        lambda db, desde: precos.append(("btc", desde)) or 0,
    )

    assert cli.main(["backfill", "--fonte", "santander", "--desde", "2018-04-05"]) == 0
    assert seen == [date(2018, 4, 5)]
    assert precos == [("bench", "IWDA.UK"), ("btc", date(2018, 4, 5))]

    db = store.connect(tmp_path / "data" / "financas.db")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert db.execute("SELECT ext_id, amount FROM transactions").fetchall() == [("tx-1", "-3")]
    assert db.execute("SELECT ext_id, amount_eur, kind FROM cashflows").fetchall() == [
        ("cf-1", "100", "deposito")
    ]
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "run_status" not in tables
    assert not (tmp_path / "Contabilidade").exists()

    seen.clear()
    precos.clear()
    assert cli.main(["backfill", "--fonte", "santander"]) == 0
    assert seen == [date(2015, 1, 1)]
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM cashflows").fetchone()[0] == 1


def test_run_grava_cashflows_e_continua_a_fazer_relatorios(monkeypatch, tmp_path, capsys):
    from financas import cli, store

    def fetch(since):
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
        )

    _patch_paths(monkeypatch, tmp_path)
    _fonte(monkeypatch, "ok", fetch)
    _rates(monkeypatch)
    monkeypatch.setattr("financas.cli.subprocess.run", lambda *a, **k: None)
    chamadas = []
    monkeypatch.setattr(
        "financas.precos.atualizar_benchmark",
        lambda db, symbol="IWDA.UK": chamadas.append(("bench", symbol)) or 0,
    )
    monkeypatch.setattr(
        "financas.precos.btc_eur_diario",
        lambda db, desde: chamadas.append(("btc", desde)) or 0,
    )

    assert cli.main(["run", "--no-notify", "--since", "2024-02-01", "--only", "ok"]) == 0
    db = store.connect(tmp_path / "data" / "financas.db")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert db.execute("SELECT ext_id FROM transactions").fetchall() == [("tx-9",)]
    assert db.execute("SELECT ext_id, amount_eur FROM cashflows").fetchall() == [("cf-9", "8")]
    md = list((tmp_path / "Contabilidade" / "Investimentos").glob("*_Patrimonio_auto.md"))
    assert len(md) == 1
    assert (tmp_path / "Contabilidade" / "Investimentos" / "Patrimonio_historico.xlsx").is_file()
    assert chamadas == [("bench", "IWDA.UK"), ("btc", date(2024, 2, 1))]
    assert "Relatório:" in capsys.readouterr().out


def _extrato(ext_id: str, dia: str, vazio: bool) -> str:
    if vazio:
        corpo = ""
    else:
        corpo = f"""
          <CashTransactions>
            <CashTransaction transactionID="{ext_id}" dateTime="{dia}" amount="100"
              currency="EUR" description="Entrada" type="Deposits/Withdrawals"/>
          </CashTransactions>
          <OpenPositions>
            <OpenPosition symbol="VWCE" description="ETF" position="1" markPrice="10"
              positionValue="10" currency="EUR" assetCategory="STK"/>
          </OpenPositions>
          <CashReport>
            <CashReportCurrency currency="EUR" endingCash="100"/>
          </CashReport>
        """
    return f"""<?xml version="1.0"?>
    <FlexQueryResponse>
      <FlexStatements count="1">
        <FlexStatement accountId="U0000001">{corpo}</FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """


@respx.mock
def test_ibkr_pede_um_ano_de_cada_vez_e_para_quando_fica_vazio(monkeypatch, tmp_path):
    from financas import cli, config, secrets, store
    import financas.connectors.ibkr as ibkr

    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(ibkr, "_sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(secrets, "get", lambda name: "TOK" if name == "ibkr_flex_token" else None)
    monkeypatch.setattr(config, "section", lambda name: {"query_id": "42"} if name == "ibkr" else {})

    class _Hoje(date):
        @classmethod
        def today(cls):
            return date(2017, 6, 15)

    monkeypatch.setattr(ibkr, "date", _Hoje)
    pedidos: list[tuple[str, str]] = []

    def send(request: httpx.Request) -> httpx.Response:
        assert request.url.params["t"] == "TOK"
        assert request.url.params["q"] == "42"
        assert request.url.params["v"] == "3"
        fd = request.url.params["fd"]
        td = request.url.params["td"]
        pedidos.append((fd, td))
        xml = (
            '<?xml version="1.0"?><FlexStatementResponse><Status>Success</Status>'
            f"<ReferenceCode>{fd}</ReferenceCode></FlexStatementResponse>"
        )
        return httpx.Response(200, text=xml)

    def statement(request: httpx.Request) -> httpx.Response:
        assert "fd" not in request.url.params
        assert "td" not in request.url.params
        ref = request.url.params["q"]
        if ref == "20160101":
            body = _extrato("X", "20160101", vazio=True)
        else:
            body = _extrato(f"C{ref}", ref, vazio=False)
        ET.fromstring(body)
        return httpx.Response(200, text=body)

    respx.get(url__startswith=SEND).mock(side_effect=send)
    respx.get(url__startswith=STATEMENT).mock(side_effect=statement)
    monkeypatch.setattr("financas.precos.atualizar_benchmark", lambda db, symbol="IWDA.UK": 0)
    monkeypatch.setattr("financas.precos.btc_eur_diario", lambda db, desde: 0)

    assert cli.main(["backfill", "--fonte", "ibkr", "--desde", "2015-01-01"]) == 0
    assert pedidos == [("20170101", "20170615"), ("20160101", "20161231")]

    db = store.connect(tmp_path / "data" / "financas.db")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert db.execute("SELECT ext_id, booked, amount FROM transactions").fetchall() == [
        ("C20170101", "2017-01-01", "100")
    ]
    assert db.execute("SELECT ext_id, kind, amount_eur FROM cashflows").fetchall() == [
        ("C20170101", "deposito", "100")
    ]


@respx.mock
def test_falha_de_precos_e_aviso_e_os_fluxos_ficam_gravados(monkeypatch, tmp_path, capsys):
    from financas import cli, store

    def fetch(since):
        return FetchResult(
            source="aforro",
            transactions=[
                Transaction(
                    source="aforro",
                    account="Série F",
                    booked=date(2020, 1, 2),
                    amount=Decimal("1"),
                    currency="EUR",
                    description="Juros líquidos CA",
                    ext_id="j1",
                )
            ],
        )

    _patch_paths(monkeypatch, tmp_path)
    _fonte(monkeypatch, "aforro", fetch)
    respx.get(url__startswith="https://stooq.com/").mock(
        return_value=httpx.Response(503, text="down")
    )
    respx.get(url__startswith="https://mempool.space/").mock(
        return_value=httpx.Response(503, text="down")
    )

    assert cli.main(["backfill", "--fonte", "aforro", "--desde", "2020-01-01"]) == 0
    out = capsys.readouterr().out
    assert "Aviso:" in out
    assert "IWDA" in out
    db = store.connect(tmp_path / "data" / "financas.db")
    assert db.execute("SELECT ext_id FROM transactions").fetchall() == [("j1",)]
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
