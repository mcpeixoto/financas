"""Certificados de Aforro: calendário TARGET, taxa, capitalização e Euribor."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from financas.models import NeedsUserAction


def _flat(start: date, end: date, value: str) -> dict[date, Decimal]:
    series = {}
    d = start
    rate = Decimal(value)
    while d <= end:
        series[d] = rate
        d += timedelta(days=1)
    return series


def test_fixing_dates_and_holidays():
    from financas.connectors.aforro import business_days, fixing_date

    # Fevereiro bissexto, sem feriados: 29, 28, 27.
    assert fixing_date(2024, 2) == date(2024, 2, 27)
    # Sexta-feira Santa 29-03-2013 retira o dia que seria o antepenúltimo.
    assert fixing_date(2013, 3) == date(2013, 3, 26)
    # Natal 25 e 26 em dias úteis (quarta e quinta).
    assert fixing_date(2024, 12) == date(2024, 12, 27)
    # 26-12-2022 cai à segunda; 31 é sábado.
    assert fixing_date(2022, 12) == date(2022, 12, 28)
    # 1 de maio no início do mês não muda o fim, mas sai da lista.
    assert fixing_date(2024, 5) == date(2024, 5, 29)

    may = business_days(date(2024, 5, 1), date(2024, 5, 31))
    assert date(2024, 5, 1) not in may
    assert date(2024, 5, 4) not in may  # sábado
    assert date(2024, 5, 6) in may
    assert date(2024, 1, 1) not in business_days(date(2024, 1, 1), date(2024, 1, 31))
    # Sexta-feira Santa e segunda de Páscoa de 2024 (31-03 e 01-04).
    assert date(2024, 3, 29) not in business_days(date(2024, 3, 1), date(2024, 3, 31))
    assert date(2024, 4, 1) not in business_days(date(2024, 4, 1), date(2024, 4, 30))
    assert date(2024, 12, 25) not in business_days(date(2024, 12, 1), date(2024, 12, 31))
    assert date(2024, 12, 26) not in business_days(date(2024, 12, 1), date(2024, 12, 31))


def test_base_rate_floor_cap_and_window():
    from financas.connectors.aforro import base_rate_f

    high = _flat(date(2020, 1, 1), date(2026, 1, 1), "3.0")
    assert base_rate_f(date(2024, 3, 12), high) == Decimal("0.025")

    negative = _flat(date(2020, 1, 1), date(2026, 1, 1), "-1.2")
    assert base_rate_f(date(2024, 3, 1), negative) == Decimal("0")

    # Fixing de fev/2024 = 27-02. Os 10 dias úteis anteriores são estes;
    # o próprio dia de fixing fica de fora (valor 9, que denunciaria a janela).
    window = [
        date(2024, 2, 13),
        date(2024, 2, 14),
        date(2024, 2, 15),
        date(2024, 2, 16),
        date(2024, 2, 19),
        date(2024, 2, 20),
        date(2024, 2, 21),
        date(2024, 2, 22),
        date(2024, 2, 23),
        date(2024, 2, 26),
    ]
    series = _flat(date(2024, 1, 1), date(2024, 3, 31), "9")
    for d in window:
        series[d] = Decimal("2.000")
    series[window[0]] = Decimal("2.005")
    # Média 2.0005 -> 2.001% -> 0.02001.
    assert base_rate_f(date(2024, 3, 1), series) == Decimal("0.02001")


def test_premium_table():
    from financas.connectors.aforro import premium_f

    assert premium_f(1) == Decimal("0")
    assert premium_f(2) == Decimal("0.0025")
    assert premium_f(5) == Decimal("0.0025")
    assert premium_f(6) == Decimal("0.005")
    assert premium_f(9) == Decimal("0.005")
    assert premium_f(10) == Decimal("0.01")
    assert premium_f(11) == Decimal("0.01")
    assert premium_f(12) == Decimal("0.015")
    assert premium_f(13) == Decimal("0.015")
    assert premium_f(14) == Decimal("0.0175")
    assert premium_f(15) == Decimal("0.0175")
    assert premium_f(16) == Decimal("0")


def test_interest_dates_on_the_31st():
    from financas.connectors.aforro import interest_dates

    assert interest_dates(date(2024, 1, 31), date(2025, 5, 1)) == [
        date(2024, 5, 1),
        date(2024, 7, 31),
        date(2024, 10, 31),
        date(2025, 1, 31),
        date(2025, 5, 1),
    ]
    assert interest_dates(date(2024, 8, 31), date(2025, 8, 31)) == [
        date(2024, 12, 1),
        date(2025, 3, 1),
        date(2025, 5, 31),
        date(2025, 8, 31),
    ]
    assert interest_dates(date(2024, 3, 12), date(2024, 6, 11)) == []


def test_serie_f_cap_compounds_one_year():
    from financas.connectors.aforro import value_at

    series = _flat(date(2023, 1, 1), date(2026, 1, 1), "3.0")
    sub = {"serie": "F", "data": "2024-03-12", "valor": 10000}
    value, events = value_at(sub, date(2025, 3, 12), series)
    expected = Decimal("10000") * (1 + Decimal("0.025") / 4 * Decimal("0.72")) ** 4
    assert abs(value - expected) <= Decimal("0.01")
    assert [d for d, _ in events] == [
        date(2024, 6, 12),
        date(2024, 9, 12),
        date(2024, 12, 12),
        date(2025, 3, 12),
    ]
    assert value > Decimal("10000")


def test_serie_f_premium_starts_in_year_two_and_serie_e_cap():
    from financas.connectors.aforro import value_at

    sub = {"serie": "F", "data": "2024-03-12", "valor": 10000}
    value, events = value_at(sub, date(2025, 6, 12), {})
    assert events[3][1] == Decimal("0")
    assert events[4][1] == Decimal("4.5")
    assert value == Decimal("10004.5")

    series = _flat(date(2023, 1, 1), date(2026, 1, 1), "3")
    sub_e = {"serie": "E", "data": "2024-03-12", "valor": 10000}
    value_e, events_e = value_at(sub_e, date(2025, 3, 12), series)
    expected = Decimal("10000") * (1 + Decimal("0.035") / 4 * Decimal("0.72")) ** 4
    assert abs(value_e - expected) <= Decimal("0.01")
    assert len(events_e) == 4


def test_parse_bundesbank_csv_headers_and_comma_decimals():
    from financas.connectors.aforro import parse_bundesbank_csv

    text = """\
BBK_ID;BBIG1.D.D0.EUR.MMKT.EURIBOR.M03.BID._Z
BBK_TITLE;Euribor
TIME_PERIOD;VALUE
2026-11-14;2,779;
2026-11-15;.;
2026-11-16;2,801;
"""
    parsed = parse_bundesbank_csv(text)
    assert parsed[date(2026, 11, 14)] == Decimal("2.779")
    assert parsed[date(2026, 11, 16)] == Decimal("2.801")
    assert date(2026, 11, 15) not in parsed


@respx.mock
def test_euribor_cache_refresh(monkeypatch, tmp_path):
    import financas.connectors.aforro as aforro

    monkeypatch.setattr(aforro.config, "DATA", tmp_path)
    body = "HEADER\nTIME;VAL\n2026-11-14;2,779;\n2026-11-16;2,801;\n"
    route = respx.get(url__startswith="https://api.statistiken.bundesbank.de/").respond(
        200, text=body
    )
    series = aforro.load_euribor(today=date(2026, 11, 17))
    assert series[date(2026, 11, 14)] == Decimal("2.779")
    assert route.called
    cached = (tmp_path / "euribor3m.csv").read_text(encoding="utf-8")
    assert "2,779" in cached

    route.reset()
    again = aforro.load_euribor(today=date(2026, 11, 17))
    assert again[date(2026, 11, 16)] == Decimal("2.801")
    assert not route.called

    # Cache antigo: volta a pedir e junta a linha nova.
    (tmp_path / "euribor3m.csv").write_text(
        "2026-11-01;2,100;\n", encoding="utf-8"
    )
    route.respond(200, text="2026-11-16;2,801;\n")
    merged = aforro.load_euribor(today=date(2026, 11, 17))
    assert merged[date(2026, 11, 1)] == Decimal("2.100")
    assert merged[date(2026, 11, 16)] == Decimal("2.801")
    assert route.called


def test_setup_saves_subscriptions(monkeypatch):
    import financas.connectors.aforro as aforro

    answers = iter(["F", "2024-03-12", "10.000,50", "e", "2023-01-31", "500", ""])
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    saved = {}
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(
        aforro.config, "update_section", lambda name, values: saved.update({name: values})
    )
    aforro.setup()
    subs = saved["aforro"]["subscricoes"]
    assert subs[0]["serie"] == "F"
    assert subs[0]["data"] == "2024-03-12"
    assert Decimal(str(subs[0]["valor"])) == Decimal("10000.50")
    assert subs[1]["serie"] == "E"
    assert subs[1]["valor"] == 500
    assert prompts
    assert all("série" in p.lower() or "data" in p.lower() or "valor" in p.lower() for p in prompts)


def test_fetch_balances_and_transactions(monkeypatch, tmp_path):
    import financas.connectors.aforro as aforro

    monkeypatch.setattr(aforro.config, "DATA", tmp_path)
    lines = ["meta"]
    d = date(2023, 1, 2)
    while d <= date(2025, 3, 14):
        if d.weekday() < 5:
            lines.append(f"{d.isoformat()};3,000;")
        d += timedelta(days=1)
    (tmp_path / "euribor3m.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        aforro.config,
        "section",
        lambda name: {
            "subscricoes": [
                {
                    "serie": "F",
                    "data": "2024-03-12",
                    "valor": 10000,
                    "valor_atual_conhecido": "99999",
                }
            ]
        },
    )
    monkeypatch.setattr(aforro, "_today", lambda: date(2025, 3, 12))

    result = aforro.fetch(date(2024, 12, 1))
    assert len(result.balances) == 1
    bal = result.balances[0]
    assert bal.kind == "savings"
    assert bal.currency == "EUR"
    assert bal.account == "Série F 2024-03-12"
    assert bal.source == "aforro"
    expected = Decimal("10000") * (1 + Decimal("0.025") / 4 * Decimal("0.72")) ** 4
    assert abs(bal.amount - expected) <= Decimal("0.01")
    assert [tx.booked for tx in result.transactions] == [
        date(2024, 12, 12),
        date(2025, 3, 12),
    ]
    assert all(tx.description == "Juros líquidos CA" for tx in result.transactions)
    assert all(tx.amount > 0 for tx in result.transactions)
    assert result.warnings
    assert "difere" in result.warnings[0].lower()


def test_fetch_without_subscriptions(monkeypatch):
    import financas.connectors.aforro as aforro

    monkeypatch.setattr(aforro.config, "section", lambda name: {})
    with pytest.raises(NeedsUserAction):
        aforro.fetch(date(2024, 1, 1))


def test_stale_cache_survives_http_error(monkeypatch, tmp_path):
    import financas.connectors.aforro as aforro

    monkeypatch.setattr(aforro.config, "DATA", tmp_path)
    (tmp_path / "euribor3m.csv").write_text("2020-01-02;1,500;\n", encoding="utf-8")
    with respx.mock:
        respx.get(url__startswith="https://api.statistiken.bundesbank.de/").mock(
            return_value=httpx.Response(503, text="down")
        )
        series = aforro.load_euribor(today=date(2026, 11, 17))
    assert series[date(2020, 1, 2)] == Decimal("1.500")
