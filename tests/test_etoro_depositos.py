"""Depósitos manuais da eToro, porque a API pública não os devolve."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import respx

from financas import config
from financas.connectors import etoro

BASE = "https://public-api.etoro.com/api/v1"
AVISO = (
    "eToro: sem depósitos configurados; a rentabilidade do eToro não pode ser calculada "
    "(config etoro.depositos)"
)


def _patch(monkeypatch, depositos):
    monkeypatch.setattr(
        etoro.secrets,
        "get",
        lambda name: {"etoro_api_key": "pub", "etoro_user_key": "user"}.get(name),
    )
    monkeypatch.setattr(etoro, "_today", lambda: date(2026, 11, 19))

    def section(name):
        if name == "etoro":
            return {} if depositos is None else {"depositos": depositos}
        return {}

    monkeypatch.setattr(config, "section", section)


def _rotas():
    respx.get(f"{BASE}/trading/info/real/pnl").mock(
        return_value=httpx.Response(200, json={"clientPortfolio": {"credit": 0, "positions": []}})
    )
    respx.get(f"{BASE}/trading/info/trade/history").mock(
        return_value=httpx.Response(200, json={"clientPortfolio": {"positions": []}})
    )


@respx.mock
def test_depositos_manuais_viram_cashflows(monkeypatch):
    _patch(
        monkeypatch,
        [
            {"data": "2023-12-01", "valor_eur": 900},
            {"data": "2026-02-01", "valor_eur": 1000},
            {"data": "2026-03-03", "valor_eur": -250},
            {"nota": "incompleto"},
        ],
    )
    _rotas()

    result = etoro.fetch(date(2026, 1, 1), _sleep=lambda _s: None)

    assert AVISO not in result.warnings
    manuais = [c for c in result.cashflows if c.ext_id.startswith("manual-")]
    assert [(c.ext_id, c.kind, c.amount_eur, c.booked) for c in manuais] == [
        ("manual-2026-02-01-1000", "deposito", Decimal("1000"), date(2026, 2, 1)),
        ("manual-2026-03-03--250", "levantamento", Decimal("-250"), date(2026, 3, 3)),
    ]
    assert all(c.source == "etoro" for c in manuais)
    assert manuais[0].description == "Depósito manual"
    assert manuais[1].description == "Levantamento manual"


@respx.mock
def test_sem_depositos_configurados_avisa_uma_vez(monkeypatch):
    _patch(monkeypatch, None)
    _rotas()

    result = etoro.fetch(date(2026, 1, 1), _sleep=lambda _s: None)

    assert result.cashflows == []
    assert result.warnings.count(AVISO) == 1


@respx.mock
def test_lista_vazia_de_depositos_avisa_uma_vez(monkeypatch):
    _patch(monkeypatch, [])
    _rotas()

    result = etoro.fetch(date(2026, 1, 1), _sleep=lambda _s: None)

    assert result.warnings.count(AVISO) == 1


def test_readme_documenta_os_depositos_manuais():
    texto = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
    assert "etoro.depositos" in texto
    assert "valor_eur" in texto
