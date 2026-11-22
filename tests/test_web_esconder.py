"""Modo escondido da API: nenhuma resposta leva montantes em euros."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from financas import config, store
from financas.web import api

# Montantes inconfundíveis: nenhum pode aparecer numa resposta com esconder=1.
SALDO = Decimal("12345.67")
POSICAO = Decimal("23456.78")
CUSTO = Decimal("21098.76")
DEPOSITO = Decimal("34567.89")
DIVIDENDO = Decimal("432.19")
GASTO = Decimal("-1234.56")
RECEITA = Decimal("4321.09")
SUBSCRICAO = Decimal("-17.43")
COMPRA = Decimal("-20987.65")
VENDA = Decimal("6543.21")
REALIZADO = Decimal("765.43")
ANTIGO = Decimal("11111.11")


def _proibidos() -> set[float]:
    base = [SALDO, POSICAO, CUSTO, DEPOSITO, DIVIDENDO, GASTO, RECEITA, SUBSCRICAO, COMPRA, VENDA, REALIZADO, ANTIGO]
    valores = {abs(float(item)) for item in base}
    valores.add(float(SALDO + POSICAO))
    valores.add(float(POSICAO - CUSTO))
    valores.add(float(abs(SUBSCRICAO) * 12))
    valores.add(float(abs(GASTO) + abs(SUBSCRICAO) * 3))
    return valores


def _numeros(dados) -> list[float]:
    if isinstance(dados, bool) or dados is None:
        return []
    if isinstance(dados, (int, float)):
        return [float(dados)]
    if isinstance(dados, dict):
        return [numero for item in dados.values() for numero in _numeros(item)]
    if isinstance(dados, list):
        return [numero for item in dados for numero in _numeros(item)]
    return []


def _semear(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    db = store.connect(tmp_path / "financas.db")
    db.execute("CREATE TABLE IF NOT EXISTS run_status(run_id INT, source TEXT, status TEXT, warnings TEXT)")
    primeiro = store.new_run(db, at=datetime(2026, 10, 1, 9, 0, 0))
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (primeiro, "santander", "conta", "EUR", str(ANTIGO), "cash", str(ANTIGO)),
    )
    segundo = store.new_run(db, at=datetime(2026, 11, 1, 9, 0, 0))
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (segundo, "santander", "conta", "EUR", str(SALDO), "cash", str(SALDO)),
    )
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (segundo, "ibkr", "conta", "ALFA", "Alfa Fictícia", "7", str(POSICAO), "EUR", str(CUSTO), str(POSICAO)),
    )
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (segundo, "santander", "ok", ""))
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (segundo, "ibkr", "ok", ""))
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2026-01-10", str(DEPOSITO), "deposito", "", "c1"),
    )
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2026-06-10", str(DIVIDENDO), "dividendo", "ALFA(XX0000000000) Cash Dividend", "c2"),
    )
    for ext_id, booked, quantidade, net_cash, pnl in (
        ("t1", "2026-01-15", "9", str(COMPRA), None),
        ("t2", "2026-07-15", "-2", str(VENDA), str(REALIZADO)),
    ):
        db.execute(
            """INSERT INTO trades(source, booked, symbol, name, isin, asset_class, quantity, price,
                   currency, net_cash, fx_to_eur, realized_pnl_eur, ext_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("ibkr", booked, "ALFA", "Alfa Fictícia", "", "acao", quantidade, "0", "EUR", net_cash, "1", pnl, ext_id),
        )
    movimentos = [
        ("2026-11-03", GASTO, "LOJA FICTICIA"),
        ("2026-11-04", RECEITA, "VENCIMENTO"),
        ("2026-09-05", SUBSCRICAO, "NETFLIX.COM"),
        ("2026-10-05", SUBSCRICAO, "NETFLIX.COM"),
        ("2026-11-05", SUBSCRICAO, "NETFLIX.COM"),
    ]
    for indice, (booked, montante, descricao) in enumerate(movimentos):
        db.execute(
            "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
            ("santander", "conta", booked, str(montante), "EUR", descricao, f"tx{indice}", 1),
        )
    db.commit()
    return db


_GASTOS = {"periodo": "personalizado", "inicio": "2026-09-01", "fim": "2026-11-30", "freq": "mes"}


def _chamadas():
    return {
        "resumo": (api.resumo, {}),
        "investimentos": (api.investimentos, {}),
        "posicoes": (api.posicoes, {}),
        "gastos": (api.gastos, dict(_GASTOS)),
        "movimentos": (api.movimentos, {}),
    }


@pytest.mark.parametrize("nome", ["resumo", "investimentos", "posicoes", "gastos", "movimentos"])
def test_esconder_nao_devolve_nenhum_montante(nome, tmp_path, monkeypatch):
    db = _semear(tmp_path, monkeypatch)
    funcao, params = _chamadas()[nome]
    proibidos = _proibidos()

    aberto = json.loads(json.dumps(funcao(db, dict(params))))
    assert set(_numeros(aberto)) & proibidos, "a base sintética tem de aparecer sem o modo escondido"

    escondido = json.loads(json.dumps(funcao(db, dict(params, esconder="1"))))
    assert escondido["esconder"] is True
    vistos = {round(abs(numero), 2) for numero in _numeros(escondido)}
    assert not (vistos & proibidos), sorted(vistos & proibidos)


def test_esconder_poe_as_chaves_de_montante_a_null(tmp_path, monkeypatch):
    db = _semear(tmp_path, monkeypatch)

    resumo = api.resumo(db, {"esconder": "1"})
    assert resumo["patrimonio"] is None
    assert all(item["valor"] is None for item in resumo["fontes"])
    assert all(item["valor"] is None for item in resumo["alocacao"])
    assert resumo["variacao"]["delta_anterior"] is None
    assert resumo["variacao"]["anterior"] is None
    assert resumo["serie"]["unidade"] == "indice"
    ultimo = sum(serie["valores"][-1] for serie in resumo["serie"]["series"])
    assert ultimo == pytest.approx(100.0)
    assert sum(item["peso"] for item in resumo["fontes"]) == pytest.approx(1.0)

    investimentos = api.investimentos(db, {"esconder": "1"})
    for linha in investimentos["linhas"]:
        assert linha["depositado"] is None
        assert linha["valor"] is None
        assert linha["ganho"] is None
        assert all(ponto["capital"] is None and ponto["valor"] is None for ponto in linha["curva"])
        assert all(item["valor"] is None for item in linha["rendimentos"])
        assert all(item["valor"] is None for item in linha["realizado"])
        mensal = linha["mensal"]
        if mensal["datas"]:
            assert mensal["unidade"] == "indice"
            assert mensal["depositado"][-1] == pytest.approx(100.0)

    posicoes = api.posicoes(db, {"esconder": "1"})
    assert posicoes["indisponivel"] is False
    atual = posicoes["atuais"][0]
    for chave in ("valor_eur", "custo_eur", "latente_eur", "realizado_eur", "dividendos_eur", "ganho_eur", "quantidade"):
        assert atual[chave] is None
    assert atual["latente_pct"] == pytest.approx(float((POSICAO - CUSTO) / CUSTO))
    assert all(item["realizado_eur"] is None for item in posicoes["realizado_por_simbolo"])
    assert all(item["valor"] is None for item in posicoes["realizado_por_ano"])
    assert posicoes["estatisticas"]["realizado_total_eur"] is None
    assert posicoes["estatisticas"]["melhor"]["valor"] is None
    for chave in ("historico_custo", "historico_valor"):
        historico = posicoes[chave]
        assert historico["unidade"] == "indice"
        assert sum(serie["valores"][-1] for serie in historico["series"]) == pytest.approx(100.0)
    assert all(item["ganho_eur"] is None for item in posicoes["ganho_por_posicao"])

    gastos = api.gastos(db, dict(_GASTOS, esconder="1"))
    assert all(item["valor"] is None for item in gastos["categorias"])
    assert all(item["valores"] is None for item in gastos["empilhado"])
    assert all(item["valor"] is None for item in gastos["comerciantes"])
    assert all(item["custo_anual"] is None for item in gastos["subscricoes"])
    assert all(item["valor"] is None for item in gastos["dias"])
    assert all(item["receitas"] is None and item["gastos"] is None for item in gastos["receitas_gastos"])
    assert all(item["este_mes"] is None for item in gastos["comparacao"])
    assert sum(item["peso"] for item in gastos["categorias"]) == pytest.approx(1.0)

    movimentos = api.movimentos(db, {"esconder": "1"})
    assert movimentos["linhas"]
    assert all(linha["montante"] is None for linha in movimentos["linhas"])
    assert {linha["sentido"] for linha in movimentos["linhas"]} == {"entrada", "saída"}


def test_sem_esconder_mantem_os_euros(tmp_path, monkeypatch):
    db = _semear(tmp_path, monkeypatch)
    resumo = api.resumo(db, {"esconder": "0"})
    assert resumo["esconder"] is False
    assert resumo["patrimonio"] == pytest.approx(float(SALDO + POSICAO))
    posicoes = api.posicoes(db, {})
    atual = posicoes["atuais"][0]
    assert atual["classe_nome"] == "Ação"
    assert atual["ganho_eur"] == pytest.approx(float(POSICAO - CUSTO + REALIZADO + DIVIDENDO))
    assert posicoes["cartoes"]["abertas"] == 1
    assert posicoes["cartoes"]["peso_top3"] == pytest.approx(1.0)
    assert posicoes["cartoes"]["latente_pct"] == pytest.approx(float((POSICAO - CUSTO) / CUSTO))
    assert date.fromisoformat(api.investimentos(db, {})["linhas"][0]["mensal"]["datas"][0])
