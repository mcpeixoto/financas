"""Relatórios semanais e mensais em HTML, autónomos e offline."""
from __future__ import annotations

import html
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import plotly.graph_objects as go

from financas.analise import (
    alocacao,
    benchmark,
    comparacao_mes,
    estatisticas_curiosas,
    gastos_por_categoria,
    gastos_por_dia_semana,
    patrimonio_serie,
    rentabilidade,
    subscricoes,
    taxa_poupanca,
    top_comerciantes,
)
from financas.dashboard.dados import (
    PALETA,
    apresentar_comparacao,
    fatias,
    formatar_eur,
    formatar_percentagem,
    mascara_serie,
    variacao_relativa,
    veredicto_benchmark,
)
from financas.report import display_name

_MESES = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 1rem;
  width: 100%;
  max-width: 900px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  line-height: 1.45;
  background: #f6f5f2;
  color: #1d1c1a;
}
h1 { font-size: 1.5rem; line-height: 1.25; margin: 0 0 0.25rem; }
.periodo { margin: 0 0 1rem; color: #5c5852; }
h2 { font-size: 1.15rem; margin: 1.6rem 0 0.6rem; }
p, li { margin: 0.35rem 0; }
.tabela { width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td {
  text-align: left;
  padding: 0.4rem 0.35rem;
  border-bottom: 1px solid #ddd;
  vertical-align: top;
}
.grafico, .plotly-graph-div { width: 100%; max-width: 100%; }
@media (prefers-color-scheme: dark) {
  body { background: #161513; color: #f4f1ea; }
  .periodo { color: #b7b1a8; }
  th, td { border-bottom-color: #3a3834; }
}
"""


class _Pagina:
    def __init__(self) -> None:
        self._com_js = False

    def grafico(self, fig: go.Figure) -> str:
        bloco = fig.to_html(
            full_html=False,
            include_plotlyjs="inline" if not self._com_js else False,
            config={"responsive": True, "displayModeBar": False},
            default_width="100%",
            default_height="420px",
        )
        self._com_js = True
        return f'<div class="grafico">{bloco}</div>'


def semana_por_omissao(hoje: date | None = None) -> str:
    """Semana ISO anterior à que contém ``hoje``. À segunda, não é a semana que começa hoje."""
    hoje = date.today() if hoje is None else hoje
    segunda = hoje - timedelta(days=hoje.weekday())
    anterior = segunda - timedelta(days=7)
    iso = anterior.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def mes_por_omissao(hoje: date | None = None) -> str:
    """Mês civil anterior. Em janeiro, dezembro do ano anterior."""
    hoje = date.today() if hoje is None else hoje
    anterior = date(hoje.year, hoje.month, 1) - timedelta(days=1)
    return f"{anterior.year:04d}-{anterior.month:02d}"


def escrever_semanal(db, semana: str | None, com_valores: bool, pasta: Path) -> Path:
    token = semana or semana_por_omissao()
    inicio, fim = _intervalo_semana(token)
    documento = _documento(
        f"Semana {token}",
        _intervalo_legivel(inicio, fim),
        _miolo_periodo(db, inicio, fim, com_valores, semanas=True),
    )
    return _gravar(pasta, f"{token}.html", documento)


def escrever_mensal(db, mes: str | None, com_valores: bool, pasta: Path) -> Path:
    token = mes or mes_por_omissao()
    inicio, fim = _intervalo_mes(token)
    ano, numero = int(token[:4]), int(token[5:7])
    titulo = f"{_MESES[numero - 1].capitalize()} de {ano}"
    documento = _documento(
        titulo,
        _intervalo_legivel(inicio, fim),
        _miolo_periodo(db, inicio, fim, com_valores, semanas=False),
    )
    return _gravar(pasta, f"{token}.html", documento)


def _miolo_periodo(db, inicio: date, fim: date, com_valores: bool, semanas: bool) -> str:
    esconder = not com_valores
    pagina = _Pagina()
    mapa = gastos_por_categoria(db, inicio, fim)
    total = sum(mapa.values(), start=Decimal(0))
    partes = [
        _seccao("Curiosidades", _lista(estatisticas_curiosas(db, inicio, fim))),
        _seccao_categoria(pagina, mapa, esconder),
        _seccao_dias(pagina, db, inicio, fim, esconder),
        _seccao_comerciantes(db, inicio, fim, total, com_valores),
    ]
    if semanas:
        partes.append(_seccao_semanas(db, inicio, com_valores))
    partes.append(_seccao_variacao(db, inicio, fim, com_valores))
    if not semanas:
        partes.extend(
            [
                _seccao_comparacao(db, fim, esconder),
                _seccao_poupanca(db, inicio, fim),
                _seccao_subscricoes(db, fim, com_valores),
                _seccao_rentabilidade(db, fim, com_valores),
                _seccao_alocacao(pagina, db, esconder),
                _seccao_evolucao(pagina, db, fim, esconder),
            ]
        )
    return "".join(partes)


def _seccao_categoria(pagina: _Pagina, mapa, esconder: bool) -> str:
    miolos = [_p(aviso) for aviso in mapa.avisos]
    fig = _donut(dict(mapa), esconder)
    if fig is None:
        miolos.append(_p("Sem gastos neste período."))
    else:
        miolos.append(pagina.grafico(fig))
    return _seccao("Gastos por categoria", "".join(miolos))


def _seccao_dias(pagina: _Pagina, db, inicio: date, fim: date, esconder: bool) -> str:
    dias = gastos_por_dia_semana(db, inicio, fim)
    fig = _colunas(
        [nome for nome, _valor in dias],
        [valor for _nome, valor in dias],
        esconder,
        "Gastos",
    )
    return _seccao("Por dia", pagina.grafico(fig))


def _seccao_comerciantes(db, inicio: date, fim: date, total: Decimal, com_valores: bool) -> str:
    comerciantes = top_comerciantes(db, inicio, fim)
    if not comerciantes:
        return _seccao("Top comerciantes", _p("Sem comerciantes neste período."))
    linhas = []
    for nome, valor in comerciantes:
        peso = None if total == 0 else valor / total
        percentagem = formatar_percentagem(peso)
        if com_valores:
            linhas.append([nome, formatar_eur(valor), percentagem])
        else:
            linhas.append([nome, percentagem])
    colunas = ["Comerciante", "Valor", "Peso"] if com_valores else ["Comerciante", "Peso"]
    return _seccao("Top comerciantes", _tabela(colunas, linhas))


def _seccao_semanas(db, inicio: date, com_valores: bool) -> str:
    linhas_base = _medias_4_semanas(db, inicio)
    if not linhas_base:
        return _seccao("Vs média das 4 semanas anteriores", _p("Sem gastos para comparar."))
    linhas = []
    for categoria, este, media in linhas_base:
        variacao = _variacao(este, media)
        if com_valores:
            linhas.append([categoria, formatar_eur(este), formatar_eur(media), variacao])
        else:
            linhas.append([categoria, variacao])
    if com_valores:
        colunas = ["Categoria", "Esta semana", "Média das 4 semanas", "Variação"]
    else:
        colunas = ["Categoria", "Variação"]
    return _seccao("Vs média das 4 semanas anteriores", _tabela(colunas, linhas))


def _seccao_variacao(db, inicio: date, fim: date, com_valores: bool) -> str:
    serie = patrimonio_serie(db)
    dentro = [ponto for ponto in serie if inicio <= ponto[0] <= fim]
    anteriores = [ponto for ponto in serie if ponto[0] < inicio]
    if not dentro or not anteriores:
        return _seccao("Variação do património", _p("Sem snapshots neste período."))
    base = anteriores[-1][2]
    total = dentro[-1][2]
    return _seccao("Variação do património", _p(_frase_variacao(total - base, base, com_valores)))


def _seccao_comparacao(db, fim: date, esconder: bool) -> str:
    vistas = apresentar_comparacao(comparacao_mes(db, fim), esconder)
    if not vistas:
        return _seccao("Comparação com o mês anterior", _p("Sem categorias para comparar."))
    if esconder:
        colunas = ["Categoria", "Vs mês anterior", "Vs média 3 meses"]
        linhas = [[vista.categoria, vista.vs_anterior, vista.vs_media] for vista in vistas]
    else:
        colunas = [
            "Categoria",
            "Este mês",
            "Mês anterior",
            "Média 3 meses",
            "Vs mês anterior",
            "Vs média 3 meses",
        ]
        linhas = [
            [
                vista.categoria,
                vista.este_mes,
                vista.mes_anterior,
                vista.media_3_meses,
                vista.vs_anterior,
                vista.vs_media,
            ]
            for vista in vistas
        ]
    return _seccao("Comparação com o mês anterior", _tabela(colunas, linhas))


def _seccao_poupanca(db, inicio: date, fim: date) -> str:
    taxa = taxa_poupanca(db, inicio, fim)
    if taxa is None:
        texto = "Sem receitas neste período."
    else:
        texto = f"A taxa de poupança foi de {formatar_percentagem(taxa)}."
    return _seccao("Taxa de poupança", _p(texto))


def _seccao_subscricoes(db, fim: date, com_valores: bool) -> str:
    itens = subscricoes(db, fim)
    if not itens:
        return _seccao("Subscrições", _p("Nenhuma subscrição detetada."))
    total = sum((item.custo_anual for item in itens), start=Decimal(0))
    linhas = []
    for item in itens:
        peso = None if total == 0 else item.custo_anual / total
        percentagem = formatar_percentagem(peso)
        if com_valores:
            linhas.append([item.nome, item.periodicidade, formatar_eur(item.custo_anual), percentagem])
        else:
            linhas.append([item.nome, item.periodicidade, percentagem])
    colunas = (
        ["Nome", "Periodicidade", "Custo anual", "Peso"]
        if com_valores
        else ["Nome", "Periodicidade", "Peso"]
    )
    return _seccao("Subscrições", _tabela(colunas, linhas))


def _seccao_rentabilidade(db, fim: date, com_valores: bool) -> str:
    fx = _fx_de_saldos(db)
    linhas = []
    for linha in rentabilidade(db, ate=fim):
        fonte = None if linha.fonte == "total" else linha.fonte
        nome = "Total" if fonte is None else display_name(linha.fonte)
        referencia = benchmark(db, source=fonte, symbol="IWDA.UK", fx=fx, ate=fim)
        veredicto = veredicto_benchmark(linha.xirr, referencia.xirr)
        ganho = formatar_percentagem(linha.ganho_pct)
        xirr = formatar_percentagem(linha.xirr)
        indice = formatar_percentagem(referencia.xirr)
        if com_valores:
            linhas.append(
                [
                    nome,
                    formatar_eur(linha.depositado_liquido),
                    formatar_eur(linha.valor_atual),
                    ganho,
                    xirr,
                    indice,
                    veredicto,
                ]
            )
        else:
            linhas.append([nome, ganho, xirr, indice, veredicto])
    if com_valores:
        colunas = ["Plataforma", "Depositado", "Valor", "Ganho", "XIRR", "MSCI World", "Comparação"]
    else:
        colunas = ["Plataforma", "Ganho", "XIRR", "MSCI World", "Comparação"]
    intro = _p("Contra o MSCI World (IWDA.UK).")
    corpo = intro + (_tabela(colunas, linhas) if linhas else _p("Sem plataformas para comparar."))
    return _seccao("Rentabilidade", corpo)


def _seccao_alocacao(pagina: _Pagina, db, esconder: bool) -> str:
    totais = alocacao(db)
    partes = fatias(totais, esconder)
    if not partes:
        return _seccao("Alocação", _p("Sem alocação neste momento."))
    linhas = [[parte.nome, parte.texto] for parte in partes]
    colunas = ["Classe", "Peso"] if esconder else ["Classe", "Valor"]
    fig = _donut(totais, esconder)
    corpo = _p("Último snapshot de cada fonte.") + _tabela(colunas, linhas)
    if fig is not None:
        corpo += pagina.grafico(fig)
    return _seccao("Alocação", corpo)


def _seccao_evolucao(pagina: _Pagina, db, fim: date, esconder: bool) -> str:
    fig = _evolucao(patrimonio_serie(db), fim, esconder)
    if fig is None:
        corpo = _p("Sem snapshots neste período.")
    else:
        corpo = pagina.grafico(fig)
    return _seccao("Evolução do património", corpo)


def _medias_4_semanas(db, inicio: date) -> list[tuple[str, Decimal, Decimal]]:
    fim = inicio + timedelta(days=6)
    atual = dict(gastos_por_categoria(db, inicio, fim))
    anteriores = []
    for deslocamento in range(1, 5):
        ini = inicio - timedelta(days=7 * deslocamento)
        anteriores.append(dict(gastos_por_categoria(db, ini, ini + timedelta(days=6))))
    categorias = set(atual)
    for mapa in anteriores:
        categorias.update(mapa)
    linhas = []
    for categoria in categorias:
        este = atual.get(categoria, Decimal(0))
        media = sum((mapa.get(categoria, Decimal(0)) for mapa in anteriores), start=Decimal(0)) / Decimal(4)
        if este == 0 and media == 0:
            continue
        linhas.append((categoria, este, media))
    linhas.sort(key=lambda item: (-item[1], item[0]))
    return linhas


def _variacao(atual: Decimal, base: Decimal) -> str:
    if base == 0:
        return "—"
    return formatar_percentagem((atual - base) / base)


def _frase_variacao(delta: Decimal, base: Decimal, com_valores: bool) -> str:
    percentagem = formatar_percentagem(variacao_relativa(delta, base))
    if not com_valores:
        if percentagem == "—":
            return "Sem base para calcular a variação."
        return f"A variação no período foi de {percentagem}."
    if percentagem == "—":
        return f"A variação no período foi de {formatar_eur(delta)}."
    return f"A variação no período foi de {formatar_eur(delta)} ({percentagem})."


def _fx_de_saldos(db):
    """EUR por unidade de moeda, a partir do saldo mais recente. Sem rede."""
    taxas: dict[str, Decimal] = {}
    for currency, amount, amount_eur in db.execute(
        """SELECT b.currency, b.amount, b.amount_eur
           FROM balances b JOIN runs r ON r.id = b.run_id
           ORDER BY r.at, r.id"""
    ):
        codigo = (currency or "").upper()
        if codigo in {"", "EUR"}:
            continue
        montante = Decimal(str(amount))
        if montante == 0:
            continue
        taxas[codigo] = Decimal(str(amount_eur)) / montante

    def fx(_dia, moeda: str) -> Decimal:
        codigo = (moeda or "EUR").upper()
        if codigo == "EUR":
            return Decimal(1)
        return taxas.get(codigo, Decimal(0))

    return fx


def _donut(totais: dict, esconder: bool) -> go.Figure | None:
    partes = fatias(totais, esconder)
    if not partes:
        return None
    fig = go.Figure(
        go.Pie(
            labels=[parte.nome for parte in partes],
            values=[float(parte.valor) for parte in partes],
            hole=0.55,
            marker=dict(colors=[parte.cor for parte in partes]),
            sort=False,
            textinfo="percent",
            hovertext=[parte.texto for parte in partes],
            hoverinfo="label+text",
        )
    )
    return _aplicar(fig, None)


def _colunas(rotulos, valores, esconder: bool, nome: str) -> go.Figure:
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
    fig.update_xaxes(type="category")
    return _aplicar(fig, eixo)


def _evolucao(serie, fim: date, esconder: bool) -> go.Figure | None:
    pontos = [ponto for ponto in serie if ponto[0] <= fim]
    if not pontos:
        return None
    convertidos, rotulos = mascara_serie([ponto[2] for ponto in pontos], esconder)
    fig = go.Figure(
        go.Scatter(
            name="Património",
            x=[ponto[0] for ponto in pontos],
            y=[float(valor) for valor in convertidos],
            mode="lines+markers",
            line=dict(color=PALETA[0], width=2),
            hovertext=rotulos,
            hoverinfo="text",
        )
    )
    fig.update_xaxes(tickformat="%d/%m/%Y")
    return _aplicar(fig, "Índice (base 100)" if esconder else "EUR")


def _aplicar(fig: go.Figure, eixo_y: str | None) -> go.Figure:
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#7a756c"),
        separators=", ",
        margin=dict(l=48, r=12, t=24, b=56),
        legend=dict(orientation="h", yanchor="top", y=-0.25),
        autosize=True,
    )
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(
        showgrid=True,
        gridcolor="rgba(128,128,128,0.25)",
        zeroline=False,
        title=eixo_y,
    )
    return fig


def _intervalo_semana(token: str) -> tuple[date, date]:
    ano = int(token[:4])
    semana = int(token[6:])
    return date.fromisocalendar(ano, semana, 1), date.fromisocalendar(ano, semana, 7)


def _intervalo_mes(token: str) -> tuple[date, date]:
    ano = int(token[:4])
    mes = int(token[5:7])
    inicio = date(ano, mes, 1)
    if mes == 12:
        return inicio, date(ano, 12, 31)
    return inicio, date(ano, mes + 1, 1) - timedelta(days=1)


def _intervalo_legivel(inicio: date, fim: date) -> str:
    if inicio.year == fim.year and inicio.month == fim.month:
        return f"{inicio.day} a {fim.day} de {_MESES[inicio.month - 1]} de {inicio.year}"
    if inicio.year == fim.year:
        return (
            f"{inicio.day} de {_MESES[inicio.month - 1]} a "
            f"{fim.day} de {_MESES[fim.month - 1]} de {inicio.year}"
        )
    return (
        f"{inicio.day} de {_MESES[inicio.month - 1]} de {inicio.year} a "
        f"{fim.day} de {_MESES[fim.month - 1]} de {fim.year}"
    )


def _documento(titulo: str, periodo: str, corpo: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="pt">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(titulo)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"<header><h1>{html.escape(titulo)}</h1>"
        f'<p class="periodo">{html.escape(periodo)}</p></header>\n'
        f"{corpo}\n"
        "</body>\n"
        "</html>\n"
    )


def _seccao(titulo: str, corpo: str) -> str:
    return f"<section><h2>{html.escape(titulo)}</h2>{corpo}</section>\n"


def _p(texto: str) -> str:
    return f"<p>{html.escape(texto)}</p>"


def _lista(frases: list[str]) -> str:
    if not frases:
        return _p("Sem estatísticas neste período.")
    itens = "".join(f"<li>{html.escape(frase)}</li>" for frase in frases)
    return f"<ul>{itens}</ul>"


def _tabela(colunas: list[str], linhas: list[list[str]]) -> str:
    cabeca = "".join(f"<th>{html.escape(coluna)}</th>" for coluna in colunas)
    corpo = []
    for linha in linhas:
        celulas = "".join(f"<td>{html.escape(celula)}</td>" for celula in linha)
        corpo.append(f"<tr>{celulas}</tr>")
    return (
        '<div class="tabela"><table><thead><tr>'
        f"{cabeca}</tr></thead><tbody>{''.join(corpo)}</tbody></table></div>"
    )


def _gravar(pasta: Path, nome: str, documento: str) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / nome
    caminho.write_text(documento, encoding="utf-8")
    return caminho
