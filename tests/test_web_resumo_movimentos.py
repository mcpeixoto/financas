"""Resumo com o último valor de cada fonte, série diária e movimentos bancários por omissão."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from financas import config, store
from financas.web import api


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    return store.connect(tmp_path / "financas.db")


def _saldo(db, run_id, source, amount, kind="cash"):
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run_id, source, "conta", "EUR", str(amount), kind, str(amount)),
    )


def _posicao(db, run_id, source, symbol, value):
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, source, "conta", symbol, symbol, "1", str(value), "EUR", None, str(value)),
    )


def test_fonte_que_falhou_entra_com_o_ultimo_valor_e_fica_marcada(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    primeiro = store.new_run(db, at=datetime(2026, 10, 1, 9, 0, 0))
    _saldo(db, primeiro, "santander", Decimal(1000))
    _posicao(db, primeiro, "etoro", "ALFA", Decimal(500))
    segundo = store.new_run(db, at=datetime(2026, 11, 1, 9, 0, 0))
    _saldo(db, segundo, "santander", Decimal(1100))
    db.commit()

    dados = api.resumo(db, {})

    assert dados["patrimonio"] == pytest.approx(1600.0)
    por_fonte = {item["fonte"]: item for item in dados["fontes"]}
    assert set(por_fonte) == {"santander", "etoro"}
    assert por_fonte["etoro"]["valor"] == pytest.approx(500.0)
    assert por_fonte["etoro"]["desatualizada_desde"] == "2026-10-01"
    assert por_fonte["santander"]["desatualizada_desde"] is None
    assert dados["desatualizadas"] == ["etoro"]
    assert sum(item["valor"] for item in dados["fontes"]) == pytest.approx(dados["patrimonio"])
    assert sum(item["peso"] for item in dados["fontes"]) == pytest.approx(1.0)
    assert sum(item["valor"] for item in dados["alocacao"]) == pytest.approx(dados["patrimonio"])
    ultimo = sum(serie["valores"][-1] for serie in dados["serie"]["series"])
    assert ultimo == pytest.approx(dados["patrimonio"])

    escondido = api.resumo(db, {"esconder": "1"})
    marcada = next(item for item in escondido["fontes"] if item["fonte"] == "etoro")
    assert marcada["desatualizada_desde"] == "2026-10-01"
    assert marcada["peso"] == pytest.approx(500 / 1600)


def test_serie_tem_um_ponto_por_dia_com_a_ultima_corrida(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    for at, valor in (
        (datetime(2026, 11, 1, 8, 0, 0), 900),
        (datetime(2026, 11, 1, 20, 0, 0), 1000),
        (datetime(2026, 11, 2, 8, 0, 0), 950),
        (datetime(2026, 11, 2, 9, 0, 0), 1050),
        (datetime(2026, 11, 3, 9, 0, 0), 1100),
    ):
        run = store.new_run(db, at=at)
        _saldo(db, run, "santander", Decimal(valor))
    db.commit()

    dados = api.resumo(db, {})

    datas = dados["serie"]["datas"]
    assert datas == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert len(set(datas)) == len(datas)
    santander = dados["serie"]["series"][0]
    assert santander["valores"] == [1000.0, 1050.0, 1100.0]


def _tx(db, source, booked, amount, description, ext_id):
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (source, "conta", booked, str(amount), "EUR", description, ext_id, 1),
    )


def _movimentos(tmp_path, monkeypatch, n_santander=3):
    db = _db(tmp_path, monkeypatch)
    for indice in range(n_santander):
        _tx(db, "santander", f"2026-10-{indice + 1:02d}", -10 - indice, f"COMPRA {indice}", f"s{indice}")
    _tx(db, "revolut", "2026-10-15", -7, "CAFE", "r1")
    _tx(db, "etoro", "2026-10-20", 100, "DEPOSITO CORRETORA", "e1")
    db.commit()
    return db


def test_movimentos_por_omissao_so_bancos_do_mais_recente_para_o_mais_antigo(tmp_path, monkeypatch):
    db = _movimentos(tmp_path, monkeypatch)

    dados = api.movimentos(db, {})

    fontes = [linha["fonte"] for linha in dados["linhas"]]
    assert "etoro" not in fontes
    assert set(fontes) == {"santander", "revolut"}
    datas = [linha["data"] for linha in dados["linhas"]]
    assert datas == sorted(datas, reverse=True)
    assert datas[0] == "2026-10-15"
    assert dados["fonte"] == ""
    assert set(dados["bancarias"]) >= {"santander", "revolut"}
    etoro = next(item for item in dados["fontes"] if item["fonte"] == "etoro")
    assert etoro["bancaria"] is False


def test_movimentos_filtro_fonte_escolhe_outras_e_todas(tmp_path, monkeypatch):
    db = _movimentos(tmp_path, monkeypatch)

    so_etoro = api.movimentos(db, {"fonte": "etoro"})
    assert [linha["fonte"] for linha in so_etoro["linhas"]] == ["etoro"]

    todas = api.movimentos(db, {"fonte": "todas"})
    assert {linha["fonte"] for linha in todas["linhas"]} == {"santander", "revolut", "etoro"}
    assert todas["linhas"][0]["data"] == "2026-10-20"


def test_movimentos_paginacao_por_limite(tmp_path, monkeypatch):
    db = _movimentos(tmp_path, monkeypatch, n_santander=12)

    primeira = api.movimentos(db, {"limite": "5"})
    assert len(primeira["linhas"]) == 5
    assert primeira["total"] == 13
    mais = api.movimentos(db, {"limite": "10"})
    assert len(mais["linhas"]) == 10
    assert [linha["data"] for linha in mais["linhas"][:5]] == [linha["data"] for linha in primeira["linhas"]]


def test_mesmo_nome_em_fontes_diferentes_leva_a_fonte(tmp_path, monkeypatch):
    from datetime import date

    from financas import posicoes

    monkeypatch.setattr(posicoes, "date", type("_Dia", (date,), {"today": classmethod(lambda cls: date(2026, 11, 1))}))
    db = _db(tmp_path, monkeypatch)
    run = store.new_run(db, at=datetime(2026, 11, 1, 9, 0, 0))
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run, "etoro", "conta", "BTC", "Bitcoin", "1", "300", "EUR", "200", "300"),
    )
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run, "bitcoin", "carteira", "BTC", "Bitcoin", "1", "700", "EUR", None, "700"),
    )
    for fonte, ext_id in (("etoro", "a"), ("bitcoin", "b")):
        db.execute(
            """INSERT INTO trades(source, booked, symbol, name, isin, asset_class, quantity, price,
                   currency, net_cash, fx_to_eur, realized_pnl_eur, ext_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fonte, "2026-10-01", "BTC", "Bitcoin", "", "cripto", "1", "0", "EUR", "-200", "1", None, ext_id),
        )
    db.commit()

    dados = api.posicoes(db, {})

    rotulos = {linha["fonte"]: linha["rotulo"] for linha in dados["atuais"]}
    assert rotulos == {"etoro": "Bitcoin (eToro)", "bitcoin": "Bitcoin (Cold wallet)"}
    assert all(linha["classe_nome"] == "Cripto" for linha in dados["atuais"])
    nomes = {serie["nome"] for serie in dados["historico_custo"]["series"]}
    assert nomes == {"Bitcoin (eToro)", "Bitcoin (Cold wallet)"}
