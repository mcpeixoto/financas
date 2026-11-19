"""IWDA.AS na Yahoo Finance, em euros, como referência do benchmark."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import respx

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/IWDA.AS"


def _ts(day: str) -> int:
    year, month, dom = (int(part) for part in day.split("-"))
    return int(datetime(year, month, dom, tzinfo=timezone.utc).timestamp())


def _grafico(precos, closes=None, com_adjclose=True):
    indicadores = {"quote": [{"close": closes if closes is not None else precos}]}
    if com_adjclose:
        indicadores["adjclose"] = [{"adjclose": precos}]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "EUR"},
                    "timestamp": [_ts("2024-01-02"), _ts("2024-01-03"), _ts("2024-01-04")],
                    "indicators": indicadores,
                }
            ]
        }
    }


@respx.mock
def test_yahoo_grava_iwda_as_em_eur_e_salta_nulls(tmp_path):
    from financas import store
    from financas.precos import atualizar_benchmark

    db = store.connect(tmp_path / "p.db")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "Mozilla/5.0"
        assert request.url.params["interval"] == "1d"
        assert request.url.params["period1"] == str(
            int(datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp())
        )
        period2 = int(request.url.params["period2"])
        assert abs(period2 - int(datetime.now(timezone.utc).timestamp())) < 120
        return httpx.Response(
            200,
            json=_grafico([80.5, None, 81.25], closes=[1, 2, 3]),
        )

    route = respx.get(url__startswith=YAHOO).mock(side_effect=handle)

    assert atualizar_benchmark(db, symbol="IWDA.AS") == 2
    assert atualizar_benchmark(db, symbol="IWDA.AS") == 0
    assert route.call_count == 2
    rows = db.execute(
        "SELECT symbol, day, close, currency FROM prices ORDER BY day"
    ).fetchall()
    assert rows == [
        ("IWDA.AS", "2024-01-02", "80.5", "EUR"),
        ("IWDA.AS", "2024-01-04", "81.25", "EUR"),
    ]
    assert [Decimal(row[2]) for row in rows] == [Decimal("80.5"), Decimal("81.25")]


@respx.mock
def test_yahoo_usa_o_close_quando_o_adjclose_falta(tmp_path):
    from financas import store
    from financas.precos import atualizar_benchmark

    db = store.connect(tmp_path / "p.db")
    respx.get(url__startswith=YAHOO).mock(
        return_value=httpx.Response(
            200,
            json=_grafico(None, closes=[70, None, "71.5"], com_adjclose=False),
        )
    )

    assert atualizar_benchmark(db, symbol="IWDA.AS") == 2
    rows = db.execute("SELECT day, close, currency FROM prices ORDER BY day").fetchall()
    assert rows == [
        ("2024-01-02", "70", "EUR"),
        ("2024-01-04", "71.5", "EUR"),
    ]


def test_benchmark_e_o_dashboard_usam_iwda_as_por_omissao():
    from financas.analise import benchmark
    from financas.dashboard.paginas.investimentos import pagina

    assert inspect.signature(benchmark).parameters["symbol"].default == "IWDA.AS"
    origem = inspect.getsource(pagina)
    assert "IWDA.UK" not in origem


def test_readme_deixa_de_apontar_o_dashboard_para_iwda_uk():
    texto = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
    assert "IWDA.AS" in texto
    assert "IWDA.UK" not in texto.split("## Dashboard", 1)[1].split("## ", 1)[0]
