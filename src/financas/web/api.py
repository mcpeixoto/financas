"""Dados da página local. Cada função recebe a ligação e os parâmetros e devolve um dict."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import date, datetime
from decimal import Decimal

from financas import config
from financas.analise import (
    _como_data,
    _ultimo_run_por_fonte,
    alocacao,
    estatisticas_curiosas,
    fontes_bancarias,
    patrimonio_serie,
    preencher_patrimonio,
)
from financas.dashboard.dados import (
    COR_OUTROS,
    avisos_ultimo_run,
    cor,
    gravar_regra,
    listar_movimentos,
    montar_investimentos,
    preparar_gastos,
    resolver_periodo,
    variacao_patrimonio,
    variacao_relativa,
)
from financas.report import display_name, ordered_sources

_CATEGORIAS = (
    "Supermercado",
    "Restauração",
    "Transportes",
    "Casa",
    "Saúde",
    "Tecnologia e subscrições",
    "Lazer",
    "Viagens",
    "Compras",
    "Levantamentos",
    "Comissões e impostos",
    "Receita",
    "Reembolsos",
    "Investimento",
    "Transferência interna",
    "Outros",
)
_TIPOS = ("gasto", "receita", "interna", "investimento")

_trava = threading.Lock()
_atual = {
    "a_correr": False,
    "ultimo_codigo": None,
    "fim": None,
    "linhas": [],
}


def _reiniciar_atualizacao() -> None:
    with _trava:
        _atual["a_correr"] = False
        _atual["ultimo_codigo"] = None
        _atual["fim"] = None
        _atual["linhas"] = []


def _posicoes():
    import financas.posicoes as posicoes

    return posicoes


def _num(valor) -> float | None:
    if valor is None:
        return None
    return float(valor)


def _dia(valor) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    return str(valor)[:10]


def _cor(nome: str) -> str:
    texto = (nome or "").strip()
    if texto.casefold() in {"outros", "outras"}:
        return COR_OUTROS
    return cor(texto)


def _texto(params: dict, chave: str) -> str | None:
    valor = params.get(chave)
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _data(valor: str | None) -> date | None:
    if not valor:
        return None
    return date.fromisoformat(str(valor)[:10])


def _tabela(db, nome: str) -> bool:
    linha = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (nome,),
    ).fetchone()
    return linha is not None


def _configuracao() -> dict:
    caminho = config.DATA / "config.json"
    if not caminho.is_file():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dados if isinstance(dados, dict) else {}


def _veredicto(db) -> dict:
    try:
        frases = [str(frase) for frase in _posicoes().veredicto(db)]
    except Exception:
        return {"frases": [], "indisponivel": True}
    return {"frases": frases, "indisponivel": False}


_NOMES_CLASSE = {"acao": "Ação", "etf": "ETF", "cripto": "Cripto", "fx": "Câmbio", "outro": "Outro"}
_CEM = Decimal(100)


def _esconder(params: dict) -> bool:
    """Modo escondido: a resposta não leva montantes em euros, só percentagens e índices."""
    texto = str(params.get("esconder") or "").strip().casefold()
    return texto in {"1", "true", "sim", "on"}


def _dec(valor) -> Decimal:
    if valor is None or valor == "":
        return Decimal(0)
    if isinstance(valor, Decimal):
        return valor
    return Decimal(str(valor))


def _razao(parte, total) -> float | None:
    if parte is None or total is None:
        return None
    base = _dec(total)
    if base == 0:
        return None
    return float(_dec(parte) / base)


def _indice(valor, base: Decimal | None) -> float | None:
    """Valor em índice: 100 = ``base``."""
    if valor is None or base is None or base == 0:
        return None
    return float(_dec(valor) * _CEM / base)


def _base_ultimo_total(colunas: list[list]) -> Decimal | None:
    """Total do último ponto de séries empilhadas (em valor absoluto), ou None se for zero."""
    if not colunas:
        return None
    n = max(len(coluna) for coluna in colunas)
    if n == 0:
        return None
    total = sum((_dec(coluna[n - 1]) for coluna in colunas if len(coluna) == n), start=Decimal(0))
    total = abs(total)
    return total or None


def _base_maximo(*listas) -> Decimal | None:
    maior = Decimal(0)
    for lista in listas:
        for valor in lista:
            if valor is not None:
                maior = max(maior, abs(_dec(valor)))
    return maior or None


def nome_classe(classe: str | None) -> str:
    """Nome legível da classe de ativo."""
    chave = (classe or "").strip()
    return _NOMES_CLASSE.get(chave, chave.capitalize() if chave else "—")


def _nome_fonte_rotulo(fonte: str) -> str:
    try:
        return _posicoes().nome_fonte_rotulo(fonte)
    except Exception:
        return display_name(fonte)


def _rotulos(pares) -> list[str]:
    """Junta a fonte ao nome quando o mesmo nome aparece em mais de uma fonte."""
    lista = list(pares)
    fontes: dict[str, set[str]] = {}
    for fonte, nome in lista:
        fontes.setdefault(nome, set()).add(fonte)
    return [
        f"{nome} ({_nome_fonte_rotulo(fonte)})" if len(fontes.get(nome, ())) > 1 else nome
        for fonte, nome in lista
    ]


def _realizado_ano(db, fonte: str | None) -> list[dict]:
    try:
        pares = _posicoes().realizado_por_ano(db, fonte)
    except Exception:
        return []
    return [{"ano": int(ano), "valor": float(valor)} for ano, valor in pares]


def _etiqueta(texto: str) -> str:
    if texto.startswith("Bateu"):
        return "bateu"
    if texto.startswith("Ficou atrás"):
        return "ficou_atras"
    if texto.startswith("Empatou"):
        return "empatou"
    return "sem_dados"


def _historico(hist, esconder: bool = False) -> dict:
    datas = []
    nomes: list[str] = []
    vistos: set[str] = set()
    pontos = list(getattr(hist, "pontos", []) or [])
    for dia, mapa in pontos:
        datas.append(_dia(dia))
        for nome in mapa:
            if nome not in vistos:
                vistos.add(nome)
                nomes.append(nome)
    colunas = {nome: [_dec(mapa.get(nome, 0)) for _dia_ponto, mapa in pontos] for nome in nomes}
    base = _base_ultimo_total(list(colunas.values())) if esconder else None
    series = []
    for nome in nomes:
        if esconder:
            valores = [_indice(valor, base) if base else 0.0 for valor in colunas[nome]]
        else:
            valores = [float(valor) for valor in colunas[nome]]
        series.append({"nome": nome, "cor": _cor(nome), "valores": valores})
    return {
        "datas": datas,
        "series": series,
        "unidade": "indice" if esconder else "eur",
        "sem_preco": [str(item) for item in (getattr(hist, "sem_preco", []) or [])],
    }


def _par(valor, esconder: bool = False) -> dict | None:
    if valor is None:
        return None
    nome, montante = valor
    return {"nome": str(nome), "valor": None if esconder else float(montante)}


def _posicoes_vazio() -> dict:
    vazio = {"datas": [], "series": [], "sem_preco": []}
    return {
        "indisponivel": True,
        "atuais": [],
        "cartoes": None,
        "ganho_por_posicao": [],
        "realizado_por_simbolo": [],
        "realizado_por_ano": [],
        "estatisticas": None,
        "historico_custo": dict(vazio),
        "historico_valor": {"datas": [], "series": [], "sem_preco": []},
    }


def _ganho(item) -> Decimal | None:
    if item.latente_eur is None:
        return None
    return _dec(item.latente_eur) + _dec(item.realizado_eur) + _dec(item.dividendos_eur)


def _posicao_atual(item, rotulo: str | None = None, esconder: bool = False) -> dict:
    ganho = _ganho(item)
    custo = item.custo_eur
    linha = {
        "fonte": item.fonte,
        "nome_fonte": display_name(item.fonte),
        "cor": _cor(display_name(item.fonte)),
        "simbolo": item.simbolo,
        "nome": item.nome,
        "rotulo": rotulo or item.nome,
        "classe": item.classe,
        "classe_nome": nome_classe(item.classe),
        "quantidade": float(item.quantidade),
        "valor_eur": _num(item.valor_eur),
        "custo_eur": _num(custo),
        "latente_eur": _num(item.latente_eur),
        "latente_pct": _num(item.latente_pct),
        "peso": float(item.peso),
        "realizado_eur": float(item.realizado_eur),
        "dividendos_eur": float(item.dividendos_eur),
        "realizado_pct": _razao(item.realizado_eur, item.valor_eur),
        "dividendos_pct": _razao(item.dividendos_eur, item.valor_eur),
        "ganho_eur": _num(ganho),
        "ganho_pct": _razao(ganho, custo) if ganho is not None else None,
    }
    if esconder:
        for chave in (
            "quantidade",
            "valor_eur",
            "custo_eur",
            "latente_eur",
            "realizado_eur",
            "dividendos_eur",
            "ganho_eur",
        ):
            linha[chave] = None
    return linha


def _cartoes_posicoes(itens) -> dict:
    pesos = sorted((_dec(item.peso) for item in itens), reverse=True)
    custo = Decimal(0)
    latente = Decimal(0)
    for item in itens:
        if item.custo_eur is None or item.latente_eur is None:
            continue
        custo += _dec(item.custo_eur)
        latente += _dec(item.latente_eur)
    return {
        "abertas": len(itens),
        "peso_top3": float(sum(pesos[:3], start=Decimal(0))) if pesos else None,
        "latente_pct": _razao(latente, custo) if custo else None,
    }


def _estatisticas(item, esconder: bool = False) -> dict:
    return {
        "fechados": int(item.fechados),
        "taxa_acerto": _num(item.taxa_acerto),
        "ganho_medio_eur": None if esconder else _num(item.ganho_medio_eur),
        "perda_media_eur": None if esconder else _num(item.perda_media_eur),
        "melhor": _par(item.melhor, esconder),
        "pior": _par(item.pior, esconder),
        "fator_lucro": _num(item.fator_lucro),
        "realizado_total_eur": None if esconder else float(item.realizado_total_eur),
    }


def _ultimo_por_fonte(db) -> tuple[dict[str, tuple[Decimal, date]], date | None]:
    """Valor do snapshot mais recente de cada fonte e o dia da última corrida."""
    ultimos = _ultimo_run_por_fonte(db)
    dias = {run_id: _como_data(at) for run_id, at in db.execute("SELECT id, at FROM runs")}
    valores: dict[str, tuple[Decimal, date]] = {}
    for fonte, run_id in ultimos.items():
        total = Decimal(0)
        for (montante,) in db.execute(
            "SELECT amount_eur FROM balances WHERE run_id = ? AND source = ?", (run_id, fonte)
        ):
            total += _dec(montante)
        for (montante,) in db.execute(
            "SELECT value_eur FROM positions WHERE run_id = ? AND source = ?", (run_id, fonte)
        ):
            total += _dec(montante)
        valores[fonte] = (total, dias.get(run_id))
    ultima = max(dias.values()) if dias else None
    return valores, ultima


def resumo(db, params: dict) -> dict:
    esconder = _esconder(params)
    serie = patrimonio_serie(db)
    cheia, preenchida = preencher_patrimonio(serie)
    variacao = variacao_patrimonio(serie)
    ultimos, ultima_corrida = _ultimo_por_fonte(db)
    total = sum((valor for valor, _dia_fonte in ultimos.values()), start=Decimal(0))
    fontes = []
    desatualizadas = []
    for fonte in ordered_sources(ultimos):
        valor, dia_fonte = ultimos[fonte]
        atrasada = dia_fonte is not None and ultima_corrida is not None and dia_fonte < ultima_corrida
        if atrasada:
            desatualizadas.append(fonte)
        fontes.append(
            {
                "fonte": fonte,
                "nome": display_name(fonte),
                "valor": None if esconder else float(valor),
                "peso": _razao(valor, total),
                "cor": _cor(display_name(fonte)),
                "desatualizada_desde": _dia(dia_fonte) if atrasada else None,
            }
        )
    nomes = ordered_sources(fonte for _dia, por_fonte, _total in cheia for fonte in por_fonte)
    colunas = {fonte: [_dec(por_fonte.get(fonte, 0)) for _dia, por_fonte, _total in cheia] for fonte in nomes}
    base = _base_ultimo_total(list(colunas.values())) if esconder else None
    series = []
    for fonte in nomes:
        if esconder:
            valores = [_indice(valor, base) if base else 0.0 for valor in colunas[fonte]]
        else:
            valores = [float(valor) for valor in colunas[fonte]]
        series.append(
            {
                "fonte": fonte,
                "nome": display_name(fonte),
                "cor": _cor(display_name(fonte)),
                "valores": valores,
            }
        )
    por_classe = {nome: _dec(valor) for nome, valor in alocacao(db).items() if valor != 0}
    resto = total - sum(por_classe.values(), start=Decimal(0))
    if resto > Decimal("0.005"):
        por_classe["Outros"] = por_classe.get("Outros", Decimal(0)) + resto
    soma_classes = sum(por_classe.values(), start=Decimal(0))
    classes = [
        {
            "nome": nome,
            "valor": None if esconder else float(valor),
            "peso": _razao(valor, soma_classes),
            "cor": _cor(nome),
        }
        for nome, valor in por_classe.items()
    ]
    avisos = []
    ok = 0
    for fonte, estado, texto in avisos_ultimo_run(db):
        if estado == "ok":
            ok += 1
        avisos.append(
            {
                "fonte": fonte,
                "nome": display_name(fonte),
                "estado": estado,
                "texto": texto,
            }
        )
    primeiro = next((linha_total for _dia, _por_fonte, linha_total in cheia if linha_total != 0), None)
    ultimo_total = cheia[-1][2] if cheia else None
    return {
        "esconder": esconder,
        "patrimonio": None if esconder else float(total),
        "patrimonio_indice": _indice(ultimo_total, abs(primeiro) if primeiro else None),
        "dia": _dia(variacao.dia),
        "fontes": fontes,
        "desatualizadas": desatualizadas,
        "n_fontes_ok": ok,
        "variacao": {
            "anterior": None if esconder else _num(variacao.anterior),
            "delta_anterior": None if esconder else _num(variacao.delta_anterior),
            "delta_anterior_pct": _num(variacao_relativa(variacao.delta_anterior, variacao.anterior)),
            "dia_anterior": _dia(variacao.dia_anterior),
            "ha_30_dias": None if esconder else _num(variacao.ha_30_dias),
            "delta_30": None if esconder else _num(variacao.delta_30),
            "delta_30_pct": _num(variacao_relativa(variacao.delta_30, variacao.ha_30_dias)),
            "dia_30": _dia(variacao.dia_30),
        },
        "serie": {
            "datas": [_dia(dia) for dia, _por_fonte, _total in cheia],
            "preenchida": bool(preenchida),
            "unidade": "indice" if esconder else "eur",
            "series": series,
        },
        "alocacao": classes,
        "avisos": avisos,
        "veredicto": _veredicto(db),
    }


def _mensal(db, fonte: str | None, esconder: bool) -> dict:
    """Série mensal de depositado, valor e MSCI World de uma plataforma (ou do total)."""
    try:
        pontos = list(_posicoes().historico_plataforma(db, fonte))
    except Exception:
        pontos = []
    datas = [_dia(ponto.dia) for ponto in pontos]
    depositado = [_dec(ponto.depositado) for ponto in pontos]
    valor = [_dec(ponto.valor) for ponto in pontos]
    indice = [None if ponto.indice is None else _dec(ponto.indice) for ponto in pontos]
    ganho = [v - d for v, d in zip(valor, depositado, strict=True)]
    if not esconder:
        return {
            "datas": datas,
            "unidade": "eur",
            "depositado": [float(item) for item in depositado],
            "valor": [float(item) for item in valor],
            "indice": [None if item is None else float(item) for item in indice],
            "ganho": [float(item) for item in ganho],
        }
    base = abs(depositado[-1]) if depositado and depositado[-1] != 0 else None
    if base is None and valor:
        base = abs(valor[-1]) or None
    return {
        "datas": datas,
        "unidade": "indice",
        "depositado": [_indice(item, base) for item in depositado],
        "valor": [_indice(item, base) for item in valor],
        "indice": [_indice(item, base) for item in indice],
        "ganho": [_indice(item, base) for item in ganho],
    }


def _anos_escondidos(rendimentos: list[dict], realizado: list[dict]) -> tuple[list[dict], list[dict]]:
    base = _base_maximo([item["valor"] for item in rendimentos], [item["valor"] for item in realizado])
    return (
        [{"ano": item["ano"], "valor": None, "indice": _indice(item["valor"], base)} for item in rendimentos],
        [{"ano": item["ano"], "valor": None, "indice": _indice(item["valor"], base)} for item in realizado],
    )


def investimentos(db, params: dict) -> dict:
    esconder = _esconder(params)
    linhas = []
    for bloco in montar_investimentos(db):
        linha = bloco["linha"]
        referencia = bloco["referencia"]
        fonte = bloco["fonte"]
        nome = bloco["nome"]
        rendimentos = [{"ano": int(ano), "valor": float(valor)} for ano, valor in bloco["rendimentos"]]
        realizado = _realizado_ano(db, fonte)
        if esconder:
            rendimentos, realizado = _anos_escondidos(rendimentos, realizado)
        linhas.append(
            {
                "nome": nome,
                "fonte": fonte,
                "cor": _cor(nome),
                "depositado": None if esconder else float(linha.depositado_liquido),
                "valor": None if esconder else float(linha.valor_atual),
                "ganho": None if esconder else float(linha.ganho_absoluto),
                "ganho_pct": _num(linha.ganho_pct),
                "xirr": _num(linha.xirr),
                "xirr_indice": _num(referencia.xirr),
                "veredicto": bloco["veredicto"],
                "etiqueta": _etiqueta(bloco["veredicto"]),
                "nota": linha.nota or "",
                "curva": [
                    {
                        "dia": _dia(dia),
                        "capital": None if esconder else float(capital),
                        "valor": None if esconder else float(valor),
                    }
                    for dia, capital, valor in bloco["curva"]
                ],
                "mensal": _mensal(db, fonte, esconder),
                "rendimentos": rendimentos,
                "realizado": realizado,
            }
        )
    try:
        sem = [display_name(fonte) for fonte in _posicoes().plataformas_sem_depositos(db)]
    except Exception:
        sem = []
    return {"esconder": esconder, "linhas": linhas, "sem_depositos": sem}


def posicoes(db, params: dict) -> dict:
    esconder = _esconder(params)
    try:
        modulo = _posicoes()
        itens = list(modulo.posicoes_atuais(db))
        rotulos = _rotulos((item.fonte, item.nome or item.simbolo) for item in itens)
        atuais = [
            _posicao_atual(item, rotulo, esconder) for item, rotulo in zip(itens, rotulos, strict=True)
        ]
        vendidos = list(modulo.realizado_por_simbolo(db))
        rotulos_vendas = _rotulos((item.fonte, item.nome or item.simbolo) for item in vendidos)
        simbolos = [
            {
                "fonte": item.fonte,
                "nome_fonte": display_name(item.fonte),
                "simbolo": item.simbolo,
                "nome": item.nome,
                "rotulo": rotulo,
                "vendas": int(item.vendas),
                "ganhos": int(item.ganhos),
                "realizado_eur": None if esconder else float(item.realizado_eur),
                "ainda_aberta": bool(item.ainda_aberta),
            }
            for item, rotulo in zip(vendidos, rotulos_vendas, strict=True)
        ]
        anos = [{"ano": int(ano), "valor": float(valor)} for ano, valor in modulo.realizado_por_ano(db)]
        if esconder:
            anos, _nada = _anos_escondidos(anos, [])
        estatisticas = _estatisticas(modulo.estatisticas_negocios(db), esconder)
        custo = _historico(modulo.historico_custo(db), esconder)
        valor = _historico(modulo.historico_valor(db), esconder)
    except Exception:
        return _posicoes_vazio()
    ganhos = []
    for item, linha in zip(itens, atuais, strict=True):
        ganho = _ganho(item)
        if ganho is None:
            continue
        pct = linha["ganho_pct"]
        if esconder and pct is None:
            continue
        ganhos.append(
            {
                "rotulo": linha["rotulo"],
                "fonte": item.fonte,
                "simbolo": item.simbolo,
                "ganho_eur": None if esconder else float(ganho),
                "ganho_pct": pct,
            }
        )
    chave = "ganho_pct" if esconder else "ganho_eur"
    ganhos.sort(key=lambda linha: (linha[chave] if linha[chave] is not None else 0.0, linha["rotulo"]))
    return {
        "esconder": esconder,
        "indisponivel": False,
        "atuais": atuais,
        "cartoes": _cartoes_posicoes(itens),
        "ganho_por_posicao": ganhos,
        "realizado_por_simbolo": simbolos,
        "realizado_por_ano": anos,
        "estatisticas": estatisticas,
        "historico_custo": custo,
        "historico_valor": valor,
    }


def _receitas_gastos_decimal(db, inicio: date, fim: date) -> list[tuple[str, Decimal, Decimal]]:
    from financas.analise import _movimentos

    movimentos, _avisos = _movimentos(db, None, inicio, fim)
    acumulado: dict[str, dict[str, Decimal]] = {}
    cursor = date(inicio.year, inicio.month, 1)
    while cursor <= fim:
        chave = f"{cursor.year:04d}-{cursor.month:02d}"
        acumulado[chave] = {"receitas": Decimal(0), "gastos": Decimal(0)}
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    for movimento in movimentos:
        if movimento.booked < inicio or movimento.booked > fim:
            continue
        chave = f"{movimento.booked.year:04d}-{movimento.booked.month:02d}"
        slot = acumulado.get(chave)
        if slot is None:
            continue
        if movimento.tipo == "receita":
            slot["receitas"] += abs(movimento.eur)
        elif movimento.tipo == "gasto":
            slot["gastos"] += abs(movimento.eur)
    return [(chave, valores["receitas"], valores["gastos"]) for chave, valores in acumulado.items()]


def _receitas_gastos(db, inicio: date, fim: date, esconder: bool = False) -> list[dict]:
    meses = _receitas_gastos_decimal(db, inicio, fim)
    if not esconder:
        return [
            {"periodo": chave, "receitas": float(receitas), "gastos": float(gastos)}
            for chave, receitas, gastos in meses
        ]
    base = _base_maximo([receitas for _c, receitas, _g in meses], [gastos for _c, _r, gastos in meses])
    return [
        {
            "periodo": chave,
            "receitas": None,
            "gastos": None,
            "indice_receitas": _indice(receitas, base),
            "indice_gastos": _indice(gastos, base),
        }
        for chave, receitas, gastos in meses
    ]


def gastos(db, params: dict) -> dict:
    esconder = _esconder(params)
    periodo = _texto(params, "periodo") or "este mês"
    freq = _texto(params, "freq") or "mes"
    inicio, fim = resolver_periodo(periodo, date.today(), _data(_texto(params, "inicio")), _data(_texto(params, "fim")))
    painel = preparar_gastos(db, inicio, fim, freq)
    total_categorias = sum(
        (_dec(valor) for valor in painel.por_categoria.values() if valor != 0), start=Decimal(0)
    )
    categorias = [
        {
            "nome": nome,
            "valor": None if esconder else float(valor),
            "peso": _razao(valor, total_categorias),
            "cor": _cor(nome),
            "_ordem": float(valor),
        }
        for nome, valor in painel.por_categoria.items()
        if valor != 0
    ]
    categorias.sort(key=lambda item: (item["nome"].casefold() == "outros", -item["_ordem"], item["nome"]))
    for item in categorias:
        del item["_ordem"]
    totais_periodo = {
        chave: sum((_dec(mapa.get(chave, 0)) for mapa in painel.empilhado.values()), start=Decimal(0))
        for chave in painel.periodos
    }
    empilhado = []
    for nome, mapa in painel.empilhado.items():
        brutos = [_dec(mapa.get(periodo_chave, 0)) for periodo_chave in painel.periodos]
        empilhado.append(
            {
                "nome": nome,
                "cor": _cor(nome),
                "valores": None if esconder else [float(valor) for valor in brutos],
                "pesos": [
                    _razao(valor, totais_periodo[chave]) or 0.0
                    for valor, chave in zip(brutos, painel.periodos, strict=True)
                ],
                "_ordem": float(sum(brutos, start=Decimal(0))),
            }
        )
    empilhado.sort(key=lambda item: (item["nome"].casefold() == "outros", -item["_ordem"], item["nome"]))
    for item in empilhado:
        del item["_ordem"]
    comparacao = []
    for linha in painel.comparacao:
        comparacao.append(
            {
                "categoria": linha.categoria,
                "cor": _cor(linha.categoria),
                "este_mes": None if esconder else float(linha.este_mes),
                "mes_anterior": None if esconder else float(linha.mes_anterior),
                "media_3_meses": None if esconder else float(linha.media_3_meses),
                "vs_anterior": _num(linha.vs_anterior),
                "vs_media": _num(linha.vs_media),
            }
        )
    total_subscricoes = sum((_dec(item.custo_anual) for item in painel.subscricoes), start=Decimal(0))
    total_dias = sum((_dec(valor) for _nome, valor in painel.dias), start=Decimal(0))
    return {
        "esconder": esconder,
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "freq": freq,
        "categorias": categorias,
        "avisos": list(painel.avisos),
        "periodos": list(painel.periodos),
        "empilhado": empilhado,
        "comerciantes": [
            {
                "nome": nome,
                "valor": None if esconder else float(valor),
                "peso": _razao(valor, total_categorias),
            }
            for nome, valor in painel.comerciantes
        ],
        "subscricoes": [
            {
                "nome": item.nome,
                "periodicidade": item.periodicidade,
                "custo_anual": None if esconder else float(item.custo_anual),
                "peso": _razao(item.custo_anual, total_subscricoes),
            }
            for item in painel.subscricoes
        ],
        "dias": [
            {
                "nome": nome,
                "valor": None if esconder else float(valor),
                "peso": _razao(valor, total_dias) if total_dias else 0.0,
            }
            for nome, valor in painel.dias
        ],
        "taxa_poupanca": _num(painel.taxa),
        "receitas_gastos": _receitas_gastos(db, inicio, fim, esconder),
        "comparacao": comparacao,
        "frases": list(estatisticas_curiosas(db, inicio, fim)),
    }


def _sentido(valor) -> str:
    numero = _dec(valor)
    if numero < 0:
        return "saída"
    if numero > 0:
        return "entrada"
    return "zero"


def movimentos(db, params: dict) -> dict:
    esconder = _esconder(params)
    limite = int(_texto(params, "limite") or 500)
    if limite < 0:
        raise ValueError("limite inválido")
    escolha = _texto(params, "fonte")
    bancarias = list(fontes_bancarias())
    todas = escolha == "todas"
    linhas = listar_movimentos(
        db,
        fonte=None if todas or not escolha else escolha,
        categoria=_texto(params, "categoria"),
        tipo=_texto(params, "tipo"),
        texto=_texto(params, "texto"),
        inicio=_data(_texto(params, "inicio")),
        fim=_data(_texto(params, "fim")),
    )
    if not escolha:
        permitidas = set(bancarias)
        linhas = [linha for linha in linhas if linha.fonte in permitidas]
    linhas = list(reversed(linhas))
    fontes = []
    if _tabela(db, "transactions"):
        for (fonte,) in db.execute(
            "SELECT DISTINCT source FROM transactions WHERE source IS NOT NULL ORDER BY source"
        ):
            fontes.append({"fonte": fonte, "nome": display_name(fonte), "bancaria": fonte in bancarias})
    return {
        "esconder": esconder,
        "linhas": [
            {
                "data": linha.data.isoformat(),
                "fonte": linha.fonte,
                "nome_fonte": display_name(linha.fonte),
                "conta": linha.conta,
                "descricao": linha.descricao,
                "montante": None if esconder else float(linha.valor),
                "sentido": _sentido(linha.valor),
                "moeda": linha.moeda,
                "tipo": linha.tipo,
                "categoria": linha.categoria,
                "cor": _cor(linha.categoria),
            }
            for linha in linhas[:limite]
        ],
        "total": len(linhas),
        "limite": limite,
        "fonte": escolha or "",
        "fontes": fontes,
        "bancarias": bancarias,
        "categorias": list(_CATEGORIAS),
        "tipos": list(_TIPOS),
    }


def gravar_categoria(db, params: dict) -> dict:
    gravar_regra(
        config.DATA / "categorias.toml",
        str(params.get("descricao") or ""),
        str(params.get("categoria") or ""),
        str(params.get("tipo") or ""),
    )
    return {"ok": True}


def _consentimento(seccao: dict) -> tuple[str | None, int | None]:
    bruto = seccao.get("valid_until") if seccao else None
    if not bruto:
        return None, None
    try:
        interpretado = datetime.fromisoformat(str(bruto).replace("Z", "+00:00"))
    except ValueError:
        return None, None
    dia = interpretado.date()
    return dia.isoformat(), (dia - date.today()).days


def _ultimo_estado(db, fonte: str) -> tuple[str, str]:
    if not _tabela(db, "run_status"):
        return "sem corridas", ""
    linha = db.execute(
        "SELECT status, warnings FROM run_status WHERE source = ? ORDER BY run_id DESC LIMIT 1",
        (fonte,),
    ).fetchone()
    if linha is None:
        return "sem corridas", ""
    return linha[0] or "", linha[1] or ""


def _n_movimentos(db, fonte: str) -> int:
    if not _tabela(db, "transactions"):
        return 0
    linha = db.execute("SELECT COUNT(*) FROM transactions WHERE source = ?", (fonte,)).fetchone()
    return int(linha[0] if linha else 0)


def _ultima_corrida(db) -> str | None:
    if not _tabela(db, "runs"):
        return None
    linha = db.execute("SELECT MAX(at) FROM runs").fetchone()
    if linha is None or not linha[0]:
        return None
    return _dia(linha[0])


def estado(db, params: dict) -> dict:
    from financas.cli import CONNECTORS

    configuracao = _configuracao()
    fontes = []
    for nome in CONNECTORS:
        seccao = configuracao.get(nome) or {}
        if not isinstance(seccao, dict):
            seccao = {}
        situacao, avisos = _ultimo_estado(db, nome)
        consentimento, dias = _consentimento(seccao)
        fontes.append(
            {
                "fonte": nome,
                "nome": display_name(nome),
                "cor": _cor(display_name(nome)),
                "estado": situacao,
                "avisos": avisos,
                "consentimento": consentimento,
                "dias": dias,
                "movimentos": _n_movimentos(db, nome),
            }
        )
    return {"ultima_corrida": _ultima_corrida(db), "fontes": fontes}


def _estado_atualizacao() -> dict:
    return {
        "a_correr": bool(_atual["a_correr"]),
        "ultimo_codigo": _atual["ultimo_codigo"],
        "fim": _atual["fim"],
        "ultimas_linhas": list(_atual["linhas"][-30:]),
    }


def consultar_atualizacao(db, params: dict) -> dict:
    with _trava:
        return _estado_atualizacao()


def _acompanhar(proc) -> None:
    linhas: list[str] = []
    codigo = 1
    try:
        saida = getattr(proc, "stdout", None)
        if saida is not None:
            for linha in saida:
                linhas.append(str(linha).rstrip("\n"))
                with _trava:
                    _atual["linhas"] = linhas[-40:]
        codigo = int(proc.wait())
    except Exception as exc:
        linhas.append(str(exc))
        codigo = 1
    with _trava:
        _atual["a_correr"] = False
        _atual["ultimo_codigo"] = codigo
        _atual["fim"] = datetime.now().isoformat(timespec="seconds")
        _atual["linhas"] = linhas[-40:]


def iniciar_atualizacao(db, params: dict) -> dict:
    with _trava:
        if _atual["a_correr"]:
            return _estado_atualizacao()
        proc = subprocess.Popen(
            [sys.executable, "-m", "financas.cli", "run", "--no-notify"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _atual["a_correr"] = True
        _atual["linhas"] = []
        _atual["fim"] = None
    threading.Thread(target=_acompanhar, args=(proc,), daemon=True).start()
    return consultar_atualizacao(db, params)


GET = {
    "resumo": resumo,
    "investimentos": investimentos,
    "posicoes": posicoes,
    "gastos": gastos,
    "movimentos": movimentos,
    "estado": estado,
    "atualizar": consultar_atualizacao,
}

POST = {
    "categoria": gravar_categoria,
    "atualizar": iniciar_atualizacao,
}
