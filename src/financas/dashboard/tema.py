"""Gráficos Plotly. O fundo é transparente e não há eixo secundário."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import plotly.graph_objects as go

from financas.dashboard.dados import (
    PALETA,
    cor,
    fatias,
    formatar_data,
    formatar_eur,
    formatar_percentagem,
    mascara_serie,
)

_GRELLA = "rgba(128,128,128,0.18)"


def aplicar(fig: go.Figure, esconder: bool, eixo_y: str | None = None) -> go.Figure:
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        separators=", ",
        margin=dict(l=48, r=16, t=36, b=48),
        legend=dict(orientation="h", yanchor="top", y=-0.2),
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridcolor=_GRELLA, zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor=_GRELLA, zeroline=False, title=eixo_y)
    if eixo_y and not esconder:
        fig.update_yaxes(ticksuffix=" €")
    return fig


def _datas_texto(datas, rotulos, nome: str) -> list[str]:
    textos = []
    for dia, rotulo in zip(datas, rotulos, strict=True):
        quando = formatar_data(dia) if isinstance(dia, date) else str(dia)
        textos.append(f"{quando} · {nome}: {rotulo}")
    return textos


def grafico_area(datas, series, esconder: bool) -> go.Figure:
    totais = [
        sum((valores[indice] for _nome, valores in series), start=Decimal(0))
        for indice in range(len(datas))
    ]
    base = next((valor for valor in totais if valor != 0), None) if esconder else None
    fig = go.Figure()
    for nome, valores in series:
        convertidos, rotulos = mascara_serie(list(valores), esconder, base=base)
        fig.add_trace(
            go.Scatter(
                name=nome,
                x=list(datas),
                y=[float(valor) for valor in convertidos],
                mode="lines",
                stackgroup="patrimonio",
                line=dict(color=cor(nome), width=1.5),
                hovertext=_datas_texto(datas, rotulos, nome),
                hoverinfo="text",
            )
        )
    aplicar(fig, esconder, "Índice (base 100)" if esconder else "EUR")
    fig.update_xaxes(tickformat="%d/%m/%Y")
    return fig


def grafico_linhas(datas, series, esconder: bool) -> go.Figure:
    base = None
    if esconder:
        for _nome, valores in series:
            base = next((valor for valor in valores if valor != 0), None)
            if base is not None:
                break
    fig = go.Figure()
    for nome, valores in series:
        convertidos, rotulos = mascara_serie(list(valores), esconder, base=base)
        fig.add_trace(
            go.Scatter(
                name=nome,
                x=list(datas),
                y=[float(valor) for valor in convertidos],
                mode="lines+markers",
                line=dict(color=cor(nome), width=2),
                hovertext=_datas_texto(datas, rotulos, nome),
                hoverinfo="text",
            )
        )
    aplicar(fig, esconder, "Índice (base 100)" if esconder else "EUR")
    fig.update_xaxes(tickformat="%d/%m/%Y")
    return fig


def grafico_donut(totais: dict[str, Decimal], esconder: bool) -> go.Figure:
    partes = fatias(totais, esconder)
    fig = go.Figure(
        go.Pie(
            labels=[parte.nome for parte in partes],
            values=[float(parte.valor) for parte in partes],
            hole=0.55,
            marker=dict(colors=[parte.cor for parte in partes]),
            sort=False,
            textinfo="percent",
            hovertext=[parte.texto for parte in partes],
            hoverinfo="text",
        )
    )
    return aplicar(fig, esconder)


def grafico_barras(periodos, por_categoria: dict[str, list[Decimal]], esconder: bool) -> go.Figure:
    fig = go.Figure()
    colunas = [
        sum((valores[indice] for valores in por_categoria.values()), start=Decimal(0))
        for indice in range(len(periodos))
    ]
    for nome, valores in por_categoria.items():
        if esconder:
            convertidos = []
            rotulos = []
            for indice, valor in enumerate(valores):
                total = colunas[indice]
                if total == 0:
                    convertidos.append(Decimal(0))
                    rotulos.append("0,0%")
                else:
                    peso = Decimal(valor) / total
                    convertidos.append(peso * Decimal(100))
                    rotulos.append(formatar_percentagem(peso))
        else:
            convertidos = [Decimal(valor) for valor in valores]
            rotulos = [formatar_eur(valor) for valor in convertidos]
        fig.add_trace(
            go.Bar(
                name=nome,
                x=list(periodos),
                y=[float(valor) for valor in convertidos],
                marker_color=cor(nome),
                hovertext=rotulos,
                hoverinfo="text",
            )
        )
    fig.update_layout(barmode="stack")
    aplicar(fig, esconder, "Percentagem" if esconder else "EUR")
    fig.update_xaxes(type="category")
    return fig


def grafico_colunas(rotulos, valores, esconder: bool, nome: str) -> go.Figure:
    lista = [Decimal(valor) for valor in valores]
    if esconder:
        total = sum(lista, start=Decimal(0))
        if total == 0:
            convertidos = [Decimal(0) for _ in lista]
            textos = ["0,0%" for _ in lista]
        else:
            convertidos = [valor / total * Decimal(100) for valor in lista]
            textos = [formatar_percentagem(valor / total) for valor in lista]
        eixo = "Percentagem"
    else:
        convertidos = lista
        textos = [formatar_eur(valor) for valor in lista]
        eixo = "EUR"
    fig = go.Figure(
        go.Bar(
            name=nome,
            x=list(rotulos),
            y=[float(valor) for valor in convertidos],
            marker_color=PALETA[0],
            hovertext=textos,
            hoverinfo="text",
        )
    )
    aplicar(fig, esconder, eixo)
    fig.update_xaxes(type="category")
    return fig
