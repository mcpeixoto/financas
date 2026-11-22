"""Dashboard: preparação de dados, máscara de valores, cores e fumo das páginas."""
import subprocess
import tomllib
from datetime import date, datetime
from decimal import Decimal

import pytest

from financas import config, store
from financas.analise import ComparacaoCategoria
from financas.dashboard.dados import (
    COR_OUTROS,
    PALETA,
    apresentar_comparacao,
    avisos_ultimo_run,
    cor,
    curva_capital,
    fatias,
    formatar_data,
    formatar_eur,
    formatar_percentagem,
    gravar_regra,
    listar_movimentos,
    mascara_serie,
    mostrar_montante,
    preparar_gastos,
    rendimentos_por_ano,
    resolver_periodo,
    variacao_patrimonio,
    variacao_relativa,
    veredicto_benchmark,
)
from financas.dashboard.tema import grafico_area, grafico_barras, grafico_donut


def test_formato_pt_de_euros_e_datas():
    """Um euro com cêntimos usa espaço nos milhares e vírgula decimal."""
    assert formatar_eur(Decimal("1234.56")) == "1 234,56 €"
    assert formatar_eur(Decimal("-1234.5")) == "-1 234,50 €"
    assert formatar_eur(Decimal("0")) == "0,00 €"
    assert formatar_eur(Decimal("999.996")) == "1 000,00 €"
    assert formatar_data(date(2026, 1, 5)) == "05/01/2026"
    assert formatar_percentagem(Decimal("0.125")) == "12,5%"
    assert formatar_percentagem(Decimal("-0.1")) == "-10,0%"
    assert formatar_percentagem(None) == "—"


def test_mascara_esconde_euros_e_reindexa_a_serie_em_base_100():
    """Com a máscara ligada não há símbolo de euro; a série passa a índice."""
    assert mostrar_montante(Decimal("1234.5"), esconder=False) == "1 234,50 €"
    assert mostrar_montante(Decimal("1234.5"), esconder=True) == "—"
    assert mostrar_montante(None, esconder=False) == "—"
    assert "€" not in mostrar_montante(Decimal("10"), esconder=True)

    valores, rotulos = mascara_serie([Decimal("0"), Decimal("50"), Decimal("100")], esconder=True)
    assert valores == [Decimal("0"), Decimal("100"), Decimal("200")]
    assert rotulos == ["0,0", "100,0", "200,0"]
    assert all("€" not in rotulo for rotulo in rotulos)

    visiveis, rotulos_eur = mascara_serie([Decimal("200"), Decimal("250")], esconder=False)
    assert visiveis == [Decimal("200"), Decimal("250")]
    assert rotulos_eur == ["200,00 €", "250,00 €"]

    com_base, _ = mascara_serie([Decimal("50"), Decimal("100")], esconder=True, base=Decimal("200"))
    assert com_base == [Decimal("25"), Decimal("50")]


def test_cores_fixas_por_nome_e_outros_cinzento():
    """A cor depende do nome, não da ordem em que a série aparece no gráfico."""
    assert PALETA == (
        "#2a78d6",
        "#eb6834",
        "#1baf7a",
        "#eda100",
        "#e87ba4",
        "#008300",
        "#6250d6",
        "#e34948",
    )
    assert COR_OUTROS == "#888780"
    assert cor("Outros") == COR_OUTROS
    assert cor("outros") == COR_OUTROS
    assert cor("Supermercado") == PALETA[0]
    assert cor("Restauração") == PALETA[1]
    assert cor("Casa") == PALETA[3]
    ordem = [cor(nome) for nome in ("Casa", "Supermercado", "Outros")]
    invertida = [cor(nome) for nome in ("Outros", "Supermercado", "Casa")]
    assert ordem[1] == invertida[1] == PALETA[0]
    assert ordem[0] == invertida[2] == PALETA[3]
    assert cor("ibkr") == cor("IBKR")
    assert cor("Categoria Nova") == cor("Categoria Nova")
    assert cor("Categoria Nova") in PALETA


def test_periodos_calendario_e_intervalo_personalizado():
    hoje = date(2026, 11, 19)
    assert resolver_periodo("este mês", hoje) == (date(2026, 11, 1), hoje)
    assert resolver_periodo("mês passado", hoje) == (date(2026, 10, 1), date(2026, 10, 31))
    assert resolver_periodo("mês passado", date(2026, 1, 15)) == (date(2025, 12, 1), date(2025, 12, 31))
    assert resolver_periodo("últimos 3 meses", hoje) == (date(2026, 8, 19), hoje)
    assert resolver_periodo("últimos 12 meses", hoje) == (date(2025, 11, 19), hoje)
    assert resolver_periodo("últimos 3 meses", date(2026, 3, 31)) == (date(2025, 12, 31), date(2026, 3, 31))
    assert resolver_periodo("personalizado", hoje, date(2026, 1, 2), date(2026, 3, 4)) == (
        date(2026, 1, 2),
        date(2026, 3, 4),
    )
    with pytest.raises(ValueError):
        resolver_periodo("personalizado", hoje)
    with pytest.raises(ValueError):
        resolver_periodo("hoje", hoje)


def test_variacao_de_patrimonio_contra_snapshot_anterior_e_ha_30_dias():
    serie = [
        (date(2026, 1, 1), {"a": Decimal("100")}, Decimal("100")),
        (date(2026, 10, 1), {"a": Decimal("150")}, Decimal("150")),
        (date(2026, 11, 1), {"a": Decimal("180")}, Decimal("180")),
    ]
    variacao = variacao_patrimonio(serie)
    assert variacao.total == Decimal("180")
    assert variacao.dia == date(2026, 11, 1)
    assert variacao.anterior == Decimal("150")
    assert variacao.delta_anterior == Decimal("30")
    assert variacao.ha_30_dias == Decimal("150")
    assert variacao.dia_30 == date(2026, 10, 1)
    assert variacao.delta_30 == Decimal("30")
    assert variacao_relativa(Decimal("30"), Decimal("150")) == Decimal("0.2")
    assert variacao_relativa(Decimal("30"), Decimal("0")) is None

    so_um = variacao_patrimonio([(date(2026, 11, 1), {}, Decimal("10"))])
    assert so_um.total == Decimal("10")
    assert so_um.anterior is None
    assert so_um.ha_30_dias is None
    vazio = variacao_patrimonio([])
    assert vazio.total == Decimal("0")
    assert vazio.dia is None


def test_veredicto_em_pontos_percentuais_por_ano():
    assert veredicto_benchmark(Decimal("0.10"), Decimal("0.075")) == "Bateu o índice por 2,5 pp/ano."
    assert veredicto_benchmark(Decimal("0.10"), Decimal("0.11")) == "Ficou atrás do índice por 1,0 pp/ano."
    assert veredicto_benchmark(Decimal("0.10"), Decimal("0.1004")) == "Empatou com o índice."
    assert veredicto_benchmark(None, Decimal("0.1")) == "Sem XIRR para comparar com o índice."
    assert veredicto_benchmark(Decimal("0.1"), None) == "Sem XIRR para comparar com o índice."


def test_fatias_escondidas_sao_percentagens_e_a_cor_nao_segue_a_posicao():
    escondidas = fatias({"Outros": Decimal("25"), "Supermercado": Decimal("75")}, esconder=True)
    por_nome = {fatia.nome: fatia for fatia in escondidas}
    assert por_nome["Outros"].cor == COR_OUTROS
    assert por_nome["Supermercado"].cor == PALETA[0]
    assert por_nome["Supermercado"].valor == Decimal("75")
    assert por_nome["Outros"].texto == "25,0%"
    assert all("€" not in fatia.texto for fatia in escondidas)

    outra_ordem = fatias({"Supermercado": Decimal("75"), "Outros": Decimal("25")}, esconder=True)
    assert {fatia.nome: fatia.cor for fatia in outra_ordem} == {fatia.nome: fatia.cor for fatia in escondidas}

    visiveis = fatias({"Supermercado": Decimal("75")}, esconder=False)
    assert visiveis[0].texto == "75,00 € (100,0%)"
    assert visiveis[0].valor == Decimal("75")
    assert fatias({}, esconder=False) == []


def test_comparacao_escondida_so_mostra_percentagens():
    linha = ComparacaoCategoria("Supermercado", Decimal("50"), Decimal("40"), Decimal("30"))
    visivel = apresentar_comparacao([linha], esconder=False)[0]
    assert visivel.este_mes == "50,00 €"
    assert visivel.mes_anterior == "40,00 €"
    assert visivel.media_3_meses == "30,00 €"
    assert visivel.vs_anterior == "25,0%"
    assert visivel.vs_media == "66,7%"

    escondida = apresentar_comparacao([linha], esconder=True)[0]
    assert escondida.este_mes == "—"
    assert escondida.mes_anterior == "—"
    assert escondida.media_3_meses == "—"
    assert escondida.vs_anterior == "25,0%"
    assert escondida.vs_media == "66,7%"
    assert "€" not in " ".join(escondida.__dict__.values())

    zero = apresentar_comparacao(
        [ComparacaoCategoria("Casa", Decimal("10"), Decimal("0"), Decimal("0"))],
        esconder=False,
    )[0]
    assert zero.vs_anterior == "—"
    assert zero.vs_media == "—"


def test_gravar_regra_no_inicio_preserva_o_resto_e_escapa_a_descricao_normalizada(tmp_path):
    caminho = tmp_path / "categorias.toml"
    caminho.write_text(
        '# nota pessoal\n\ncontas_proprias = ["CONTA-TESTE"]\n\n'
        '[[regra]]\ncategoria = "Casa"\ntipo = "gasto"\npadrao = "ikea"\n',
        encoding="utf-8",
    )
    gravar_regra(caminho, "Pingo Doce 123", "Supermercado", "gasto")
    texto = caminho.read_text(encoding="utf-8")
    assert texto.index("Supermercado") < texto.index("Casa")
    assert "# nota pessoal" in texto
    assert "ikea" in texto
    assert "CONTA-TESTE" in texto
    documento = tomllib.loads(texto)
    assert documento["regra"][0]["categoria"] == "Supermercado"
    assert documento["regra"][0]["tipo"] == "gasto"
    assert documento["regra"][0]["padrao"] == r"Pingo\ Doce"
    assert documento["regra"][1]["padrao"] == "ikea"
    assert documento["contas_proprias"] == ["CONTA-TESTE"]


def test_gravar_regra_cria_o_ficheiro_e_passa_a_classificar(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    from financas.categorias import carregar_regras, classify

    descricao = "Padaria do Bairro 44"
    assert classify("santander", "conta", descricao, Decimal("-5")).categoria == "Outros"
    caminho = tmp_path / "categorias.toml"
    gravar_regra(caminho, descricao, "Restauração", "gasto")
    assert caminho.is_file()
    classe = classify("santander", "conta", descricao, Decimal("-5"), carregar_regras())
    assert classe.tipo == "gasto"
    assert classe.categoria == "Restauração"


def test_gravar_regra_recusa_descricao_vazia_depois_de_normalizar(tmp_path):
    caminho = tmp_path / "categorias.toml"
    with pytest.raises(ValueError):
        gravar_regra(caminho, "12345 !!!", "Casa", "gasto")
    assert not caminho.exists()


def _db(tmp_path, nome="financas.db"):
    return store.connect(tmp_path / nome)


def _tx(db, booked, amount, description, source="santander", account="conta-a", currency="EUR"):
    _tx.n += 1
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (source, account, booked.isoformat(), str(amount), currency, description, f"tx{_tx.n}", 1),
    )


_tx.n = 0


def _semear_gastos(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    db = _db(tmp_path)
    db.execute("CREATE TABLE IF NOT EXISTS run_status(run_id INT, source TEXT, status TEXT, warnings TEXT)")
    antigo = store.new_run(db, at=datetime(2026, 10, 1, 9, 0, 0))
    recente = store.new_run(db, at=datetime(2026, 11, 1, 9, 0, 0))
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (antigo, "santander", "conta-a", "EUR", "200", "cash", "200"),
    )
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (recente, "santander", "conta-a", "EUR", "300", "cash", "300"),
    )
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (antigo, "ibkr", "conta", "ETF", "Fundo", "1", "1000", "EUR", None, "1000"),
    )
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (recente, "ibkr", "conta", "ETF", "Fundo", "1", "1100", "EUR", None, "1100"),
    )
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (recente, "bitcoin", "carteira", "BTC", "0.01", "crypto", "200"),
    )
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (recente, "aforro", "serie", "EUR", "100", "savings", "100"),
    )
    for dia, valor, descricao in (
        (date(2026, 9, 2), Decimal("-20"), "pingo doce"),
        (date(2026, 9, 15), Decimal("-10"), "netflix"),
        (date(2026, 10, 2), Decimal("-20"), "pingo doce"),
        (date(2026, 10, 15), Decimal("-10"), "netflix"),
        (date(2026, 11, 2), Decimal("-30"), "pingo doce"),
        (date(2026, 11, 14), Decimal("-10"), "netflix"),
        (date(2026, 11, 5), Decimal("1000"), "vencimento"),
        (date(2026, 11, 6), Decimal("-500"), "transfer to poupanca"),
    ):
        _tx(db, dia, valor, descricao)
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2025-01-01", "1000", "deposito", "entrada", "cf1"),
    )
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2025-06-01", "10", "juros", "juros", "cf2"),
    )
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2026-03-01", "40", "dividendo", "dividendo", "cf3"),
    )
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("IWDA.UK", "2025-01-01", "100", "EUR"))
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("IWDA.UK", "2026-11-01", "110", "EUR"))
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (antigo, "ibkr", "erro: antigo", "nao mostrar"))
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (recente, "ibkr", "ok", "limite de teste"))
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (recente, "santander", "precisa de ti: renova o consentimento", ""),
    )
    db.commit()
    return db


def test_avisos_sao_so_os_do_ultimo_run(tmp_path, monkeypatch):
    db = _semear_gastos(tmp_path, monkeypatch)
    assert avisos_ultimo_run(db) == [
        ("ibkr", "ok", "limite de teste"),
        ("santander", "precisa de ti: renova o consentimento", ""),
    ]
    vazio = _db(tmp_path, "vazio.db")
    assert avisos_ultimo_run(vazio) == []


def test_preparar_gastos_agrega_meses_sem_internas(tmp_path, monkeypatch):
    db = _semear_gastos(tmp_path, monkeypatch)
    painel = preparar_gastos(db, date(2026, 9, 1), date(2026, 11, 30), "mes")
    assert painel.por_categoria["Supermercado"] == Decimal("70")
    assert painel.por_categoria["Tecnologia e subscrições"] == Decimal("30")
    assert sum(painel.por_categoria.values(), start=Decimal(0)) == Decimal("100")
    assert painel.empilhado["Supermercado"]["2026-09"] == Decimal("20")
    assert painel.empilhado["Supermercado"]["2026-11"] == Decimal("30")
    assert painel.periodos == ("2026-09", "2026-10", "2026-11")
    assert painel.comerciantes[0] == ("pingo doce", Decimal("70"))
    assert any(
        item.nome == "netflix" and item.periodicidade == "mensal" and item.custo_anual == Decimal("120")
        for item in painel.subscricoes
    )
    assert painel.dias[0] == ("segunda-feira", Decimal("30"))
    assert len(painel.dias) == 7
    assert painel.taxa == Decimal("0.9")
    supermercado = next(linha for linha in painel.comparacao if linha.categoria == "Supermercado")
    assert supermercado.este_mes == Decimal("30")
    assert supermercado.mes_anterior == Decimal("20")
    assert supermercado.media_3_meses == Decimal("40") / Decimal("3")


def test_gastos_por_semana_iso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    db = _db(tmp_path, "semana.db")
    _tx(db, date(2026, 1, 1), Decimal("-15"), "pingo doce")
    db.commit()
    painel = preparar_gastos(db, date(2026, 1, 1), date(2026, 1, 7), "semana")
    assert painel.periodos == ("2026-W01",)
    assert painel.empilhado["Supermercado"]["2026-W01"] == Decimal("15")


def test_listar_movimentos_filtra_texto_tipo_categoria_e_datas(tmp_path, monkeypatch):
    db = _semear_gastos(tmp_path, monkeypatch)
    assert len(listar_movimentos(db, texto="PINGO")) == 3
    receitas = listar_movimentos(db, tipo="receita")
    assert [linha.descricao for linha in receitas] == ["vencimento"]
    assert len(listar_movimentos(db, categoria="Tecnologia e subscrições")) == 3
    novembro = listar_movimentos(db, inicio=date(2026, 11, 1), fim=date(2026, 11, 30))
    assert len(novembro) == 4
    assert listar_movimentos(db, fonte="revolut") == []
    assert len(listar_movimentos(db, conta="conta-a")) == 8
    assert listar_movimentos(db, conta="outra") == []


def test_curva_de_capital_e_rendimentos_por_ano(tmp_path):
    db = _db(tmp_path, "inv.db")
    primeiro = store.new_run(db, at=datetime(2026, 10, 1, 9, 0, 0))
    segundo = store.new_run(db, at=datetime(2026, 11, 1, 9, 0, 0))
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (primeiro, "ibkr", "conta", "ETF", "Fundo", "1", "1000", "EUR", None, "1000"),
    )
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (segundo, "ibkr", "conta", "ETF", "Fundo", "1", "1100", "EUR", None, "1100"),
    )
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2025-01-01", "1000", "deposito", "", "cf1"),
    )
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2025-06-01", "10", "juros", "", "cf2"),
    )
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        ("ibkr", "2026-03-01", "40", "dividendo", "", "cf3"),
    )
    db.commit()

    assert curva_capital(db, "ibkr") == [
        (date(2026, 10, 1), Decimal("1000"), Decimal("1000")),
        (date(2026, 11, 1), Decimal("1000"), Decimal("1100")),
    ]
    assert rendimentos_por_ano(db, "ibkr") == [(2025, Decimal("10")), (2026, Decimal("40"))]
    assert rendimentos_por_ano(db, None) == [(2025, Decimal("10")), (2026, Decimal("40"))]


def test_graficos_partilham_cores_fundo_transparente_e_mascara():
    datas = [date(2026, 1, 1), date(2026, 2, 1)]
    ibkr = [Decimal("200"), Decimal("250")]
    outros = [Decimal("40"), Decimal("40")]
    fig_a = grafico_area(datas, [("IBKR", ibkr), ("Outros", outros)], esconder=True)
    fig_b = grafico_area(datas, [("Outros", outros), ("IBKR", ibkr)], esconder=True)
    cores_a = {traco.name: traco.line.color for traco in fig_a.data}
    cores_b = {traco.name: traco.line.color for traco in fig_b.data}
    assert cores_a == cores_b
    assert cores_a["Outros"] == "#888780"
    assert cores_a["IBKR"] == cor("IBKR")
    assert "01/01/2026" in fig_a.data[0].hovertext[0]
    assert all("€" not in texto for traco in fig_a.data for texto in traco.hovertext)
    assert str(fig_a.layout.paper_bgcolor).replace(" ", "") == "rgba(0,0,0,0)"
    assert str(fig_a.layout.plot_bgcolor).replace(" ", "") == "rgba(0,0,0,0)"
    assert all(getattr(traco, "yaxis", None) in (None, "y") for traco in fig_a.data)
    ibkr_a = next(traco for traco in fig_a.data if traco.name == "IBKR")
    assert abs(ibkr_a.y[0] - (200 / 240 * 100)) < 1e-6

    visivel = grafico_area(datas, [("IBKR", ibkr)], esconder=False)
    assert "200,00 €" in visivel.data[0].hovertext[0]

    donut = grafico_donut({"Outros": Decimal("25"), "Supermercado": Decimal("75")}, esconder=True)
    assert all("€" not in texto for texto in donut.data[0].hovertext)
    rotulos = list(donut.data[0].labels)
    cores = list(donut.data[0].marker.colors)
    assert dict(zip(rotulos, cores, strict=True))["Outros"] == "#888780"
    assert dict(zip(rotulos, cores, strict=True))["Supermercado"] == cor("Supermercado")

    barras = grafico_barras(
        ["2026-11"],
        {"Supermercado": [Decimal("300")], "Casa": [Decimal("700")]},
        esconder=True,
    )
    assert barras.layout.barmode == "stack"
    assert all("€" not in texto for traco in barras.data for texto in traco.hovertext)
    assert sorted(round(valor, 5) for traco in barras.data for valor in traco.y) == [30.0, 70.0]
    assert all(getattr(traco, "yaxis", None) in (None, "y") for traco in barras.data)
    mostradas = grafico_barras(["2026-11"], {"Supermercado": [Decimal("300")]}, esconder=False)
    assert "300,00 €" in mostradas.data[0].hovertext[0]


def test_cli_dashboard_abre_a_pagina_local(monkeypatch):
    portas = []
    monkeypatch.setattr("financas.web.server.serve", lambda porta: portas.append(porta))
    from financas import cli

    assert cli.main(["dashboard"]) == 0
    assert cli.main(["dashboard", "--porta", "8502"]) == 0
    assert portas == [8501, 8502]
