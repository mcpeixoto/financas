"""Gastos só de bancos, investimentos só com depósitos, e património sem saltos falsos."""
import json
from datetime import date, datetime
from decimal import Decimal

from financas import config, store
from financas.analise import (
    estatisticas_curiosas,
    gastos_por_categoria,
    gastos_por_dia_semana,
    patrimonio_serie,
    rentabilidade,
    subscricoes,
    taxa_poupanca,
    top_comerciantes,
)
from financas.dashboard.dados import variacao_patrimonio


class _Classe:
    def __init__(self, tipo, categoria):
        self.tipo = tipo
        self.categoria = categoria


def _db(tmp_path, nome="financas.db"):
    return store.connect(tmp_path / nome)


def _tx(db, source, booked, amount, description, currency="EUR"):
    _tx.n += 1
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (source, "conta", booked.isoformat(), str(amount), currency, description, f"tx{_tx.n}", 1),
    )


_tx.n = 0


def _fluxo(db, source, booked, amount, kind):
    _fluxo.n += 1
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        (source, booked.isoformat(), str(amount), kind, "", f"cf{_fluxo.n}"),
    )


_fluxo.n = 0


def _saldo(db, run_id, source, amount, kind="cash"):
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run_id, source, "conta", "EUR", str(amount), kind, str(amount)),
    )


def _posicao(db, run_id, source, value):
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, source, "conta", "ETF", "Fundo", "1", str(value), "EUR", None, str(value)),
    )


def _preco(db, symbol, day, close):
    db.execute(
        "INSERT INTO prices VALUES(?,?,?,?)",
        (symbol, day.isoformat(), str(close), "EUR"),
    )


def _classificar_tudo_outros(source, account, description, amount):
    if description == "VENCIMENTO":
        return _Classe("receita", "Salário")
    return _Classe("gasto", "Outros")


def _config_analise(monkeypatch, tmp_path, analise):
    caminho = tmp_path / "config.json"
    caminho.write_text(json.dumps({"analise": analise}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", caminho)


def test_gastos_ignoram_corretoras_e_a_config_pode_trocar_as_fontes(tmp_path, monkeypatch):
    """Um BUY, um fecho da eToro ou um dividendo não são gastos. A config muda as fontes."""
    db = _db(tmp_path)
    classify = _classificar_tudo_outros
    _tx(db, "santander", date(2026, 1, 10), Decimal("-10"), "Netflix 100")
    _tx(db, "santander", date(2026, 2, 10), Decimal("-10"), "Netflix 200")
    _tx(db, "santander", date(2026, 3, 2), Decimal("-40"), "PINGO DOCE")
    _tx(db, "santander", date(2026, 3, 3), Decimal("1000"), "VENCIMENTO")
    _tx(db, "ibkr", date(2026, 1, 6), Decimal("-9"), "BUY 10 NVD @ 138.52")
    _tx(db, "ibkr", date(2026, 2, 6), Decimal("-9"), "BUY 10 NVD @ 140.00")
    _tx(db, "ibkr", date(2026, 3, 2), Decimal("-200"), "Dividendo IBKR")
    _tx(db, "etoro", date(2026, 3, 4), Decimal("-15"), "Fechou 3000 (1.5)")
    _tx(db, "ibkr", date(2026, 3, 6), Decimal("-5"), "FEE", currency="USD")
    db.commit()

    inicio, fim = date(2026, 3, 1), date(2026, 3, 31)
    gastos = gastos_por_categoria(db, inicio, fim, classify=classify)
    assert gastos["Outros"] == Decimal("40")
    assert "USD" not in " ".join(gastos.avisos)
    assert sum(valor for _dia, valor in gastos_por_dia_semana(db, inicio, fim, classify=classify)) == Decimal("40")
    assert taxa_poupanca(db, inicio, fim, classify=classify) == Decimal("0.96")
    assert top_comerciantes(db, inicio, fim, classify=classify) == [("PINGO DOCE", Decimal("40"))]
    nomes = {item.nome for item in subscricoes(db, date(2026, 12, 31), classify=classify)}
    assert "Netflix" in nomes
    assert "BUY NVD" not in nomes
    frases = estatisticas_curiosas(db, inicio, fim, classify=classify)
    assert any(frase == "A taxa de poupança foi de 96%." for frase in frases)
    assert all("200" not in frase and "138" not in frase for frase in frases)

    _config_analise(monkeypatch, tmp_path, {"fontes_bancarias": ["ibkr"]})
    so_ibkr = gastos_por_categoria(db, date(2026, 1, 1), date(2026, 3, 31), classify=classify)
    assert so_ibkr["Outros"] == Decimal("218")


def test_comando_categorias_nao_lista_movimentos_de_corretora(monkeypatch, tmp_path, capsys):
    """O comando financas categorias também fica nas contas bancárias."""
    from financas import cli

    monkeypatch.setattr(config, "DATA", tmp_path)
    db = store.connect(tmp_path / "financas.db")
    _tx(db, "santander", date(2026, 6, 2), Decimal("-30"), "Lidl Benfica")
    _tx(db, "ibkr", date(2026, 6, 3), Decimal("-138.52"), "BUY 10 NVD @ 138.52")
    _tx(db, "etoro", date(2026, 6, 4), Decimal("-15"), "Fechou 3000 (1.5)")
    db.commit()
    db.close()

    assert cli.main(["categorias", "--sem-categoria"]) == 0
    texto = capsys.readouterr().out
    assert "Supermercado → 100,0%" in texto
    assert "NVD" not in texto
    assert "Fechou" not in texto
    assert "Outros" not in texto


def test_rentabilidade_exclui_bancos_e_plataformas_sem_deposito(tmp_path):
    """Sem depósito não há ganho nem XIRR, e esse valor não entra no total."""
    db = _db(tmp_path)
    _fluxo(db, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "ibkr", date(2025, 6, 1), Decimal("30"), "juros")
    _fluxo(db, "etoro", date(2025, 6, 1), Decimal("12"), "dividendo")
    _fluxo(db, "santander", date(2025, 1, 1), Decimal("8000"), "deposito")
    _fluxo(db, "aforro", date(2024, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "aforro", date(2024, 6, 1), Decimal("80"), "juros")
    run = store.new_run(db, at=datetime(2026, 1, 1, 12, 0, 0))
    _posicao(db, run, "ibkr", Decimal("1100"))
    _posicao(db, run, "etoro", Decimal("5000"))
    _saldo(db, run, "santander", Decimal("9000"))
    _saldo(db, run, "aforro", Decimal("1080"), kind="savings")
    db.commit()

    por = {linha.fonte: linha for linha in rentabilidade(db)}
    assert "santander" not in por
    etoro = por["etoro"]
    assert etoro.nota == "sem dados de depósitos"
    assert etoro.ganho_pct is None
    assert etoro.xirr is None
    assert etoro.valor_atual == Decimal("5000")
    assert etoro.rendimentos == Decimal("12")

    ibkr = por["ibkr"]
    assert ibkr.ganho_absoluto == Decimal("100")
    assert ibkr.ganho_pct == Decimal("0.1")
    assert ibkr.xirr is not None
    assert abs(ibkr.xirr - Decimal("0.1")) < Decimal("0.00001")

    # Os 80 de juros já estão nos 1080. Somá-los outra vez faria o ganho de 160.
    aforro = por["aforro"]
    assert aforro.rendimentos == Decimal("80")
    assert aforro.valor_atual == Decimal("1080")
    assert aforro.ganho_absoluto == Decimal("80")
    assert aforro.ganho_pct == Decimal("0.08")

    total = por["total"]
    assert total.depositado_liquido == Decimal("2000")
    assert total.valor_atual == Decimal("2180")
    assert total.rendimentos == Decimal("110")
    assert total.ganho_absoluto == Decimal("180")
    assert total.ganho_pct == Decimal("0.09")
    assert "eToro" not in total.nota
    assert "Santander" not in total.nota
    assert "IBKR" in total.nota
    assert "Certificados de Aforro" in total.nota


def test_fontes_de_investimento_leem_se_da_config(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _fluxo(db, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "santander", date(2025, 1, 1), Decimal("400"), "deposito")
    run = store.new_run(db, at=datetime(2026, 1, 1, 12, 0, 0))
    _posicao(db, run, "ibkr", Decimal("1100"))
    _saldo(db, run, "santander", Decimal("400"))
    db.commit()
    _config_analise(monkeypatch, tmp_path, {"fontes_investimento": ["santander"]})

    por = {linha.fonte: linha for linha in rentabilidade(db)}
    assert set(por) == {"santander", "total"}
    assert por["total"].valor_atual == Decimal("400")
    assert por["total"].nota == "Inclui Santander."


def test_preenchimento_evita_o_salto_e_a_variacao_so_usa_fontes_comuns(tmp_path):
    """Ligar uma fonte a meio não pode parecer um ganho. A variação ignora quem falta num dos lados."""
    from financas.analise import preencher_patrimonio

    db = _db(tmp_path)
    primeiro = store.new_run(db, at=datetime(2026, 1, 1, 9, 0, 0))
    _saldo(db, primeiro, "santander", Decimal("100"))
    segundo = store.new_run(db, at=datetime(2026, 2, 1, 9, 0, 0))
    _saldo(db, segundo, "santander", Decimal("130"))
    _posicao(db, segundo, "ibkr", Decimal("50"))
    db.commit()

    serie = patrimonio_serie(db)
    assert [set(por) for _dia, por, _total in serie] == [{"santander"}, {"santander", "ibkr"}]

    cheia, houve = preencher_patrimonio(serie)
    assert houve is True
    assert cheia[0][1]["ibkr"] == Decimal("50")
    assert cheia[0][2] == Decimal("150")
    assert cheia[1][2] == Decimal("180")

    variacao = variacao_patrimonio(serie)
    assert variacao.total == Decimal("180")
    assert variacao.anterior == Decimal("100")
    assert variacao.delta_anterior == Decimal("30")

    sem_santander = [
        (date(2026, 1, 1), {"santander": Decimal("100"), "ibkr": Decimal("40")}, Decimal("140")),
        (date(2026, 2, 1), {"ibkr": Decimal("40")}, Decimal("40")),
    ]
    levando = variacao_patrimonio(sem_santander)
    assert levando.anterior == Decimal("40")
    assert levando.delta_anterior == Decimal("0")
    preenchida, houve_buraco = preencher_patrimonio(sem_santander)
    assert houve_buraco is True
    assert preenchida[1][1]["santander"] == Decimal("100")
    assert preenchida[1][2] == Decimal("140")

    estavel = [
        (date(2026, 1, 1), {"santander": Decimal("10")}, Decimal("10")),
        (date(2026, 2, 1), {"santander": Decimal("12")}, Decimal("12")),
    ]
    _igual, houve_estavel = preencher_patrimonio(estavel)
    assert houve_estavel is False

    trinta = [
        (date(2026, 1, 1), {"santander": Decimal("100")}, Decimal("100")),
        (date(2026, 3, 1), {"santander": Decimal("110"), "ibkr": Decimal("70")}, Decimal("180")),
    ]
    ha_trinta = variacao_patrimonio(trinta)
    assert ha_trinta.ha_30_dias == Decimal("100")
    assert ha_trinta.delta_30 == Decimal("10")


def test_benchmark_le_a_tabela_de_precos_uma_vez(tmp_path):
    """Várias plataformas partilham a mesma leitura de preços."""
    from financas.analise import benchmarks

    db = _db(tmp_path)
    _preco(db, "IWDA.AS", date(2025, 1, 1), Decimal("100"))
    _preco(db, "IWDA.AS", date(2026, 1, 1), Decimal("110"))
    _fluxo(db, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "etoro", date(2025, 1, 1), Decimal("300"), "deposito")
    db.commit()

    class _Contador:
        def __init__(self, ligacao):
            self._db = ligacao
            self.precos = 0

        def execute(self, sql, *args, **kwargs):
            if "prices" in str(sql):
                self.precos += 1
            return self._db.execute(sql, *args, **kwargs)

        def __getattr__(self, nome):
            return getattr(self._db, nome)

    contador = _Contador(db)
    resultados = benchmarks(contador, [None, "ibkr", "etoro"], symbol="IWDA.AS", ate=date(2026, 1, 1))
    assert contador.precos == 1
    assert [item.unidades for item in resultados] == [Decimal("13"), Decimal("10"), Decimal("3")]


