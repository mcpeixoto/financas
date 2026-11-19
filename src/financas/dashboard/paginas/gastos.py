"""Página de gastos, subscrições e taxa de poupança."""
from __future__ import annotations


def pagina() -> None:
    import streamlit as st
    from datetime import date
    from decimal import Decimal

    from financas.dashboard.dados import (
        apresentar_comparacao,
        formatar_eur,
        formatar_percentagem,
        ligar,
        mostrar_montante,
        preparar_gastos,
        resolver_periodo,
    )
    from financas.dashboard.paginas.comum import esconder_valores
    from financas.dashboard.tema import grafico_barras, grafico_colunas, grafico_donut

    st.title("Gastos")
    esconder = esconder_valores()
    hoje = date.today()
    escolha = st.selectbox(
        "Período",
        ["este mês", "mês passado", "últimos 3 meses", "últimos 12 meses", "personalizado"],
        key="periodo_gastos",
    )
    frequencia = "semana" if st.selectbox("Frequência", ["Mês", "Semana"], key="freq_gastos") == "Semana" else "mes"
    if escolha == "personalizado":
        inicio = st.date_input("De", value=date(hoje.year, hoje.month, 1), key="gastos_de")
        fim = st.date_input("Até", value=hoje, key="gastos_ate")
        if inicio > fim:
            st.warning("A data de início é posterior à de fim. Troquei as duas.")
            inicio, fim = fim, inicio
    else:
        inicio, fim = resolver_periodo(escolha, hoje)

    db = ligar()
    try:
        painel = preparar_gastos(db, inicio, fim, frequencia)
    finally:
        db.close()

    for aviso in painel.avisos:
        st.warning(aviso)

    if painel.por_categoria:
        st.subheader("Por categoria")
        st.plotly_chart(grafico_donut(painel.por_categoria, esconder), width="stretch")
    else:
        st.caption("Sem gastos neste período.")

    if painel.periodos:
        st.subheader("Por período")
        ordenadas = sorted(
            painel.empilhado,
            key=lambda nome: (nome.casefold() == "outros", -sum(painel.empilhado[nome].values(), start=Decimal(0))),
        )
        por_categoria = {
            nome: [painel.empilhado[nome].get(periodo, Decimal(0)) for periodo in painel.periodos]
            for nome in ordenadas
        }
        st.plotly_chart(grafico_barras(list(painel.periodos), por_categoria, esconder), width="stretch")

    total = sum(painel.por_categoria.values(), start=Decimal(0))
    if painel.comerciantes:
        st.subheader("Top comerciantes")
        linhas = []
        for nome, valor in painel.comerciantes:
            peso = None if total == 0 else valor / total
            linhas.append(
                {
                    "Comerciante": nome,
                    "Valor": mostrar_montante(valor, esconder),
                    "Peso": formatar_percentagem(peso),
                }
            )
        st.dataframe(linhas, hide_index=True, width="stretch")

    st.subheader("Subscrições")
    if painel.subscricoes:
        total_anual = sum((item.custo_anual for item in painel.subscricoes), start=Decimal(0))
        linhas = []
        for item in painel.subscricoes:
            peso = None if total_anual == 0 else item.custo_anual / total_anual
            linhas.append(
                {
                    "Nome": item.nome,
                    "Periodicidade": item.periodicidade,
                    "Custo anual": formatar_eur(item.custo_anual) if not esconder else formatar_percentagem(peso),
                }
            )
        st.dataframe(linhas, hide_index=True, width="stretch")
    else:
        st.caption("Nenhuma subscrição detetada.")

    st.subheader("Por dia da semana")
    st.plotly_chart(
        grafico_colunas(
            [nome for nome, _valor in painel.dias],
            [valor for _nome, valor in painel.dias],
            esconder,
            "Gastos",
        ),
        width="stretch",
    )

    st.metric("Taxa de poupança", formatar_percentagem(painel.taxa))

    st.subheader("Comparação com o mês anterior")
    vistas = apresentar_comparacao(painel.comparacao, esconder)
    if not vistas:
        st.caption("Sem categorias para comparar.")
    elif esconder:
        st.dataframe(
            [
                {
                    "Categoria": vista.categoria,
                    "Vs mês anterior": vista.vs_anterior,
                    "Vs média 3 meses": vista.vs_media,
                }
                for vista in vistas
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.dataframe(
            [
                {
                    "Categoria": vista.categoria,
                    "Este mês": vista.este_mes,
                    "Mês anterior": vista.mes_anterior,
                    "Média 3 meses": vista.media_3_meses,
                    "Vs mês anterior": vista.vs_anterior,
                    "Vs média 3 meses": vista.vs_media,
                }
                for vista in vistas
            ],
            hide_index=True,
            width="stretch",
        )
