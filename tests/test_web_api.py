"""API da página local: funções puras sobre uma base sintética."""
import json
import re
import tomllib
from datetime import date, datetime
from decimal import Decimal

import pytest

from financas import config, store
from financas.web import api


def _db(tmp_path):
    db = store.connect(tmp_path / "financas.db")
    db.execute(
        """CREATE TABLE IF NOT EXISTS run_status(
            run_id INT, source TEXT, status TEXT, warnings TEXT)"""
    )
    return db


def _saldo(db, run_id, source, amount_eur, kind="cash"):
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run_id, source, "conta", "EUR", str(amount_eur), kind, str(amount_eur)),
    )


def _tx(db, booked, amount, description, source="santander"):
    _tx.n += 1
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (source, "conta", booked.isoformat(), str(amount), "EUR", description, f"tx{_tx.n}", 1),
    )


_tx.n = 0


def _fluxo(db, source, booked, amount, kind):
    _fluxo.n += 1
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        (source, booked.isoformat(), str(amount), kind, "", f"cf{_fluxo.n}"),
    )


_fluxo.n = 0


class _Posicao:
    def __init__(self):
        self.fonte = "ibkr"
        self.simbolo = "IWDA"
        self.nome = "Fundo mundo"
        self.classe = "etf"
        self.quantidade = Decimal("2")
        self.valor_eur = Decimal("200")
        self.custo_eur = Decimal("150")
        self.latente_eur = Decimal("50")
        self.latente_pct = Decimal("0.333")
        self.peso = Decimal("1")
        self.realizado_eur = Decimal("10")
        self.dividendos_eur = Decimal("4")


class _Realizado:
    fonte = "ibkr"
    simbolo = "IWDA"
    nome = "Fundo mundo"
    vendas = 2
    ganhos = 1
    realizado_eur = Decimal("10")
    ainda_aberta = True


class _Estatisticas:
    fechados = 2
    taxa_acerto = Decimal("0.5")
    ganho_medio_eur = Decimal("20")
    perda_media_eur = Decimal("-10")
    melhor = ("IWDA", Decimal("20"))
    pior = ("XYZ", Decimal("-10"))
    fator_lucro = Decimal("2")
    realizado_total_eur = Decimal("10")


class _Historico:
    def __init__(self):
        self.pontos = [
            (date(2026, 1, 31), {"IWDA": Decimal("100")}),
            (date(2026, 2, 28), {"IWDA": Decimal("110"), "CASH": Decimal("5")}),
        ]
        self.sem_preco = ["SEM"]


class _ModuloPosicoes:
    def posicoes_atuais(self, db):
        return [_Posicao()]

    def realizado_por_simbolo(self, db, source=None):
        return [_Realizado()]

    def realizado_por_ano(self, db, source=None):
        return [(2026, Decimal("10"))]

    def estatisticas_negocios(self, db, source=None):
        return _Estatisticas()

    def historico_custo(self, db, freq="mes"):
        return _Historico()

    def historico_valor(self, db, freq="mes"):
        return _Historico()

    def veredicto(self, db):
        return ["A carteira acompanha o índice."]


def _base(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    db = _db(tmp_path)
    primeiro = store.new_run(db, at=datetime(2026, 10, 1, 9, 0, 0))
    _saldo(db, primeiro, "santander", Decimal("800"))
    segundo = store.new_run(db, at=datetime(2026, 11, 1, 9, 0, 0))
    _saldo(db, segundo, "santander", Decimal("1000"))
    _saldo(db, segundo, "ibkr", Decimal("200"), kind="investment")
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (segundo, "santander", "ok", ""),
    )
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (segundo, "ibkr", "ok", "sem novidades"),
    )
    _tx(db, date(2026, 11, 3), Decimal("-25"), "LOJA FICTICIA")
    _tx(db, date(2026, 11, 4), Decimal("1500"), "VENCIMENTO")
    _fluxo(db, "ibkr", date(2026, 1, 15), Decimal("100"), "deposito")
    _fluxo(db, "ibkr", date(2026, 6, 1), Decimal("5"), "dividendo")
    db.commit()
    return db


def _json(dados):
    texto = json.dumps(dados)
    assert "Traceback" not in texto
    return texto


def test_resumo_tem_chaves_numeros_e_veredicto(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "_posicoes", lambda: _ModuloPosicoes())
    dados = api.resumo(db, {})
    _json(dados)
    assert set(dados) >= {
        "patrimonio",
        "dia",
        "fontes",
        "n_fontes_ok",
        "variacao",
        "serie",
        "alocacao",
        "avisos",
        "veredicto",
    }
    assert dados["patrimonio"] == 1200.0
    assert dados["dia"] == "2026-11-01"
    assert dados["n_fontes_ok"] == 2
    santander = next(item for item in dados["fontes"] if item["fonte"] == "santander")
    assert santander["nome"] == "Santander"
    assert santander["valor"] == 1000.0
    assert santander["cor"].startswith("#")
    assert isinstance(dados["variacao"]["delta_anterior"], float)
    assert dados["variacao"]["dia_anterior"] == "2026-10-01"
    assert dados["serie"]["datas"][0] == "2026-10-01"
    assert dados["serie"]["series"]
    assert dados["serie"]["series"][0]["cor"].startswith("#")
    assert dados["veredicto"]["indisponivel"] is False
    assert dados["veredicto"]["frases"] == ["A carteira acompanha o índice."]
    assert any(item["nome"] == "Cash" and item["cor"].startswith("#") for item in dados["alocacao"])


def test_veredicto_indisponivel_nao_rebenta_o_resumo(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)

    def falha():
        raise ImportError("sem posicoes")

    monkeypatch.setattr(api, "_posicoes", falha)
    dados = api.resumo(db, {})
    _json(dados)
    assert dados["veredicto"] == {"frases": [], "indisponivel": True}
    assert dados["patrimonio"] == 1200.0


def test_investimentos_linhas_curva_e_rendimentos(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "_posicoes", lambda: _ModuloPosicoes())
    dados = api.investimentos(db, {})
    _json(dados)
    assert "linhas" in dados
    total = next(item for item in dados["linhas"] if item["nome"] == "Total")
    assert set(total) >= {
        "nome",
        "fonte",
        "cor",
        "depositado",
        "valor",
        "ganho",
        "ganho_pct",
        "xirr",
        "xirr_indice",
        "veredicto",
        "etiqueta",
        "nota",
        "curva",
        "rendimentos",
        "realizado",
    }
    assert total["fonte"] is None
    assert total["etiqueta"] in {"bateu", "ficou_atras", "sem_dados", "empatou"}
    assert isinstance(total["veredicto"], str)
    assert total["realizado"] == [{"ano": 2026, "valor": 10.0}]
    ibkr = next(item for item in dados["linhas"] if item["fonte"] == "ibkr")
    assert ibkr["rendimentos"] == [{"ano": 2026, "valor": 5.0}]
    assert ibkr["depositado"] == 100.0
    assert all("dia" in ponto and "capital" in ponto and "valor" in ponto for ponto in ibkr["curva"])


def test_posicoes_serializa_o_contrato(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "_posicoes", lambda: _ModuloPosicoes())
    dados = api.posicoes(db, {})
    _json(dados)
    assert dados["indisponivel"] is False
    atual = dados["atuais"][0]
    assert atual["simbolo"] == "IWDA"
    assert atual["latente_pct"] == pytest.approx(0.333)
    assert atual["peso"] == 1.0
    assert dados["realizado_por_simbolo"][0]["ainda_aberta"] is True
    assert dados["realizado_por_ano"] == [{"ano": 2026, "valor": 10.0}]
    assert dados["estatisticas"]["fechados"] == 2
    assert dados["estatisticas"]["melhor"] == {"nome": "IWDA", "valor": 20.0}
    assert dados["estatisticas"]["pior"]["nome"] == "XYZ"
    assert dados["historico_custo"]["datas"] == ["2026-01-31", "2026-02-28"]
    assert dados["historico_custo"]["sem_preco"] == ["SEM"]
    assert dados["historico_valor"]["series"][0]["nome"] == "IWDA"
    assert dados["historico_custo"]["series"][0]["cor"].startswith("#")


def test_posicoes_indisponivel_devolve_seccao_vazia(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)

    class Partido:
        def posicoes_atuais(self, db):
            raise RuntimeError("falhou a conta")

        def realizado_por_simbolo(self, db, source=None):
            return [_Realizado()]

        def realizado_por_ano(self, db, source=None):
            return [(2026, Decimal("10"))]

        def estatisticas_negocios(self, db, source=None):
            return _Estatisticas()

        def historico_custo(self, db, freq="mes"):
            return _Historico()

        def historico_valor(self, db, freq="mes"):
            return _Historico()

        def veredicto(self, db):
            return ["ok"]

    monkeypatch.setattr(api, "_posicoes", lambda: Partido())
    dados = api.posicoes(db, {})
    _json(dados)
    assert dados["indisponivel"] is True
    assert dados["atuais"] == []
    assert dados["realizado_por_simbolo"] == []
    assert dados["estatisticas"] is None
    assert dados["historico_custo"]["series"] == []
    assert dados["historico_valor"]["datas"] == []


def test_gastos_periodo_personalizado_e_outros_cinzento(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)
    dados = api.gastos(
        db,
        {
            "periodo": "personalizado",
            "inicio": "2026-11-01",
            "fim": "2026-11-30",
            "freq": "mes",
        },
    )
    _json(dados)
    assert dados["inicio"] == "2026-11-01"
    assert dados["fim"] == "2026-11-30"
    assert dados["freq"] == "mes"
    for chave in (
        "categorias",
        "periodos",
        "empilhado",
        "comerciantes",
        "subscricoes",
        "dias",
        "taxa_poupanca",
        "receitas_gastos",
        "comparacao",
        "frases",
        "avisos",
    ):
        assert chave in dados
    outros = next(item for item in dados["categorias"] if item["nome"] == "Outros")
    assert outros["cor"] == "#888780"
    assert outros["valor"] == 25.0
    assert isinstance(dados["taxa_poupanca"], float)
    novembro = next(item for item in dados["receitas_gastos"] if item["periodo"] == "2026-11")
    assert novembro["receitas"] == 1500.0
    assert novembro["gastos"] == 25.0
    assert isinstance(dados["frases"], list) and dados["frases"]
    assert all(isinstance(item["valor"], float) for item in dados["dias"])


def test_movimentos_filtra_e_limita(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)
    dados = api.movimentos(db, {"texto": "loja", "limite": "1"})
    _json(dados)
    assert dados["total"] == 1
    assert len(dados["linhas"]) == 1
    linha = dados["linhas"][0]
    assert linha["data"] == "2026-11-03"
    assert linha["fonte"] == "santander"
    assert linha["descricao"] == "LOJA FICTICIA"
    assert linha["montante"] == -25.0
    assert linha["moeda"] == "EUR"
    assert linha["tipo"] == "gasto"
    assert linha["categoria"] == "Outros"
    assert linha["conta"] == "conta"
    vazio = api.movimentos(db, {"fonte": "revolut"})
    assert vazio["linhas"] == []
    assert vazio["total"] == 0


def test_gravar_categoria_escreve_toml(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)
    resultado = api.gravar_categoria(
        db, {"descricao": "LOJA FICTICIA", "categoria": "Compras", "tipo": "gasto"}
    )
    _json(resultado)
    assert resultado["ok"] is True
    texto = (tmp_path / "categorias.toml").read_text(encoding="utf-8")
    regra = tomllib.loads(texto)["regra"][0]
    assert regra["categoria"] == "Compras"
    assert regra["tipo"] == "gasto"
    assert regra["padrao"] == re.escape("LOJA FICTICIA")


def test_estado_consentimento_e_movimentos(tmp_path, monkeypatch):
    db = _base(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text(
        json.dumps({"santander": {"valid_until": "2026-12-31T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    dados = api.estado(db, {})
    _json(dados)
    assert dados["ultima_corrida"] == "2026-11-01"
    santander = next(item for item in dados["fontes"] if item["fonte"] == "santander")
    assert santander["nome"] == "Santander"
    assert santander["estado"] == "ok"
    assert santander["consentimento"] == "2026-12-31"
    assert santander["dias"] == (date(2026, 12, 31) - date.today()).days
    assert santander["movimentos"] == 2
    revolut = next(item for item in dados["fontes"] if item["fonte"] == "revolut")
    assert revolut["consentimento"] is None
    assert revolut["dias"] is None
    assert revolut["movimentos"] == 0
