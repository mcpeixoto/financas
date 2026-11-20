"""Erros vistos no relatório mensal: sinal da categoria, percentagens e snapshots."""
import re
from datetime import date, datetime
from decimal import Decimal

from financas import config, store
from financas.analise import benchmark, comparacao_mes, rentabilidade, subscricoes, taxa_poupanca, top_comerciantes
from financas.categorias import Classe, Regra, Regras, classify
from financas.dashboard.dados import apresentar_comparacao
from financas.relatorios import escrever_mensal


class _Classe:
    def __init__(self, tipo, categoria):
        self.tipo = tipo
        self.categoria = categoria


def _db(tmp_path, nome="financas.db"):
    return store.connect(tmp_path / nome)


def _tx(db, booked, amount, description, source="santander"):
    _tx.n += 1
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (source, "conta", booked.isoformat(), str(amount), "EUR", description, f"t{_tx.n}", 1),
    )


_tx.n = 0


def _fluxo(db, source, booked, amount, kind):
    _fluxo.n += 1
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        (source, booked.isoformat(), str(amount), kind, "", f"c{_fluxo.n}"),
    )


_fluxo.n = 0


def _preco(db, symbol, day, close):
    db.execute(
        "INSERT INTO prices VALUES(?,?,?,?)",
        (symbol, day.isoformat(), str(close), "EUR"),
    )


def _run(db, quando):
    return store.new_run(db, at=quando)


def _saldo(db, run_id, source, amount):
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run_id, source, "conta", "EUR", str(amount), "cash", str(amount)),
    )


def _posicao(db, run_id, source, value):
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, source, "conta", "ETF", "Fundo", "1", str(value), "EUR", None, str(value)),
    )


def _regra(categoria, tipo, padrao):
    return Regra(categoria, tipo, re.compile(padrao, re.IGNORECASE))


def _regras_pessoais(*regras):
    return Regras(tuple(regras), ())


def test_valor_positivo_nao_fica_gasto_e_receita_negativa_passa_a_gasto():
    """Regra de gasto com amount > 0 é reembolso; regra de receita com amount < 0 é gasto."""
    regras = _regras_pessoais(
        _regra("Transferência", "gasto", r"trf\.imed"),
        _regra("Pessoas", "gasto", r"from"),
        _regra("Salário", "receita", r"vencimento"),
    )

    assert classify(
        "santander", "conta", "TRF.IMED. DE EMPRESA", Decimal("1500"), regras
    ) == Classe("receita", "Reembolsos")
    assert classify(
        "santander", "conta", "Reembolso From Colega", Decimal("40"), regras
    ) == Classe("receita", "Reembolsos")
    assert classify(
        "santander", "conta", "TRF.IMED. PARA FORNECEDOR", Decimal("-20"), regras
    ) == Classe("gasto", "Transferência")
    assert classify(
        "santander", "conta", "Estorno vencimento", Decimal("-100"), regras
    ) == Classe("gasto", "Outros")
    assert classify(
        "santander", "conta", "VENCIMENTO OUTUBRO", Decimal("2000"), regras
    ) == Classe("receita", "Salário")
    assert classify(
        "santander", "conta", "TRF.IMED. ZERO", Decimal("0"), regras
    ) == Classe("gasto", "Transferência")


def test_regra_generica_de_gasto_com_entrada_tambem_e_reembolso():
    """O mesmo corte de sinal aplica-se às regras genéricas, não só às pessoais."""
    regras = Regras((), (_regra("Supermercado", "gasto", r"lidl"),))

    assert classify("santander", "conta", "LIDL DEVOLUCAO", Decimal("12.40"), regras) == Classe(
        "receita", "Reembolsos"
    )
    assert classify("santander", "conta", "LIDL DEVOLUCAO", Decimal("-12.40"), regras) == Classe(
        "gasto", "Supermercado"
    )


def test_top_e_subscricoes_ignoram_entradas_mesmo_classificadas_como_gasto(tmp_path):
    """Entrada de dinheiro não entra no top nem vira subscrição, ainda que o tipo venha gasto."""

    def classify(source, account, description, amount):
        return _Classe("gasto", "Outros")

    db = _db(tmp_path)
    _tx(db, date(2026, 10, 3), Decimal("2500"), "EMPRESA PAGAMENTO")
    _tx(db, date(2026, 10, 4), Decimal("-40"), "PINGO DOCE")
    _tx(db, date(2026, 1, 10), Decimal("10"), "ORDENADO 1")
    _tx(db, date(2026, 2, 10), Decimal("10"), "ORDENADO 2")
    _tx(db, date(2026, 1, 10), Decimal("-10"), "NETFLIX 1")
    _tx(db, date(2026, 2, 10), Decimal("-10"), "NETFLIX 2")
    db.commit()

    inicio, fim = date(2026, 10, 1), date(2026, 10, 31)
    assert top_comerciantes(db, inicio, fim, classify=classify) == [("PINGO DOCE", Decimal("40"))]
    nomes = [item.nome for item in subscricoes(db, date(2026, 12, 31), classify=classify)]
    assert nomes == ["NETFLIX"]


def test_comparacao_omite_percentagem_com_base_infima_ou_mes_incompleto(tmp_path):
    """Base abaixo de 5% do mês, ou mês anterior sem o dia 1, não gera percentagem."""
    db = _db(tmp_path)

    def classify(source, account, description, amount):
        return _Classe("gasto", "Mercado")

    for booked, amount in (
        (date(2026, 8, 1), Decimal("-1")),
        (date(2026, 9, 15), Decimal("-1")),
        (date(2026, 10, 15), Decimal("-100")),
        (date(2026, 11, 15), Decimal("-1000")),
    ):
        _tx(db, booked, amount, "MERCADO")
    db.commit()

    novembro = {linha.categoria: linha for linha in comparacao_mes(db, date(2026, 11, 15), classify=classify)}
    mercado = novembro["Mercado"]
    assert mercado.este_mes == Decimal("1000")
    assert mercado.mes_anterior == Decimal("100")
    assert mercado.vs_anterior == Decimal("9")
    assert mercado.vs_media is None
    vistas = apresentar_comparacao([mercado], esconder=False)
    assert vistas[0].vs_anterior == "900,0%"
    assert vistas[0].vs_media == "—"

    limite = _db(tmp_path, "limite.db")
    for booked, amount in (
        (date(2026, 8, 1), Decimal("-5")),
        (date(2026, 9, 1), Decimal("-5")),
        (date(2026, 10, 1), Decimal("-5")),
        (date(2026, 11, 1), Decimal("-100")),
    ):
        _tx(limite, booked, amount, "MERCADO")
    limite.commit()
    no_limite = comparacao_mes(limite, date(2026, 11, 1), classify=classify)[0]
    assert no_limite.vs_anterior == Decimal("19")
    assert no_limite.vs_media == Decimal("19")

    abaixo = _db(tmp_path, "abaixo.db")
    for booked, amount in (
        (date(2026, 8, 1), Decimal("-200")),
        (date(2026, 9, 1), Decimal("-200")),
        (date(2026, 10, 1), Decimal("-4.99")),
        (date(2026, 11, 1), Decimal("-100")),
    ):
        _tx(abaixo, booked, amount, "MERCADO")
    abaixo.commit()
    curto = comparacao_mes(abaixo, date(2026, 11, 1), classify=classify)[0]
    assert curto.vs_anterior is None
    assert curto.vs_media is not None
    assert apresentar_comparacao([curto], esconder=False)[0].vs_anterior == "—"


def test_comparacao_sem_dados_desde_o_dia_1_nao_mostra_percentagem(tmp_path):
    """O primeiro movimento bancário depois do dia 1 deixa o mês anterior incompleto."""

    def classify(source, account, description, amount):
        return _Classe("gasto", "Mercado")

    db = _db(tmp_path)
    _tx(db, date(2026, 10, 1), Decimal("-50"), "COMPRA CORRETORA", source="ibkr")
    _tx(db, date(2026, 10, 2), Decimal("-80"), "MERCADO")
    _tx(db, date(2026, 11, 2), Decimal("-100"), "MERCADO")
    db.commit()

    linha = comparacao_mes(db, date(2026, 11, 15), classify=classify)[0]
    assert linha.mes_anterior == Decimal("80")
    assert linha.este_mes == Decimal("100")
    assert linha.vs_anterior is None
    assert linha.vs_media is None
    assert apresentar_comparacao([linha], esconder=True)[0].vs_anterior == "—"

    completo = _db(tmp_path, "dia1.db")
    _tx(completo, date(2026, 10, 1), Decimal("-90"), "MERCADO")
    _tx(completo, date(2026, 11, 1), Decimal("-100"), "MERCADO")
    completo.commit()
    no_dia = comparacao_mes(completo, date(2026, 11, 1), classify=classify)[0]
    assert no_dia.vs_anterior == (Decimal("100") - Decimal("90")) / Decimal("90")
    assert no_dia.vs_media is None


def test_rentabilidade_e_benchmark_aceitam_snapshot_ate_10_dias_depois(tmp_path):
    """Sem snapshot até ao fim, vale o mais próximo nos 10 dias seguintes; mais longe, sem valor."""
    db = _db(tmp_path)
    _fluxo(db, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    perto = _run(db, datetime(2026, 11, 2, 9, 0, 0))
    _posicao(db, perto, "ibkr", Decimal("1100"))
    db.commit()

    linha = rentabilidade(db, source="ibkr", ate=date(2026, 10, 31))[0]
    assert linha.valor_atual == Decimal("1100")
    assert linha.depositado_liquido == Decimal("1000")
    assert linha.ganho_pct == Decimal("0.1")
    assert linha.xirr is not None
    assert linha.ganho_pct != Decimal("-1")

    no_dia = _db(tmp_path, "no_dia.db")
    _fluxo(no_dia, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    antigo = _run(no_dia, datetime(2026, 10, 31, 9, 0, 0))
    _posicao(no_dia, antigo, "ibkr", Decimal("1100"))
    seguinte = _run(no_dia, datetime(2026, 11, 2, 9, 0, 0))
    _posicao(no_dia, seguinte, "ibkr", Decimal("9999"))
    no_dia.commit()
    preferido = rentabilidade(no_dia, source="ibkr", ate=date(2026, 10, 31))[0]
    assert preferido.valor_atual == Decimal("1100")

    limite = _db(tmp_path, "dez_dias.db")
    _fluxo(limite, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    dia_dez = _run(limite, datetime(2026, 11, 10, 9, 0, 0))
    _posicao(limite, dia_dez, "ibkr", Decimal("1100"))
    limite.commit()
    assert rentabilidade(limite, source="ibkr", ate=date(2026, 10, 31))[0].valor_atual == Decimal("1100")

    longe = _db(tmp_path, "longe.db")
    _fluxo(longe, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    tarde = _run(longe, datetime(2026, 11, 11, 9, 0, 0))
    _posicao(longe, tarde, "ibkr", Decimal("1100"))
    longe.commit()
    sem_valor = rentabilidade(longe, source="ibkr", ate=date(2026, 10, 31))[0]
    assert sem_valor.ganho_pct is None
    assert sem_valor.xirr is None
    assert sem_valor.nota == "Sem snapshot perto do fim do período."
    assert sem_valor.ganho_pct != Decimal("-1")

    # Há cotação no dia do depósito, anterior ao fim: o preço de novembro não substitui essa.
    precos = _db(tmp_path, "precos.db")
    _preco(precos, "IWDA.AS", date(2025, 10, 31), Decimal("100"))
    _preco(precos, "IWDA.AS", date(2026, 11, 3), Decimal("110"))
    _fluxo(precos, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    precos.commit()
    referencia = benchmark(precos, source="ibkr", symbol="IWDA.AS", ate=date(2026, 10, 31))
    assert referencia.unidades == Decimal("10")
    assert referencia.valor_atual == Decimal("1000")

    # Sem nenhuma cotação até ao fim, usa-se a dos 3 dias seguintes.
    futuro = _db(tmp_path, "futuro.db")
    futuro.execute(
        "INSERT INTO prices VALUES(?,?,?,?)",
        ("IWDA.AS", "2026-11-03", "110", "USD"),
    )
    _fluxo(futuro, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    futuro.commit()
    chamadas = []

    def fx(dia, moeda):
        chamadas.append((dia, moeda))
        return Decimal("1")

    usado = benchmark(futuro, source="ibkr", symbol="IWDA.AS", fx=fx, ate=date(2026, 10, 31))
    assert chamadas == [(date(2026, 11, 3), "USD")]
    assert usado.xirr is None

    sem_preco = _db(tmp_path, "sem_preco.db")
    sem_preco.execute(
        "INSERT INTO prices VALUES(?,?,?,?)",
        ("IWDA.AS", "2026-11-11", "110", "USD"),
    )
    _fluxo(sem_preco, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    sem_preco.commit()
    chamadas.clear()
    vazio = benchmark(sem_preco, source="ibkr", symbol="IWDA.AS", fx=fx, ate=date(2026, 10, 31))
    assert chamadas == []
    assert vazio.valor_atual == Decimal("0")
    assert vazio.xirr is None


def test_relatorio_diz_quando_nao_ha_snapshot_perto_e_usa_o_de_dois_dias(monkeypatch, tmp_path):
    """O fecho do mês pode ser o snapshot do dia 2; mais longe, a frase substitui o −100%."""
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    pasta = tmp_path / "relatorios"

    perto = _db(tmp_path, "perto.db")
    base = _run(perto, datetime(2026, 9, 30, 9, 0, 0))
    _saldo(perto, base, "santander", Decimal("1000"))
    fim = _run(perto, datetime(2026, 11, 2, 9, 0, 0))
    _saldo(perto, fim, "santander", Decimal("1100"))
    _fluxo(perto, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    _posicao(perto, fim, "ibkr", Decimal("1100"))
    perto.commit()
    html_perto = escrever_mensal(perto, "2026-10", False, pasta).read_text(encoding="utf-8")
    assert "Sem snapshot perto do fim do período." not in html_perto
    assert "-100,0%" not in html_perto
    assert "A variação no período foi de 120,0%." in html_perto
    assert "10,0%" in html_perto
    assert "2026-11-02" in html_perto

    longe = _db(tmp_path, "rel_longe.db")
    antigo = _run(longe, datetime(2026, 9, 30, 9, 0, 0))
    _saldo(longe, antigo, "santander", Decimal("1000"))
    tarde = _run(longe, datetime(2026, 11, 20, 9, 0, 0))
    _saldo(longe, tarde, "santander", Decimal("1100"))
    _fluxo(longe, "ibkr", date(2025, 10, 31), Decimal("1000"), "deposito")
    _posicao(longe, tarde, "ibkr", Decimal("1100"))
    longe.commit()
    html_longe = escrever_mensal(longe, "2026-10", False, pasta).read_text(encoding="utf-8")
    assert "Sem snapshot perto do fim do período." in html_longe
    assert "-100,0%" not in html_longe
    assert "A variação no período foi de" not in html_longe


def test_taxa_de_poupanca_so_com_receitas_e_traco_sem_elas(monkeypatch, tmp_path):
    """Sem receitas no período a taxa não se calcula e o relatório mostra um traço."""
    monkeypatch.setattr(config, "DATA", tmp_path / "data")

    def classify(source, account, description, amount):
        if description == "VENCIMENTO":
            return _Classe("receita", "Salário")
        return _Classe("gasto", "Mercado")

    sem = _db(tmp_path, "sem.db")
    _tx(sem, date(2026, 10, 4), Decimal("-40"), "MERCADO")
    sem.commit()
    assert taxa_poupanca(sem, date(2026, 10, 1), date(2026, 10, 31), classify=classify) is None

    pasta = tmp_path / "relatorios"
    html = escrever_mensal(sem, "2026-10", False, pasta).read_text(encoding="utf-8")
    assert "<h2>Taxa de poupança</h2><p>—</p>" in html
    assert "Sem receitas neste período." not in html

    com = _db(tmp_path, "com.db")
    _tx(com, date(2026, 10, 1), Decimal("1000"), "VENCIMENTO")
    _tx(com, date(2026, 10, 2), Decimal("-250"), "MERCADO")
    com.commit()
    assert taxa_poupanca(com, date(2026, 10, 1), date(2026, 10, 31), classify=classify) == Decimal("0.75")
    html_com = escrever_mensal(com, "2026-10", False, pasta).read_text(encoding="utf-8")
    assert "A taxa de poupança foi de 75,0%." in html_com
