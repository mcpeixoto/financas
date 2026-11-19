"""Cache das contas pesadas do dashboard.

A chave é a data de modificação do ficheiro da base e os parâmetros da conta.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st


def investimentos(symbol: str = "IWDA.AS"):
    from financas import config

    caminho = config.DATA / "financas.db"
    try:
        mtime = caminho.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return _investimentos(str(caminho), mtime, symbol)


@st.cache_data(show_spinner=False)
def _investimentos(caminho: str, _mtime: int, symbol: str):
    """A chave é o caminho, a data de modificação da base e o símbolo."""
    from financas import store
    from financas.dashboard.dados import montar_investimentos

    db = store.connect(Path(caminho))
    try:
        return montar_investimentos(db, symbol)
    finally:
        db.close()
