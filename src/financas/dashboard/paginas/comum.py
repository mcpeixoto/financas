"""Estado partilhado das páginas."""
from __future__ import annotations


def esconder_valores() -> bool:
    """O interruptor da barra lateral. Ligado quando a página corre sozinha."""
    import streamlit as st

    return bool(st.session_state.get("esconder_valores", True))
