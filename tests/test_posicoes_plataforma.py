"""Série mensal por plataforma: depositado, valor reconstruído e o mesmo dinheiro no MSCI World."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from financas import config, store

DDL = """
CREATE TABLE IF NOT EXISTS trades(source TEXT, booked TEXT, symbol TEXT, name TEXT, isin TEXT,
    asset_class TEXT, quantity TEXT, price TEXT, currency TEXT, net_cash TEXT, fx_to_eur TEXT,
    realized_pnl_eur TEXT, ext_id TEXT, PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS tickers(source TEXT, symbol TEXT, yahoo TEXT, currency TEXT,
    PRIMARY KEY(source, symbol));
"""


def _hoje(dia: date):
    class _Dia(date):
        @classmethod
        def today(cls):
            return dia

    return _Dia


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    db = store.connect(tmp_path / "plataforma.db")
    db.executescript(DDL)
    return db


def _trade(db, source, booked, symbol, quantity, net_cash, ext_id, pnl=None):
    db.execute(
        """INSERT INTO trades(source, booked, symbol, name, isin, asset_class, quantity, price,
               currency, net_cash, fx_to_eur, realized_pnl_eur, ext_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source, booked.isoformat(), symbol, "Alfa", "", "acao", str(quantity), "0", "EUR",
         str(net_cash), "1", None if pnl is None else str(pnl), ext_id),
    )


def _fluxo(db, source, booked, amount, kind, ext_id):
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        (source, booked.isoformat(), str(amount), kind, "", ext_id),
    )


def _preco(db, symbol, day, close):
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", (symbol, day.isoformat(), str(close), "EUR"))


def _semear(db):
    _fluxo(db, "ibkr", date(2024, 1, 5), 1000, "deposito", "d1")
    _trade(db, "ibkr", date(2024, 1, 10), "ALFA", 10, -800, "b1")
    _trade(db, "ibkr", date(2024, 2, 10), "ALFA", -4, 400, "s1", pnl=80)
    _fluxo(db, "ibkr", date(2024, 2, 15), 10, "dividendo", "dv1")
    _fluxo(db, "ibkr", date(2024, 3, 5), -100, "levantamento", "l1")
    db.execute("INSERT INTO tickers VALUES('ibkr','ALFA','ALFA.DE','EUR')")
    _preco(db, "ALFA.DE", date(2024, 1, 10), 80)
    _preco(db, "ALFA.DE", date(2024, 2, 1), 90)
    _preco(db, "ALFA.DE", date(2024, 3, 1), 100)
    _preco(db, "IWDA.AS", date(2024, 1, 2), 50)
    _preco(db, "IWDA.AS", date(2024, 2, 1), 55)
    _preco(db, "IWDA.AS", date(2024, 3, 1), 60)


def test_deposito_compra_venda_parcial_preco_e_indice(tmp_path, monkeypatch):
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 3, 15)))
    db = _db(tmp_path, monkeypatch)
    _semear(db)
    db.commit()

    pontos = posicoes.historico_plataforma(db, "ibkr")

    assert [ponto.dia for ponto in pontos] == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 15)]
    janeiro, fevereiro, marco = pontos
    # Janeiro: 200 em caixa + 10 ações a 80.
    assert janeiro.depositado == Decimal(1000)
    assert janeiro.valor == Decimal(1000)
    assert janeiro.indice == Decimal(1000)
    # Fevereiro: venda de 4 por 400 e dividendo de 10; restam 6 ações a 90.
    assert fevereiro.depositado == Decimal(1000)
    assert fevereiro.valor == Decimal(1150)
    assert fevereiro.indice == Decimal(1100)
    # Março: levantamento de 100; 6 ações a 100; o índice vende 100 € a 60.
    assert marco.depositado == Decimal(900)
    assert marco.valor == Decimal(1110)
    assert marco.indice == pytest.approx(Decimal(1100), abs=Decimal("0.000001"))
    assert all(isinstance(ponto.valor, Decimal) for ponto in pontos)


def test_ultimo_ponto_usa_o_snapshot_real(tmp_path, monkeypatch):
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 3, 15)))
    db = _db(tmp_path, monkeypatch)
    _semear(db)
    run = store.new_run(db, at=datetime(2024, 3, 14, 9, 0, 0))
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run, "ibkr", "conta", "EUR", "1234", "investment", "1234"),
    )
    db.commit()

    pontos = posicoes.historico_plataforma(db, "ibkr")

    assert pontos[-1].valor == Decimal(1234)
    assert pontos[-2].valor == Decimal(1150)


def test_sem_precos_do_indice_fica_none(tmp_path, monkeypatch):
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 2, 10)))
    db = _db(tmp_path, monkeypatch)
    _fluxo(db, "aforro", date(2024, 1, 5), 500, "deposito", "a1")
    _fluxo(db, "aforro", date(2024, 2, 1), 5, "juros", "a2")
    db.commit()

    pontos = posicoes.historico_plataforma(db, "aforro")

    assert [(ponto.dia, ponto.depositado, ponto.valor) for ponto in pontos] == [
        (date(2024, 1, 31), Decimal(500), Decimal(500)),
        (date(2024, 2, 10), Decimal(500), Decimal(505)),
    ]
    assert all(ponto.indice is None for ponto in pontos)


def test_total_soma_plataformas_com_depositos_e_lista_as_outras(tmp_path, monkeypatch):
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 3, 15)))
    db = _db(tmp_path, monkeypatch)
    _semear(db)
    _fluxo(db, "aforro", date(2024, 2, 1), 500, "deposito", "a1")
    _fluxo(db, "aforro", date(2024, 3, 1), 5, "juros", "a2")
    run = store.new_run(db, at=datetime(2024, 1, 1, 9, 0, 0))
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run, "bitcoin", "carteira", "BTC", "1", "crypto", "999"),
    )
    db.commit()

    total = posicoes.historico_plataforma(db)

    assert [ponto.dia for ponto in total] == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 15)]
    assert total[0].depositado == Decimal(1000)
    assert total[-1].depositado == Decimal(1400)
    # O bitcoin (sem depósitos) não entra: 1110 da ibkr + 505 do aforro.
    assert total[-1].valor == Decimal(1615)
    esperado = Decimal(1100) + Decimal(500) / Decimal(55) * Decimal(60)
    assert total[-1].indice == pytest.approx(esperado, abs=Decimal("0.000001"))
    assert posicoes.plataformas_sem_depositos(db) == ["bitcoin"]
    assert posicoes.historico_plataforma(db, "bitcoin") == []


def test_frequencia_desconhecida_falha(tmp_path, monkeypatch):
    from financas import posicoes

    db = _db(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        posicoes.historico_plataforma(db, None, freq="ano")
