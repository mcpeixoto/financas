"""Entrada do dashboard: `streamlit run src/financas/dashboard/app.py`."""
from __future__ import annotations


def main() -> None:
    import streamlit as st

    from financas.dashboard.paginas import gastos, investimentos, movimentos, resumo

    st.set_page_config(page_title="Finanças", layout="wide")
    st.sidebar.toggle("Esconder valores", value=True, key="esconder_valores")
    navegacao = st.navigation(
        [
            st.Page(resumo.pagina, title="Resumo", default=True),
            st.Page(gastos.pagina, title="Gastos", url_path="gastos"),
            st.Page(investimentos.pagina, title="Investimentos", url_path="investimentos"),
            st.Page(movimentos.pagina, title="Movimentos", url_path="movimentos"),
        ]
    )
    navegacao.run()


if __name__ == "__main__":
    main()
