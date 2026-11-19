"""Estatísticas do dashboard: gastos, subscrições, património, XIRR e benchmark."""
import sys
import types
from datetime import date, datetime
from decimal import Decimal

from financas import store
from financas.analise import (
    alocacao,
    benchmark,
    comparacao_mes,
    estatisticas_curiosas,
    gastos_por_categoria,
    gastos_por_dia_semana,
    gastos_por_periodo,
    patrimonio_serie,
    rentabilidade,
    subscricoes,
    taxa_poupanca,
    top_comerciantes,
    xirr,
)

DDL_CONTRATO = """
CREATE TABLE IF NOT EXISTS cashflows(
    source TEXT, booked TEXT, amount_eur TEXT, kind TEXT, description TEXT, ext_id TEXT,
    PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS prices(
    symbol TEXT, day TEXT, close TEXT, currency TEXT,
    PRIMARY KEY(symbol, day));
"""


class _Classe:
    def __init__(self, tipo, categoria):
        self.tipo = tipo
        self.categoria = categoria


def _db(tmp_path, nome="financas.db"):
    db = store.connect(tmp_path / nome)
    db.executescript(DDL_CONTRATO)
    return db


def _classificador(regras):
    """regras: descrição → (tipo, categoria). O que não estiver mapeado é gasto «Outros»."""

    def classify(source, account, description, amount):
        tipo, categoria = regras.get(description, ("gasto", "Outros"))
        return _Classe(tipo, categoria)

    return classify


def _tx(db, booked, amount, description, currency="EUR", source="santander", account="conta"):
    _tx.n += 1
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (source, account, booked.isoformat(), str(amount), currency, description, f"tx{_tx.n}", 1),
    )


_tx.n = 0


def _fluxo(db, source, booked, amount, kind, description=""):
    _fluxo.n += 1
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        (source, booked.isoformat(), str(amount), kind, description, f"cf{_fluxo.n}"),
    )


_fluxo.n = 0


def _preco(db, symbol, day, close, currency="EUR"):
    db.execute(
        "INSERT INTO prices VALUES(?,?,?,?)",
        (symbol, day.isoformat(), str(close), currency),
    )


def _saldo(db, run_id, source, amount, amount_eur, currency="EUR", kind="cash", account="conta"):
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run_id, source, account, currency, str(amount), kind, str(amount_eur)),
    )


def _posicao(db, run_id, source, value_eur, symbol="ETF", name="Fundo"):
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, source, "conta", symbol, name, "1", str(value_eur), "EUR", None, str(value_eur)),
    )


def test_gastos_por_categoria_eur_taxa_recente_e_avisos(tmp_path):
    """Gasto em EUR e USD (taxa do saldo mais recente); GBP sem taxa fica de fora, com aviso."""
    db = _db(tmp_path)
    antigo = store.new_run(db, at=datetime(2026, 1, 1, 9, 0, 0))
    _saldo(db, antigo, "revolut", Decimal("100"), Decimal("80"), currency="USD")
    recente = store.new_run(db, at=datetime(2026, 6, 1, 9, 0, 0))
    _saldo(db, recente, "revolut", Decimal("100"), Decimal("90"), currency="USD")
    classify = _classificador(
        {
            "MERCADO": ("gasto", "Supermercado"),
            "LOJA": ("gasto", "Compras"),
            "TESCO": ("gasto", "Supermercado"),
            "FORA": ("gasto", "Supermercado"),
            "SALARIO": ("receita", "Salário"),
            "PARA POUPANCA": ("interna", "Transferência"),
            "COMPRA IWDA": ("investimento", "Investimento"),
        }
    )
    _tx(db, date(2026, 6, 30), Decimal("-1000"), "FORA")
    _tx(db, date(2026, 7, 1), Decimal("-25"), "MERCADO")
    _tx(db, date(2026, 7, 15), Decimal("-10"), "LOJA", currency="USD")
    _tx(db, date(2026, 7, 16), Decimal("-40"), "TESCO", currency="GBP")
    _tx(db, date(2026, 7, 20), Decimal("3000"), "SALARIO")
    _tx(db, date(2026, 7, 21), Decimal("-500"), "PARA POUPANCA")
    _tx(db, date(2026, 7, 22), Decimal("-800"), "COMPRA IWDA")
    _tx(db, date(2026, 8, 1), Decimal("-700"), "MERCADO")
    db.commit()

    gastos = gastos_por_categoria(db, date(2026, 7, 1), date(2026, 7, 31), classify=classify)

    assert gastos["Supermercado"] == Decimal("25")
    assert gastos["Compras"] == Decimal("9")
    assert set(gastos) == {"Supermercado", "Compras"}
    assert any("GBP" in aviso and "sem taxa de câmbio" in aviso for aviso in gastos.avisos)


def test_classify_none_importa_categorias_so_quando_corre(tmp_path, monkeypatch):
    """Sem classificador injetado, as regras carregam-se por import preguiçoso."""
    db = _db(tmp_path)
    _tx(db, date(2026, 5, 2), Decimal("-3"), "CAFÉ")
    db.commit()
    mod = types.ModuleType("financas.categorias")

    def carregar_regras():
        return object()

    def classify(source, account, description, amount, regras=None):
        assert regras is not None
        assert amount == Decimal("-3")
        return _Classe("gasto", "Café")

    mod.carregar_regras = carregar_regras
    mod.classify = classify
    monkeypatch.setitem(sys.modules, "financas.categorias", mod)

    gastos = gastos_por_categoria(db, date(2026, 5, 1), date(2026, 5, 31))

    assert gastos == {"Café": Decimal("3")}
    assert gastos.avisos == []


def test_periodo_semana_iso_atravessa_o_ano_civil(tmp_path):
    """2025-12-29 é a semana ISO 2026-W01, não uma semana de 2025."""
    db = _db(tmp_path)
    classify = _classificador({"SALARIO": ("receita", "Salário")})
    _tx(db, date(2025, 12, 29), Decimal("-12"), "LOJA")
    _tx(db, date(2026, 11, 8), Decimal("-4"), "LOJA")
    _tx(db, date(2026, 11, 9), Decimal("-6"), "LOJA")
    _tx(db, date(2026, 11, 9), Decimal("100"), "SALARIO")
    db.commit()

    semanas = gastos_por_periodo(db, date(2025, 12, 1), date(2026, 11, 30), "semana", classify=classify)

    assert semanas == [
        ("2026-W01", Decimal("12")),
        ("2026-W45", Decimal("4")),
        ("2026-W46", Decimal("6")),
    ]
    assert gastos_por_periodo(db, date(2025, 12, 29), date(2025, 12, 29), "mes", classify=classify) == [
        ("2025-12", Decimal("12"))
    ]
    assert gastos_por_periodo(db, date(2025, 12, 29), date(2025, 12, 29), "ano", classify=classify) == [
        ("2025", Decimal("12"))
    ]


def test_periodo_mes_soma_as_semanas(tmp_path):
    db = _db(tmp_path)
    classify = _classificador({})
    _tx(db, date(2026, 11, 2), Decimal("-10"), "A")
    _tx(db, date(2026, 11, 9), Decimal("-30"), "A")
    db.commit()

    assert gastos_por_periodo(db, date(2026, 11, 1), date(2026, 11, 30), "mes", classify=classify) == [
        ("2026-11", Decimal("40"))
    ]
    assert gastos_por_periodo(db, date(2026, 11, 1), date(2026, 11, 30), "ano", classify=classify) == [
        ("2026", Decimal("40"))
    ]


def test_frequencia_desconhecida_falha(tmp_path):
    db = _db(tmp_path)
    try:
        gastos_por_periodo(db, date(2026, 1, 1), date(2026, 1, 31), "mês", classify=_classificador({}))
    except ValueError as exc:
        assert "frequência" in str(exc)
    else:
        raise AssertionError("devia recusar frequência desconhecida")


def test_top_comerciantes_normaliza_e_ignora_internas(tmp_path):
    """Números, refs e a repetição «X X» não partem o mesmo comerciante; internas ficam de fora."""
    db = _db(tmp_path)
    classify = _classificador(
        {
            "TRANSF TRANSF": ("interna", "Transferência"),
            "DEGIRO 1": ("investimento", "Investimento"),
        }
    )
    _tx(db, date(2026, 4, 1), Decimal("-30"), "Worten 9981 Worten 9981")
    _tx(db, date(2026, 4, 2), Decimal("-20"), "WORTEN 12")
    _tx(db, date(2026, 4, 3), Decimal("-15"), "PINGO DOCE REF 12345")
    _tx(db, date(2026, 4, 4), Decimal("-8"), "Continente Continente")
    _tx(db, date(2026, 4, 5), Decimal("-2"), "Continente 7")
    _tx(db, date(2026, 4, 6), Decimal("-999"), "TRANSF TRANSF")
    _tx(db, date(2026, 4, 7), Decimal("-500"), "DEGIRO 1")
    db.commit()

    inicio, fim = date(2026, 4, 1), date(2026, 4, 30)
    assert top_comerciantes(db, inicio, fim, n=2, classify=classify) == [
        ("Worten", Decimal("50")),
        ("PINGO DOCE", Decimal("15")),
    ]
    assert top_comerciantes(db, inicio, fim, n=1, classify=classify) == [("Worten", Decimal("50"))]


def test_subscricoes_mensal_anual_e_limites(tmp_path):
    """Mensal (±10%, ~30 dias) e anual; valor ou intervalo fora, ou tipo interna, não contam."""
    db = _db(tmp_path)
    regras = {"POUPANCA": ("interna", "Transferência")}
    for booked, amount, desc in (
        (date(2026, 1, 10), Decimal("-10"), "Netflix 100"),
        (date(2026, 2, 10), Decimal("-11"), "Netflix 200"),
        (date(2026, 1, 5), Decimal("-10"), "Spotify 1"),
        (date(2026, 2, 5), Decimal("-10"), "Spotify 2"),
        (date(2026, 3, 5), Decimal("-50"), "Spotify 3"),
        (date(2024, 6, 1), Decimal("-80"), "Seguro 1"),
        (date(2025, 6, 1), Decimal("-80"), "Seguro 2"),
        (date(2026, 1, 1), Decimal("-10"), "Ginásio 1"),
        (date(2026, 2, 1), Decimal("-12"), "Ginásio 2"),
        (date(2026, 3, 1), Decimal("-20"), "Café 1"),
        (date(2026, 3, 11), Decimal("-20"), "Café 2"),
        (date(2026, 4, 1), Decimal("-9"), "Jornal"),
        (date(2026, 1, 15), Decimal("-10"), "POUPANCA"),
        (date(2026, 2, 15), Decimal("-10"), "POUPANCA"),
    ):
        _tx(db, booked, amount, desc)
    db.commit()

    encontradas = {
        (s.nome, s.periodicidade, s.custo_anual)
        for s in subscricoes(db, date(2026, 12, 31), classify=_classificador(regras))
    }

    assert ("Netflix", "mensal", Decimal("126")) in encontradas
    assert ("Spotify", "mensal", Decimal("120")) in encontradas
    assert ("Seguro", "anual", Decimal("80")) in encontradas
    assert all(nome not in {"Ginásio", "Café", "Jornal", "POUPANCA"} for nome, _, _ in encontradas)


def test_subscricao_depois_de_ate_nao_entra(tmp_path):
    db = _db(tmp_path)
    _tx(db, date(2026, 1, 10), Decimal("-10"), "Netflix")
    _tx(db, date(2026, 2, 10), Decimal("-10"), "Netflix")
    db.commit()

    assert subscricoes(db, date(2026, 1, 31), classify=_classificador({})) == []


def test_gastos_por_dia_semana_inclui_zeros(tmp_path):
    db = _db(tmp_path)
    _tx(db, date(2026, 11, 6), Decimal("-40"), "JANTAR")
    _tx(db, date(2026, 11, 6), Decimal("100"), "SALARIO")
    db.commit()
    classify = _classificador({"SALARIO": ("receita", "Salário")})

    dias = gastos_por_dia_semana(db, date(2026, 11, 1), date(2026, 11, 30), classify=classify)

    assert [nome for nome, _ in dias] == [
        "segunda-feira",
        "terça-feira",
        "quarta-feira",
        "quinta-feira",
        "sexta-feira",
        "sábado",
        "domingo",
    ]
    assert dict(dias)["sexta-feira"] == Decimal("40")
    assert dict(dias)["sábado"] == Decimal("0")


def test_taxa_poupanca_e_none_sem_receitas(tmp_path):
    db = _db(tmp_path)
    classify = _classificador({"ORDENADO": ("receita", "Salário"), "PARA CONTA": ("interna", "Transferência")})
    _tx(db, date(2026, 3, 1), Decimal("1000"), "ORDENADO")
    _tx(db, date(2026, 3, 2), Decimal("-250"), "RENDA")
    _tx(db, date(2026, 3, 3), Decimal("-900"), "PARA CONTA")
    db.commit()

    assert taxa_poupanca(db, date(2026, 3, 1), date(2026, 3, 31), classify=classify) == Decimal("0.75")

    db2 = _db(tmp_path, "sem_receita.db")
    _tx(db2, date(2026, 3, 2), Decimal("-250"), "RENDA")
    db2.commit()
    assert taxa_poupanca(db2, date(2026, 3, 1), date(2026, 3, 31), classify=_classificador({})) is None


def test_comparacao_mes_contra_anterior_e_media_de_tres(tmp_path):
    """A média são os 3 meses anteriores, com zeros; julho e o investimento não entram."""
    db = _db(tmp_path)
    classify = _classificador(
        {
            "MERCADO": ("gasto", "Mercado"),
            "VIAGEM": ("gasto", "Viagens"),
            "ACAO": ("investimento", "Investimento"),
        }
    )
    for booked, amount, desc in (
        (date(2026, 7, 15), Decimal("-1000"), "MERCADO"),
        (date(2026, 8, 15), Decimal("-30"), "MERCADO"),
        (date(2026, 9, 15), Decimal("-60"), "MERCADO"),
        (date(2026, 10, 15), Decimal("-90"), "MERCADO"),
        (date(2026, 11, 1), Decimal("-120"), "MERCADO"),
        (date(2026, 10, 15), Decimal("-30"), "VIAGEM"),
        (date(2026, 12, 1), Decimal("-500"), "MERCADO"),
        (date(2026, 11, 15), Decimal("-9999"), "ACAO"),
    ):
        _tx(db, booked, amount, desc)
    db.commit()

    por = {linha.categoria: linha for linha in comparacao_mes(db, date(2026, 11, 15), classify=classify)}

    mercado = por["Mercado"]
    assert mercado.este_mes == Decimal("120")
    assert mercado.mes_anterior == Decimal("90")
    assert mercado.media_3_meses == Decimal("60")
    viagem = por["Viagens"]
    assert viagem.este_mes == Decimal("0")
    assert viagem.mes_anterior == Decimal("30")
    assert viagem.media_3_meses == Decimal("10")
    assert "Investimento" not in por


def test_patrimonio_serie_ultimo_run_do_dia_sem_arrastar(tmp_path):
    db = _db(tmp_path)
    manha = store.new_run(db, at=datetime(2026, 1, 10, 8, 0, 0))
    _saldo(db, manha, "santander", Decimal("100"), Decimal("100"))
    noite = store.new_run(db, at=datetime(2026, 1, 10, 20, 0, 0))
    _saldo(db, noite, "santander", Decimal("150"), Decimal("150"))
    fevereiro = store.new_run(db, at=datetime(2026, 2, 1, 9, 0, 0))
    _saldo(db, fevereiro, "ibkr", Decimal("40"), Decimal("40"))
    _posicao(db, fevereiro, "ibkr", Decimal("60"))
    db.commit()

    assert patrimonio_serie(db) == [
        (date(2026, 1, 10), {"santander": Decimal("150")}, Decimal("150")),
        (date(2026, 2, 1), {"ibkr": Decimal("100")}, Decimal("100")),
    ]


def test_xirr_conhecido_ordem_irrelevante_e_sem_solucao():
    """−1000 → +1100 num ano de 365 dias é 10%; em 2024 (366 dias) fica ~0,0997."""
    assert xirr([]) is None
    assert xirr([(date(2025, 1, 1), Decimal("-1000"))]) is None
    assert xirr(
        [(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("-50"))]
    ) is None

    certo = xirr(
        [(date(2026, 1, 1), Decimal("1100")), (date(2025, 1, 1), Decimal("-1000"))]
    )
    assert certo is not None
    assert abs(certo - Decimal("0.1")) < Decimal("0.00001")

    bissexto = xirr(
        [(date(2024, 1, 1), Decimal("-1000")), (date(2025, 1, 1), Decimal("1100"))]
    )
    assert bissexto is not None
    assert abs(bissexto - Decimal("0.099713585934")) < Decimal("0.00001")

    perda = xirr(
        [(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("900"))]
    )
    assert perda is not None
    assert abs(perda - Decimal("-0.1")) < Decimal("0.00001")


def test_rentabilidade_por_plataforma_xirr_e_total(tmp_path):
    """Depósito > 0 é dinheiro investido (negativo no XIRR). Juros não entram no depositado."""
    db = _db(tmp_path)
    _fluxo(db, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "ibkr", date(2025, 6, 1), Decimal("30"), "juros")
    _fluxo(db, "ibkr", date(2025, 6, 2), Decimal("-5"), "custo")
    _fluxo(db, "etoro", date(2025, 1, 1), Decimal("500"), "deposito")
    run = store.new_run(db, at=datetime(2026, 1, 1, 12, 0, 0))
    _saldo(db, run, "ibkr", Decimal("100"), Decimal("100"))
    _posicao(db, run, "ibkr", Decimal("1000"))
    _posicao(db, run, "etoro", Decimal("400"), symbol="STOCK", name="Ação")
    db.commit()

    por = {linha.fonte: linha for linha in rentabilidade(db)}
    ibkr = por["ibkr"]
    assert ibkr.depositado_liquido == Decimal("1000")
    assert ibkr.rendimentos == Decimal("30")
    assert ibkr.valor_atual == Decimal("1100")
    assert ibkr.ganho_absoluto == Decimal("100")
    assert ibkr.ganho_pct == Decimal("0.1")
    assert ibkr.xirr is not None
    assert abs(ibkr.xirr - Decimal("0.1")) < Decimal("0.00001")

    etoro = por["etoro"]
    assert etoro.depositado_liquido == Decimal("500")
    assert etoro.rendimentos == Decimal("0")
    assert etoro.valor_atual == Decimal("400")
    assert etoro.ganho_absoluto == Decimal("-100")
    assert etoro.ganho_pct == Decimal("-0.2")

    total = por["total"]
    assert total.depositado_liquido == Decimal("1500")
    assert total.rendimentos == Decimal("30")
    assert total.valor_atual == Decimal("1500")
    assert total.ganho_absoluto == Decimal("0")
    assert total.ganho_pct == Decimal("0")
    assert total.xirr is not None
    assert abs(total.xirr) < Decimal("0.00001")
    assert [linha.fonte for linha in rentabilidade(db, source="ibkr")] == ["ibkr"]


def test_rentabilidade_ate_corta_fluxos_e_snapshot(tmp_path):
    db = _db(tmp_path)
    _fluxo(db, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "ibkr", date(2026, 6, 1), Decimal("5000"), "deposito")
    primeiro = store.new_run(db, at=datetime(2026, 1, 1, 12, 0, 0))
    _posicao(db, primeiro, "ibkr", Decimal("1100"))
    segundo = store.new_run(db, at=datetime(2026, 8, 1, 12, 0, 0))
    _posicao(db, segundo, "ibkr", Decimal("7000"))
    db.commit()

    linha = rentabilidade(db, ate=date(2026, 1, 15))[0]
    assert linha.fonte == "ibkr"
    assert linha.depositado_liquido == Decimal("1000")
    assert linha.valor_atual == Decimal("1100")


def test_rentabilidade_levantamento_reduz_o_depositado_liquido(tmp_path):
    db = _db(tmp_path)
    _fluxo(db, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "ibkr", date(2025, 7, 1), Decimal("-100"), "levantamento")
    _fluxo(db, "ibkr", date(2025, 8, 1), Decimal("10"), "dividendo")
    run = store.new_run(db, at=datetime(2026, 1, 1, 12, 0, 0))
    _posicao(db, run, "ibkr", Decimal("990"))
    db.commit()

    linha = rentabilidade(db, source="ibkr")[0]
    assert linha.depositado_liquido == Decimal("900")
    assert linha.rendimentos == Decimal("10")
    assert linha.valor_atual == Decimal("990")
    assert linha.ganho_absoluto == Decimal("90")
    assert linha.ganho_pct == Decimal("0.1")


def test_benchmark_compra_o_indice_e_ignora_dividendos(tmp_path):
    db = _db(tmp_path)
    _preco(db, "IWDA.UK", date(2025, 1, 1), Decimal("100"))
    _preco(db, "IWDA.UK", date(2026, 1, 1), Decimal("110"))
    _fluxo(db, "ibkr", date(2025, 1, 1), Decimal("1000"), "deposito")
    _fluxo(db, "ibkr", date(2025, 6, 1), Decimal("5000"), "dividendo")
    _fluxo(db, "etoro", date(2025, 1, 1), Decimal("300"), "deposito")
    db.commit()

    resultado = benchmark(db, source="ibkr", symbol="IWDA.UK")

    assert resultado.symbol == "IWDA.UK"
    assert resultado.unidades == Decimal("10")
    assert resultado.valor_atual == Decimal("1100")
    assert resultado.depositado_liquido == Decimal("1000")
    assert resultado.xirr is not None
    assert abs(resultado.xirr - Decimal("0.1")) < Decimal("0.00001")


def test_benchmark_vende_no_levantamento(tmp_path):
    db = _db(tmp_path)
    _preco(db, "IWDA.UK", date(2025, 1, 2), Decimal("100"))
    _preco(db, "IWDA.UK", date(2025, 6, 2), Decimal("80"))
    _preco(db, "IWDA.UK", date(2026, 1, 2), Decimal("90"))
    _fluxo(db, "ibkr", date(2025, 1, 2), Decimal("1000"), "deposito")
    _fluxo(db, "ibkr", date(2025, 6, 2), Decimal("-400"), "levantamento")
    db.commit()

    resultado = benchmark(db, symbol="IWDA.UK", ate=date(2026, 1, 2))

    assert resultado.unidades == Decimal("5")
    assert resultado.valor_atual == Decimal("450")
    assert resultado.depositado_liquido == Decimal("600")


def test_benchmark_preco_anterior_e_fx_injetado(tmp_path):
    """Domingo usa a cotação de sexta; preço em USD passa pelo fx (EUR por 1 USD)."""
    db = _db(tmp_path)
    _preco(db, "VWCE.DE", date(2024, 1, 5), Decimal("80"), currency="USD")
    _preco(db, "VWCE.DE", date(2024, 1, 8), Decimal("999"), currency="USD")
    _fluxo(db, "ibkr", date(2024, 1, 7), Decimal("80"), "deposito")
    db.commit()
    chamadas = []

    def fx(dia, moeda):
        chamadas.append((dia, moeda))
        assert moeda == "USD"
        return Decimal("0.5")

    resultado = benchmark(db, symbol="VWCE.DE", fx=fx, ate=date(2024, 1, 7))

    assert resultado.unidades == Decimal("2")
    assert resultado.valor_atual == Decimal("80")
    assert set(chamadas) == {(date(2024, 1, 5), "USD")}


def test_alocacao_por_classe_com_ultimo_snapshot_de_cada_fonte(tmp_path):
    db = _db(tmp_path)
    antigo = store.new_run(db, at=datetime(2026, 1, 1, 9, 0, 0))
    _saldo(db, antigo, "bitcoin", Decimal("0.01"), Decimal("800"), currency="BTC", kind="crypto")
    _posicao(db, antigo, "ibkr", Decimal("9999"))
    recente = store.new_run(db, at=datetime(2026, 2, 1, 9, 0, 0))
    _posicao(db, recente, "ibkr", Decimal("1000"), symbol="VWCE", name="ETF")
    _saldo(db, recente, "ibkr", Decimal("100"), Decimal("100"))
    _posicao(db, recente, "etoro", Decimal("500"), symbol="AAPL", name="Apple")
    _posicao(db, recente, "traderepublic", Decimal("200"), symbol="CSPX", name="ETF")
    _saldo(db, recente, "santander", Decimal("300"), Decimal("300"))
    _saldo(db, recente, "aforro", Decimal("400"), Decimal("400"), kind="savings")
    _posicao(db, recente, "santander", Decimal("999"), symbol="FUNDO", name="Fundo banco")
    db.commit()

    assert alocacao(db) == {
        "Ações/ETFs": Decimal("1700"),
        "Cash": Decimal("400"),
        "Bitcoin": Decimal("800"),
        "Dívida pública": Decimal("400"),
    }


def test_estatisticas_curiosas_sem_valores_absolutos(tmp_path):
    db = _db(tmp_path)
    classify = _classificador(
        {
            "TAP": ("gasto", "Viagens"),
            "ORDENADO": ("receita", "Salário"),
            "PARA POUPANCA": ("interna", "Transferência"),
            "COMPRA IWDA": ("investimento", "Investimento"),
        }
    )
    for booked in (date(2026, 8, 6), date(2026, 9, 6), date(2026, 10, 6), date(2026, 11, 6)):
        _tx(db, booked, Decimal("-10"), "Netflix 42")
    _tx(db, date(2026, 11, 6), Decimal("-30"), "TAP")
    _tx(db, date(2026, 11, 10), Decimal("200"), "ORDENADO")
    _tx(db, date(2026, 11, 11), Decimal("-9999"), "PARA POUPANCA")
    _tx(db, date(2026, 11, 12), Decimal("-9999"), "COMPRA IWDA")
    db.commit()

    frases = estatisticas_curiosas(db, date(2026, 11, 1), date(2026, 11, 30), classify=classify)

    assert frases == [
        "O maior gasto foi em Viagens (75% do período).",
        "Gastas mais à sexta-feira.",
        "As subscrições custam 25% dos teus gastos.",
        "A taxa de poupança foi de 80%.",
    ]
    texto = " ".join(frases)
    assert "€" not in texto
    assert "9999" not in texto
    assert "200" not in texto


def test_estatisticas_sabado_e_periodo_sem_gastos(tmp_path):
    db = _db(tmp_path)
    _tx(db, date(2026, 11, 7), Decimal("-15"), "MERCADO")
    db.commit()
    classify = _classificador({})

    frases = estatisticas_curiosas(db, date(2026, 11, 1), date(2026, 11, 30), classify=classify)
    assert "Gastas mais ao sábado." in frases

    assert estatisticas_curiosas(db, date(2026, 1, 1), date(2026, 1, 31), classify=classify) == [
        "Não há gastos neste período."
    ]
