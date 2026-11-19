"""Página de resumo do património."""
from __future__ import annotations


def pagina() -> None:
    import streamlit as st
    from datetime import date
    from decimal import Decimal

    from financas.analise import alocacao, estatisticas_curiosas, patrimonio_serie, preencher_patrimonio
    from financas.dashboard.dados import (
        avisos_ultimo_run,
        formatar_data,
        formatar_eur,
        formatar_indice,
        formatar_percentagem,
        ligar,
        mostrar_montante,
        variacao_patrimonio,
        variacao_relativa,
    )
    from financas.dashboard.paginas.comum import esconder_valores
    from financas.dashboard.tema import grafico_area, grafico_donut
    from financas.report import display_name, ordered_sources

    st.title("Resumo")
    esconder = esconder_valores()
    db = ligar()
    try:
        serie = patrimonio_serie(db)
        serie_cheia, preenchida = preencher_patrimonio(serie)
        variacao = variacao_patrimonio(serie)
        classes = alocacao(db)
        avisos = avisos_ultimo_run(db)
        hoje = date.today()
        frases = estatisticas_curiosas(db, date(hoje.year, hoje.month, 1), hoje)
    finally:
        db.close()

    if variacao.dia is None:
        st.info("Ainda não há snapshots.")
    else:
        st.caption(f"Snapshot de {formatar_data(variacao.dia)}.")
        colunas = st.columns(3)
        if esconder:
            base = next((total for _dia, _fontes, total in serie_cheia if total != 0), None)
            atual = serie_cheia[-1][2] if serie_cheia else variacao.total
            indice = Decimal(100) if not base else atual / base * Decimal(100)
            colunas[0].metric("Património", formatar_indice(indice))
            colunas[1].metric(
                "Desde o snapshot anterior",
                formatar_percentagem(variacao_relativa(variacao.delta_anterior, variacao.anterior)),
            )
            colunas[2].metric(
                "Há 30 dias",
                formatar_percentagem(variacao_relativa(variacao.delta_30, variacao.ha_30_dias)),
            )
        else:
            colunas[0].metric("Património", formatar_eur(variacao.total))
            colunas[1].metric(
                "Desde o snapshot anterior",
                mostrar_montante(variacao.delta_anterior, False),
                delta=formatar_percentagem(variacao_relativa(variacao.delta_anterior, variacao.anterior)),
            )
            colunas[2].metric(
                "Há 30 dias",
                mostrar_montante(variacao.delta_30, False),
                delta=formatar_percentagem(variacao_relativa(variacao.delta_30, variacao.ha_30_dias)),
            )

    if preenchida:
        st.caption(
            "Algumas fontes foram preenchidas com o primeiro valor conhecido, "
            "para o total não saltar quando uma fonte aparece. "
            "A variação desde o snapshot anterior compara só fontes presentes nos dois snapshots."
        )

    if serie_cheia:
        fontes = ordered_sources(fonte for _dia, por_fonte, _total in serie_cheia for fonte in por_fonte)
        datas = [dia for dia, _por_fonte, _total in serie_cheia]
        series = [
            (display_name(fonte), [por_fonte.get(fonte, Decimal(0)) for _dia, por_fonte, _total in serie_cheia])
            for fonte in fontes
        ]
        st.subheader("Património por fonte")
        st.plotly_chart(grafico_area(datas, series, esconder), width="stretch")

    if any(valor != 0 for valor in classes.values()):
        st.subheader("Alocação")
        st.plotly_chart(grafico_donut(classes, esconder), width="stretch")
    else:
        st.caption("Sem alocação neste momento.")

    st.subheader("Avisos da última corrida")
    relevantes = [aviso for aviso in avisos if aviso[1] != "ok" or aviso[2]]
    if not relevantes:
        st.caption("Sem avisos na última corrida.")
    for fonte, estado, texto in relevantes:
        mensagem = f"{display_name(fonte)}: {estado}"
        if texto:
            mensagem = f"{mensagem} — {texto}"
        if estado == "ok":
            st.info(mensagem)
        else:
            st.warning(mensagem)

    st.subheader("Este mês")
    if frases:
        for frase in frases:
            st.markdown(f"- {frase}")
    else:
        st.caption("Sem estatísticas neste mês.")
