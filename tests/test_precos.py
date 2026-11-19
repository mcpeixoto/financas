"""Cotações de referência: IWDA na Stooq e Bitcoin em euros na mempool."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx

STOOQ = "https://stooq.com/q/d/l/?s=iwda.uk&i=d"
CSV = """Date,Open,High,Low,Close,Volume
2024-01-02,90.00,91.00,89.00,90.50,1000
2024-01-03,90.50,92.00,90.00,91.25,1100
lixo,nao,e,uma,data,0
"""


def _meio_dia(day: date) -> str:
    instant = datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)
    return str(int(instant.timestamp()))


@respx.mock
def test_stooq_grava_iwda_em_usd_e_nao_duplica(tmp_path):
    from financas import store
    from financas.precos import atualizar_benchmark

    db = store.connect(tmp_path / "p.db")
    route = respx.get(STOOQ).mock(return_value=httpx.Response(200, text=CSV))

    assert atualizar_benchmark(db) == 2
    assert atualizar_benchmark(db, symbol="IWDA.UK") == 0
    assert route.call_count == 2
    rows = db.execute(
        "SELECT symbol, day, close, currency FROM prices ORDER BY day"
    ).fetchall()
    assert rows == [
        ("IWDA.UK", "2024-01-02", "90.50", "USD"),
        ("IWDA.UK", "2024-01-03", "91.25", "USD"),
    ]


@respx.mock
def test_btc_eur_diario_e_dedupe(monkeypatch, tmp_path):
    from financas import store
    from financas.precos import btc_eur_diario

    class _Hoje(date):
        @classmethod
        def today(cls):
            return date(2024, 1, 3)

    monkeypatch.setattr("financas.precos.date", _Hoje)
    db = store.connect(tmp_path / "p.db")
    pedidos: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        ts = request.url.params["timestamp"]
        pedidos.append(ts)
        assert request.url.params["currency"] == "EUR"
        return httpx.Response(200, json={"prices": [{"time": int(ts), "EUR": 100 + len(pedidos)}]})

    route = respx.get(url__startswith="https://mempool.space/api/v1/historical-price").mock(
        side_effect=handle
    )
    desde = date(2024, 1, 1)
    assert btc_eur_diario(db, desde) == 3
    assert pedidos == [_meio_dia(date(2024, 1, d)) for d in (1, 2, 3)]
    assert btc_eur_diario(db, desde) == 0
    assert route.call_count == 3
    rows = db.execute(
        "SELECT symbol, day, close, currency FROM prices ORDER BY day"
    ).fetchall()
    assert [row[0] for row in rows] == ["BTC", "BTC", "BTC"]
    assert [row[1] for row in rows] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert [row[3] for row in rows] == ["EUR", "EUR", "EUR"]
    assert [Decimal(row[2]) for row in rows] == [Decimal("101"), Decimal("102"), Decimal("103")]


@respx.mock
def test_stooq_invalida_nao_grava(tmp_path):
    from financas import store
    from financas.precos import atualizar_benchmark

    db = store.connect(tmp_path / "p.db")
    respx.get(STOOQ).mock(return_value=httpx.Response(200, text="<html>verificação</html>"))
    with pytest.raises(Exception):
        atualizar_benchmark(db)
    assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
