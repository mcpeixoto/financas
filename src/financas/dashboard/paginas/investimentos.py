"""Página de rentabilidade e comparação com o MSCI World."""
from __future__ import annotations


def pagina() -> None:
    import streamlit as st

    from financas.analise import benchmark, rentabilidade
    from financas.dashboard.dados import (
        curva_capital,
        formatar_eur,
        formatar_percentagem,
        ligar,
        rendimentos_por_ano,
        veredicto_benchmark,
    )
    from financas.dashboard.paginas.comum import esconder_valores
    from financas.dashboard.tema import grafico_colunas, grafico_linhas
    from financas.report import display_name

    st.title("Investimentos")
    esconder = esconder_valores()
    db = ligar()
    try:
        linhas = rentabilidade(db)
        blocos = []
        for linha in linhas:
            fonte = None if linha.fonte == "total" else linha.fonte
            nome = "Total" if fonte is None else display_name(linha.fonte)
            referencia = benchmark(db, source=fonte, symbol="IWDA.UK")
            blocos.append(
                {
                    "nome": nome,
                    "fonte": fonte,
                    "linha": linha,
                    "referencia": referencia,
                    "veredicto": veredicto_benchmark(linha.xirr, referencia.xirr),
                    "curva": curva_capital(db, fonte),
                    "rendimentos": rendimentos_por_ano(db, fonte),
                }
            )
    finally:
        db.close()

    if esconder:
        tabela = [
            {
                "Plataforma": bloco["nome"],
                "Ganho": formatar_percentagem(bloco["linha"].ganho_pct),
                "XIRR": formatar_percentagem(bloco["linha"].xirr),
            }
            for bloco in blocos
        ]
    else:
        tabela = [
            {
                "Plataforma": bloco["nome"],
                "Depositado": formatar_eur(bloco["linha"].depositado_liquido),
                "Valor atual": formatar_eur(bloco["linha"].valor_atual),
                "Ganho": formatar_percentagem(bloco["linha"].ganho_pct),
                "XIRR": formatar_percentagem(bloco["linha"].xirr),
            }
            for bloco in blocos
        ]
    st.dataframe(tabela, hide_index=True, width="stretch")

    for bloco in blocos:
        nome = bloco["nome"]
        st.subheader(nome)
        colunas = st.columns(2)
        colunas[0].metric(f"A tua XIRR · {nome}", formatar_percentagem(bloco["linha"].xirr))
        colunas[1].metric(f"MSCI World · {nome}", formatar_percentagem(bloco["referencia"].xirr))
        st.markdown(f"{nome}: {bloco['veredicto']}")
        if bloco["curva"]:
            st.caption(f"Capital investido acumulado e valor · {nome}")
            datas = [ponto[0] for ponto in bloco["curva"]]
            series = [
                ("Capital investido", [ponto[1] for ponto in bloco["curva"]]),
                ("Valor", [ponto[2] for ponto in bloco["curva"]]),
            ]
            st.plotly_chart(grafico_linhas(datas, series, esconder), width="stretch", key=f"curva-{nome}")
        else:
            st.caption(f"Sem snapshots para a curva de capital · {nome}")
        if bloco["rendimentos"]:
            st.caption(f"Dividendos e juros por ano · {nome}")
            st.plotly_chart(
                grafico_colunas(
                    [str(ano) for ano, _valor in bloco["rendimentos"]],
                    [valor for _ano, valor in bloco["rendimentos"]],
                    esconder,
                    "Dividendos e juros",
                ),
                width="stretch",
                key=f"juros-{nome}",
            )
        else:
            st.caption(f"Sem dividendos nem juros · {nome}")
