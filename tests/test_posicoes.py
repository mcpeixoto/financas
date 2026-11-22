"""Posições abertas, realizados e histórico reconstruído a partir de negócios sintéticos."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from financas import store

DDL = """
CREATE TABLE IF NOT EXISTS trades(source TEXT, booked TEXT, symbol TEXT, name TEXT, isin TEXT,
    asset_class TEXT, quantity TEXT, price TEXT, currency TEXT, net_cash TEXT, fx_to_eur TEXT,
    realized_pnl_eur TEXT, ext_id TEXT, PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS tickers(source TEXT, symbol TEXT, yahoo TEXT, currency TEXT,
    PRIMARY KEY(source, symbol));
"""


def _db(tmp_path, nome="p.db"):
    db = store.connect(tmp_path / nome)
    db.executescript(DDL)
    return db


def _posicao(db, run_id, source, symbol, name, quantity, value, value_eur, cost_basis, currency="EUR"):
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            source,
            "conta",
            symbol,
            name,
            str(quantity),
            str(value),
            currency,
            None if cost_basis is None else str(cost_basis),
            str(value_eur),
        ),
    )


def _trade(
    db,
    source,
    booked,
    symbol,
    name,
    quantity,
    net_cash,
    ext_id,
    asset_class="acao",
    currency="EUR",
    fx_to_eur="1",
    realized_pnl_eur=None,
    isin="",
    price="0",
):
    db.execute(
        """INSERT INTO trades(source, booked, symbol, name, isin, asset_class, quantity, price,
               currency, net_cash, fx_to_eur, realized_pnl_eur, ext_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source,
            booked.isoformat(),
            symbol,
            name,
            isin,
            asset_class,
            str(quantity),
            str(price),
            currency,
            str(net_cash),
            str(fx_to_eur),
            None if realized_pnl_eur is None else str(realized_pnl_eur),
            ext_id,
        ),
    )


def _fluxo(db, source, booked, amount, kind, description, ext_id):
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        (source, booked.isoformat(), str(amount), kind, description, ext_id),
    )


def _preco(db, symbol, day, close, currency="EUR"):
    db.execute(
        "INSERT INTO prices VALUES(?,?,?,?)",
        (symbol, day.isoformat(), str(close), currency),
    )


def _hoje(dia: date):
    class _Dia(date):
        @classmethod
        def today(cls):
            return dia

    return _Dia


def test_posicoes_agregam_etoro_e_ignoram_o_snapshot_antigo(tmp_path):
    """Várias linhas do mesmo instrumento somam-se; o custo passa a euros pelo rácio do valor."""
    from financas.posicoes import posicoes_atuais

    db = _db(tmp_path)
    antigo = store.new_run(db, at=datetime(2024, 1, 1, 9, 0, 0))
    _posicao(db, antigo, "etoro", "AAPL", "Antiga", 99, 9999, 9999, 9999, currency="USD")
    recente = store.new_run(db, at=datetime(2024, 6, 1, 9, 0, 0))
    _posicao(db, recente, "etoro", "AAPL", "Apple", 2, 100, 80, 50, currency="USD")
    _posicao(db, recente, "etoro", "AAPL", "Apple", 3, 150, 120, 70, currency="USD")
    _posicao(db, recente, "etoro", "AAPL", "Apple", 1, 50, 40, 30, currency="USD")
    _posicao(db, recente, "etoro", "MSFT", "Microsoft", 1, 100, 100, None)
    _posicao(db, recente, "ibkr", "VWCE", "VWCE ETF", 4, 400, 400, 200)
    _trade(db, "etoro", date(2024, 1, 2), "AAPL", "Velha", 1, -10, "a1", asset_class="outro")
    _trade(db, "etoro", date(2024, 5, 2), "AAPL", "Maçã", -1, 10, "a2", realized_pnl_eur=4)
    _trade(db, "etoro", date(2024, 5, 3), "AAPL", "Maçã", -1, 10, "a3", realized_pnl_eur=6)
    _trade(db, "etoro", date(2024, 5, 4), "AAPL", "Maçã", -1, 10, "a4", realized_pnl_eur=None)
    _trade(db, "ibkr", date(2024, 5, 1), "VWCE", "Vanguard", 4, -200, "v1", asset_class="etf", realized_pnl_eur=-2)
    _fluxo(db, "etoro", date(2024, 4, 1), 7, "dividendo", "AAPL(US0378331005) Cash Dividend", "d1")
    _fluxo(db, "etoro", date(2024, 4, 2), 99, "dividendo", "AAPL extra sem parenteses", "d2")
    _fluxo(db, "etoro", date(2024, 4, 3), 3, "dividendo", "MSFT(US5949181045) Cash Dividend", "d3")
    _fluxo(db, "etoro", date(2024, 4, 4), 50, "juros", "AAPL(US0378331005) Interest", "d4")
    _fluxo(db, "ibkr", date(2024, 4, 5), 50, "dividendo", "AAPL(US0378331005) Cash Dividend", "d5")
    db.commit()

    posicoes = posicoes_atuais(db)

    assert [item.simbolo for item in posicoes] == ["VWCE", "AAPL", "MSFT"]
    aapl, msft = posicoes[1], posicoes[2]
    vwce = posicoes[0]
    assert aapl.fonte == "etoro"
    assert aapl.nome == "Maçã"
    assert aapl.classe == "acao"
    assert aapl.quantidade == Decimal(6)
    assert aapl.valor_eur == Decimal(240)
    assert aapl.custo_eur == Decimal(120)
    assert aapl.latente_eur == Decimal(120)
    assert aapl.latente_pct == Decimal(1)
    assert aapl.realizado_eur == Decimal(10)
    assert aapl.dividendos_eur == Decimal(7)
    assert msft.nome == "Microsoft"
    assert msft.classe == "acao"
    assert msft.custo_eur is None
    assert msft.latente_eur is None
    assert msft.latente_pct is None
    assert msft.realizado_eur == Decimal(0)
    assert msft.dividendos_eur == Decimal(3)
    assert vwce.nome == "Vanguard"
    assert vwce.classe == "etf"
    assert vwce.custo_eur == Decimal(200)
    assert vwce.latente_eur == Decimal(200)
    assert vwce.realizado_eur == Decimal(-2)
    assert vwce.dividendos_eur == Decimal(0)
    total = Decimal(740)
    assert aapl.peso == Decimal(240) / total
    assert msft.peso == Decimal(100) / total
    assert vwce.peso == Decimal(400) / total


def test_custo_medio_desce_na_proporcao_da_venda_parcial(tmp_path, monkeypatch):
    """A venda reduz o custo pelo rácio da quantidade; o dinheiro recebido não entra."""
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 2, 29)))
    db = _db(tmp_path)
    _trade(db, "ibkr", date(2024, 1, 10), "ALFA", "Alfa", 10, -1000, "b1", price=90)
    _trade(db, "ibkr", date(2024, 2, 10), "ALFA", "Alfa", -4, 450, "s1", realized_pnl_eur=20)
    _trade(
        db, "ibkr", date(2024, 1, 10), "USD", "Dolar", 1, -100, "u1",
        currency="USD", fx_to_eur="0.9",
    )
    _trade(
        db, "ibkr", date(2024, 1, 10), "EURUSD", "Cambio", -1, 5000, "fx1",
        asset_class="fx", realized_pnl_eur=5000,
    )
    db.commit()

    historico = posicoes.historico_custo(db, freq="mes")

    assert historico.sem_preco == []
    assert [dia for dia, _valores in historico.pontos] == [date(2024, 1, 31), date(2024, 2, 29)]
    janeiro, fevereiro = (valores for _dia, valores in historico.pontos)
    assert janeiro["Alfa"] == Decimal(1000)
    assert fevereiro["Alfa"] == Decimal(600)
    assert janeiro["Dolar"] == Decimal(90)
    assert fevereiro["Dolar"] == Decimal(90)
    assert "Cambio" not in fevereiro
    assert "Outras" not in fevereiro


def test_historico_mensal_guarda_as_dez_maiores_e_junta_o_resto(tmp_path, monkeypatch):
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 1, 31)))
    db = _db(tmp_path)
    for indice in range(1, 12):
        _trade(
            db, "ibkr", date(2024, 1, 15), f"N{indice:02d}", f"N{indice:02d}",
            1, -(indice * 10), f"n{indice}",
        )
    _trade(db, "ibkr", date(2024, 1, 15), "FX", "Cambio", 1, -9999, "fx", asset_class="fx")
    db.commit()

    historico = posicoes.historico_custo(db)
    assert len(historico.pontos) == 1
    dia, valores = historico.pontos[0]
    assert dia == date(2024, 1, 31)
    assert list(valores)[:3] == ["N11", "N10", "N09"]
    assert "N01" not in valores
    assert valores["Outras"] == Decimal(10)
    assert sum(valores.values(), start=Decimal(0)) == Decimal(660)
    assert historico.sem_preco == []


def test_historico_semanal_fecha_ao_domingo_ate_hoje(tmp_path, monkeypatch):
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 1, 12)))
    db = _db(tmp_path)
    _trade(db, "ibkr", date(2024, 1, 3), "ALFA", "Alfa", 2, -80, "b")
    _trade(db, "ibkr", date(2024, 1, 8), "ALFA", "Alfa", -1, 50, "s", realized_pnl_eur=10)
    db.commit()

    historico = posicoes.historico_custo(db, freq="semana")

    assert [dia for dia, _valores in historico.pontos] == [date(2024, 1, 7), date(2024, 1, 12)]
    assert historico.pontos[0][1]["Alfa"] == Decimal(80)
    assert historico.pontos[1][1]["Alfa"] == Decimal(40)


def test_historico_valor_cambia_usd_e_pence_e_marca_sem_preco(tmp_path, monkeypatch):
    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", _hoje(date(2024, 2, 15)))
    db = _db(tmp_path)
    _trade(
        db, "ibkr", date(2024, 1, 2), "AAPL", "Apple", 2, -200, "a",
        currency="USD", fx_to_eur="0.9",
    )
    _trade(
        db, "ibkr", date(2024, 1, 2), "VOD", "Vodafone", 10, -200, "v",
        currency="GBP", fx_to_eur="1.2",
    )
    _trade(db, "ibkr", date(2024, 1, 2), "SEM", "Sem", 1, -50, "s")
    db.execute("INSERT INTO tickers VALUES('ibkr','AAPL','AAPL','USD')")
    db.execute("INSERT INTO tickers VALUES('ibkr','VOD','VOD.L','GBp')")
    db.execute("INSERT INTO tickers VALUES('ibkr','SEM','','')")
    _preco(db, "AAPL", date(2024, 1, 2), "100", "USD")
    _preco(db, "AAPL", date(2024, 2, 20), "999", "USD")
    _preco(db, "VOD.L", date(2024, 1, 2), "2500", "GBp")
    _preco(db, "EURUSD=X", date(2024, 1, 2), "1.25", "USD")
    _preco(db, "EURUSD=X", date(2024, 2, 1), "1", "USD")
    _preco(db, "EURGBP=X", date(2024, 1, 2), "0.8", "GBP")
    db.commit()

    historico = posicoes.historico_valor(db, freq="mes")

    assert [dia for dia, _valores in historico.pontos] == [date(2024, 1, 31), date(2024, 2, 15)]
    janeiro, fevereiro = (valores for _dia, valores in historico.pontos)
    assert janeiro["Apple"] == Decimal(160)
    assert fevereiro["Apple"] == Decimal(200)
    assert janeiro["Vodafone"] == Decimal("312.5")
    assert fevereiro["Vodafone"] == Decimal("312.5")
    assert janeiro["Sem"] == Decimal(50)
    assert fevereiro["Sem"] == Decimal(50)
    assert historico.sem_preco == ["Sem"]


def test_estatisticas_ignoram_fx_e_vendas_sem_pnl(tmp_path):
    from financas.posicoes import (
        estatisticas_negocios,
        realizado_por_ano,
        realizado_por_simbolo,
    )

    db = _db(tmp_path)
    _trade(db, "ibkr", date(2023, 3, 1), "AAA", "Alfa", 10, -100, "c1")
    _trade(db, "ibkr", date(2023, 6, 1), "AAA", "Alfa", -4, 50, "v1", realized_pnl_eur=100)
    _trade(db, "ibkr", date(2024, 1, 1), "AAA", "Alfa nova", -1, 10, "v2", realized_pnl_eur=50)
    _trade(db, "ibkr", date(2024, 2, 1), "BBB", "Beta", 5, -50, "c2")
    _trade(db, "ibkr", date(2024, 3, 1), "BBB", "Beta", -5, 40, "v3", realized_pnl_eur=-40)
    _trade(db, "ibkr", date(2024, 4, 1), "CCC", "Gama", -1, 1, "v4", realized_pnl_eur=-10)
    _trade(db, "ibkr", date(2024, 5, 1), "DDD", "Delta", -1, 1, "v5", realized_pnl_eur=0)
    _trade(db, "ibkr", date(2024, 6, 1), "EEE", "Eco", -1, 1, "v6", realized_pnl_eur=None)
    _trade(
        db, "ibkr", date(2024, 7, 1), "EURUSD", "Cambio", -1, 1, "fx",
        asset_class="fx", realized_pnl_eur=999,
    )
    _trade(db, "etoro", date(2024, 8, 1), "ZZZ", "Zeta", -1, 1, "e1", realized_pnl_eur=7)
    db.commit()

    por_simbolo = realizado_por_simbolo(db, source="ibkr")
    assert [(item.simbolo, item.vendas, item.ganhos, item.realizado_eur, item.ainda_aberta, item.nome) for item in por_simbolo] == [
        ("AAA", 2, 2, Decimal(150), True, "Alfa nova"),
        ("DDD", 1, 0, Decimal(0), False, "Delta"),
        ("CCC", 1, 0, Decimal(-10), False, "Gama"),
        ("BBB", 1, 0, Decimal(-40), False, "Beta"),
    ]
    assert realizado_por_ano(db, source="ibkr") == [
        (2023, Decimal(100)),
        (2024, Decimal(0)),
    ]

    stats = estatisticas_negocios(db, source="ibkr")
    assert stats.fechados == 5
    assert stats.taxa_acerto == Decimal(2) / Decimal(5)
    assert stats.ganho_medio_eur == Decimal(75)
    assert stats.perda_media_eur == Decimal(-25)
    assert stats.melhor == ("AAA", Decimal(100))
    assert stats.pior == ("BBB", Decimal(-40))
    assert stats.fator_lucro == Decimal(3)
    assert stats.realizado_total_eur == Decimal(100)

    vazio = estatisticas_negocios(_db(tmp_path, "vazio.db"))
    assert vazio.fechados == 0
    assert vazio.taxa_acerto is None
    assert vazio.ganho_medio_eur is None
    assert vazio.perda_media_eur is None
    assert vazio.melhor is None
    assert vazio.pior is None
    assert vazio.fator_lucro is None
    assert vazio.realizado_total_eur == Decimal(0)

    so_ganhos = _db(tmp_path, "ganhos.db")
    _trade(so_ganhos, "ibkr", date(2024, 1, 1), "AAA", "Alfa", -1, 1, "g", realized_pnl_eur=5)
    so_ganhos.commit()
    assert estatisticas_negocios(so_ganhos).fator_lucro is None
    assert estatisticas_negocios(so_ganhos).perda_media_eur is None


def test_veredicto_sem_dados_fica_vazio(tmp_path):
    from financas.posicoes import veredicto

    assert veredicto(_db(tmp_path)) == []


def test_veredicto_com_dados_usa_percentagens_e_omite_euros(tmp_path):
    from financas.posicoes import veredicto

    db = _db(tmp_path)
    _fluxo(db, "ibkr", date(2024, 1, 1), 1000, "deposito", "Depósito", "dep")
    _preco(db, "IWDA.AS", date(2024, 1, 1), 100)
    _preco(db, "IWDA.AS", date(2025, 1, 1), 110)
    run = store.new_run(db, at=datetime(2025, 1, 1, 12, 0, 0))
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run, "ibkr", "conta", "EUR", "250", "cash", "250"),
    )
    _posicao(db, run, "ibkr", "AAA", "Alfa", 1, 600, 600, 300)
    _posicao(db, run, "ibkr", "BBB", "Beta", 1, 150, 150, 300)
    _posicao(db, run, "etoro", "EEE", "Fundo", 1, 50, 50, None)
    _trade(db, "ibkr", date(2024, 6, 1), "XXX", "Xis", -1, 1, "g1", realized_pnl_eur=10)
    _trade(db, "ibkr", date(2024, 6, 2), "YYY", "Ipsilon", -1, 1, "g2", realized_pnl_eur=10)
    _trade(db, "ibkr", date(2024, 6, 3), "ZZZ", "Zeta", -1, 1, "p1", realized_pnl_eur=-10)
    _trade(
        db, "ibkr", date(2024, 6, 4), "EURUSD", "Cambio", -1, 1, "fx",
        asset_class="fx", realized_pnl_eur=100,
    )
    db.commit()

    frases = veredicto(db)
    texto = " ".join(frases)

    assert 4 <= len(frases) <= 7
    assert any("MSCI World" in frase and "0,0%" in frase for frase in frases)
    assert any("Alfa" in frase and "100,0%" in frase and "ganho latente" in frase for frase in frases)
    assert any("Beta" in frase and "-50,0%" in frase for frase in frases)
    assert any("75,0%" in frase and "acima de 25%" in frase for frase in frases)
    assert any("cash" in frase.casefold() and "23,8%" in frase and "património" in frase for frase in frases)
    assert any("66,7%" in frase and "acerto" in frase for frase in frases)
    assert any("eToro" in frase and "depósitos" in frase for frase in frases)
    assert "€" not in texto
    assert "1000" not in texto
    assert "euro" not in texto.casefold()


def test_frequencia_desconhecida_falha(tmp_path):
    import pytest

    from financas.posicoes import historico_custo

    with pytest.raises(ValueError):
        historico_custo(_db(tmp_path), freq="dia")
