"""Movimentos filtráveis e correção de categoria."""
from __future__ import annotations


def pagina() -> None:
    import streamlit as st

    from financas import config
    from financas.dashboard.dados import (
        formatar_data,
        formatar_decimal,
        gravar_regra,
        ligar,
        listar_movimentos,
        mostrar_montante,
    )
    from financas.dashboard.paginas.comum import esconder_valores
    from financas.dashboard.paginas.movimentos import _texto_valor
    from financas.report import display_name

    st.title("Movimentos")
    esconder = esconder_valores()
    db = ligar()
    try:
        todos = listar_movimentos(db)
        fontes = sorted({linha.fonte for linha in todos})
        contas = sorted({linha.conta for linha in todos})
        categorias = sorted({linha.categoria for linha in todos})
        nomes_fonte = {fonte: display_name(fonte) for fonte in fontes}
        opcoes_fonte = ["Todas", *[nomes_fonte[fonte] for fonte in fontes]]
        escolha_fonte = st.selectbox("Fonte", opcoes_fonte, key="filtro_fonte")
        fonte = None if escolha_fonte == "Todas" else next(item for item, nome in nomes_fonte.items() if nome == escolha_fonte)
        conta = st.selectbox("Conta", ["Todas", *contas], key="filtro_conta")
        categoria = st.selectbox("Categoria", ["Todas", *categorias], key="filtro_categoria")
        tipo = st.selectbox(
            "Tipo",
            ["Todos", "gasto", "receita", "interna", "investimento"],
            key="filtro_tipo",
        )
        texto = st.text_input("Texto", key="filtro_texto").strip()
        inicio = fim = None
        if todos:
            inicio = st.date_input("De", value=min(linha.data for linha in todos), key="filtro_de")
            fim = st.date_input("Até", value=max(linha.data for linha in todos), key="filtro_ate")
            if inicio > fim:
                st.warning("A data de início é posterior à de fim. Troquei as duas.")
                inicio, fim = fim, inicio
        filtrados = listar_movimentos(
            db,
            fonte=fonte,
            conta=None if conta == "Todas" else conta,
            categoria=None if categoria == "Todas" else categoria,
            tipo=None if tipo == "Todos" else tipo,
            texto=texto or None,
            inicio=inicio,
            fim=fim,
        )
    finally:
        db.close()

    if not filtrados:
        st.caption("Sem movimentos para estes filtros.")
    else:
        st.dataframe(
            [
                {
                    "Data": formatar_data(linha.data),
                    "Fonte": display_name(linha.fonte),
                    "Conta": linha.conta,
                    "Descrição": linha.descricao,
                    "Categoria": linha.categoria,
                    "Tipo": linha.tipo,
                    "Valor": _texto_valor(linha, esconder, mostrar_montante, formatar_decimal),
                }
                for linha in filtrados
            ],
            hide_index=True,
            width="stretch",
        )

    st.subheader("Corrigir categoria")
    if not todos:
        st.caption("Sem movimentos para corrigir.")
        return
    descricoes = sorted({linha.descricao for linha in todos})
    descricao = st.selectbox("Descrição", descricoes, key="correcao_descricao")
    tipo_novo = st.selectbox(
        "Tipo",
        ["gasto", "receita", "interna", "investimento"],
        key="correcao_tipo",
    )
    categoria_nova = st.text_input("Categoria", key="correcao_categoria").strip()
    if st.button("Guardar regra", key="guardar_regra"):
        if not categoria_nova:
            st.error("Indica a categoria.")
        else:
            try:
                gravar_regra(config.DATA / "categorias.toml", descricao, categoria_nova, tipo_novo)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("Regra gravada no início de categorias.toml.")
                st.rerun()


def _texto_valor(linha, esconder: bool, mostrar_montante, formatar_decimal) -> str:
    if esconder:
        return mostrar_montante(linha.valor, True)
    if linha.moeda == "EUR":
        return mostrar_montante(linha.valor, False)
    return f"{formatar_decimal(linha.valor, 2)} {linha.moeda}"
