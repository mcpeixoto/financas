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
    alocacao,
    estatisticas_curiosas,
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


def _historico(hist) -> dict:
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
    series = []
    for nome in nomes:
        series.append(
            {
                "nome": nome,
                "cor": _cor(nome),
                "valores": [float(mapa.get(nome, 0)) for _dia_ponto, mapa in pontos],
            }
        )
    return {
        "datas": datas,
        "series": series,
        "sem_preco": [str(item) for item in (getattr(hist, "sem_preco", []) or [])],
    }


def _par(valor) -> dict | None:
    if valor is None:
        return None
    nome, montante = valor
    return {"nome": str(nome), "valor": float(montante)}


def _posicoes_vazio() -> dict:
    vazio = {"datas": [], "series": [], "sem_preco": []}
    return {
        "indisponivel": True,
        "atuais": [],
        "realizado_por_simbolo": [],
        "realizado_por_ano": [],
        "estatisticas": None,
        "historico_custo": dict(vazio),
        "historico_valor": {"datas": [], "series": [], "sem_preco": []},
    }


def _posicao_atual(item) -> dict:
    return {
        "fonte": item.fonte,
        "nome_fonte": display_name(item.fonte),
        "cor": _cor(display_name(item.fonte)),
        "simbolo": item.simbolo,
        "nome": item.nome,
        "classe": item.classe,
        "quantidade": float(item.quantidade),
        "valor_eur": _num(item.valor_eur),
        "custo_eur": _num(item.custo_eur),
        "latente_eur": _num(item.latente_eur),
        "latente_pct": _num(item.latente_pct),
        "peso": float(item.peso),
        "realizado_eur": float(item.realizado_eur),
        "dividendos_eur": float(item.dividendos_eur),
    }


def _estatisticas(item) -> dict:
    return {
        "fechados": int(item.fechados),
        "taxa_acerto": _num(item.taxa_acerto),
        "ganho_medio_eur": _num(item.ganho_medio_eur),
        "perda_media_eur": _num(item.perda_media_eur),
        "melhor": _par(item.melhor),
        "pior": _par(item.pior),
        "fator_lucro": _num(item.fator_lucro),
        "realizado_total_eur": float(item.realizado_total_eur),
    }


def resumo(db, params: dict) -> dict:
    serie = patrimonio_serie(db)
    cheia, preenchida = preencher_patrimonio(serie)
    variacao = variacao_patrimonio(serie)
    ultimo = serie[-1][1] if serie else {}
    fontes = []
    for fonte in ordered_sources(ultimo):
        fontes.append(
            {
                "fonte": fonte,
                "nome": display_name(fonte),
                "valor": float(ultimo[fonte]),
                "cor": _cor(display_name(fonte)),
            }
        )
    nomes = ordered_sources(fonte for _dia, por_fonte, _total in cheia for fonte in por_fonte)
    series = []
    for fonte in nomes:
        series.append(
            {
                "fonte": fonte,
                "nome": display_name(fonte),
                "cor": _cor(display_name(fonte)),
                "valores": [float(por_fonte.get(fonte, 0)) for _dia, por_fonte, _total in cheia],
            }
        )
    classes = []
    for nome, valor in alocacao(db).items():
        if valor == 0:
            continue
        classes.append({"nome": nome, "valor": float(valor), "cor": _cor(nome)})
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
    return {
        "patrimonio": float(variacao.total),
        "dia": _dia(variacao.dia),
        "fontes": fontes,
        "n_fontes_ok": ok,
        "variacao": {
            "anterior": _num(variacao.anterior),
            "delta_anterior": _num(variacao.delta_anterior),
            "delta_anterior_pct": _num(variacao_relativa(variacao.delta_anterior, variacao.anterior)),
            "dia_anterior": _dia(variacao.dia_anterior),
            "ha_30_dias": _num(variacao.ha_30_dias),
            "delta_30": _num(variacao.delta_30),
            "delta_30_pct": _num(variacao_relativa(variacao.delta_30, variacao.ha_30_dias)),
            "dia_30": _dia(variacao.dia_30),
        },
        "serie": {
            "datas": [_dia(dia) for dia, _por_fonte, _total in cheia],
            "preenchida": bool(preenchida),
            "series": series,
        },
        "alocacao": classes,
        "avisos": avisos,
        "veredicto": _veredicto(db),
    }


def investimentos(db, params: dict) -> dict:
    linhas = []
    for bloco in montar_investimentos(db):
        linha = bloco["linha"]
        referencia = bloco["referencia"]
        fonte = bloco["fonte"]
        nome = bloco["nome"]
        linhas.append(
            {
                "nome": nome,
                "fonte": fonte,
                "cor": _cor(nome),
                "depositado": float(linha.depositado_liquido),
                "valor": float(linha.valor_atual),
                "ganho": float(linha.ganho_absoluto),
                "ganho_pct": _num(linha.ganho_pct),
                "xirr": _num(linha.xirr),
                "xirr_indice": _num(referencia.xirr),
                "veredicto": bloco["veredicto"],
                "etiqueta": _etiqueta(bloco["veredicto"]),
                "nota": linha.nota or "",
                "curva": [
                    {"dia": _dia(dia), "capital": float(capital), "valor": float(valor)}
                    for dia, capital, valor in bloco["curva"]
                ],
                "rendimentos": [
                    {"ano": int(ano), "valor": float(valor)} for ano, valor in bloco["rendimentos"]
                ],
                "realizado": _realizado_ano(db, fonte),
            }
        )
    return {"linhas": linhas}


def posicoes(db, params: dict) -> dict:
    try:
        modulo = _posicoes()
        atuais = [_posicao_atual(item) for item in modulo.posicoes_atuais(db)]
        simbolos = [
            {
                "fonte": item.fonte,
                "nome_fonte": display_name(item.fonte),
                "simbolo": item.simbolo,
                "nome": item.nome,
                "vendas": int(item.vendas),
                "ganhos": int(item.ganhos),
                "realizado_eur": float(item.realizado_eur),
                "ainda_aberta": bool(item.ainda_aberta),
            }
            for item in modulo.realizado_por_simbolo(db)
        ]
        anos = [
            {"ano": int(ano), "valor": float(valor)} for ano, valor in modulo.realizado_por_ano(db)
        ]
        estatisticas = _estatisticas(modulo.estatisticas_negocios(db))
        custo = _historico(modulo.historico_custo(db))
        valor = _historico(modulo.historico_valor(db))
    except Exception:
        return _posicoes_vazio()
    return {
        "indisponivel": False,
        "atuais": atuais,
        "realizado_por_simbolo": simbolos,
        "realizado_por_ano": anos,
        "estatisticas": estatisticas,
        "historico_custo": custo,
        "historico_valor": valor,
    }


def _receitas_gastos(db, inicio: date, fim: date) -> list[dict]:
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
    return [
        {"periodo": chave, "receitas": float(valores["receitas"]), "gastos": float(valores["gastos"])}
        for chave, valores in acumulado.items()
    ]


def gastos(db, params: dict) -> dict:
    periodo = _texto(params, "periodo") or "este mês"
    freq = _texto(params, "freq") or "mes"
    inicio, fim = resolver_periodo(periodo, date.today(), _data(_texto(params, "inicio")), _data(_texto(params, "fim")))
    painel = preparar_gastos(db, inicio, fim, freq)
    categorias = [
        {"nome": nome, "valor": float(valor), "cor": _cor(nome)}
        for nome, valor in painel.por_categoria.items()
        if valor != 0
    ]
    categorias.sort(key=lambda item: (item["nome"].casefold() == "outros", -item["valor"], item["nome"]))
    empilhado = []
    for nome, mapa in painel.empilhado.items():
        empilhado.append(
            {
                "nome": nome,
                "cor": _cor(nome),
                "valores": [float(mapa.get(periodo_chave, 0)) for periodo_chave in painel.periodos],
            }
        )
    empilhado.sort(key=lambda item: (item["nome"].casefold() == "outros", -sum(item["valores"]), item["nome"]))
    comparacao = []
    for linha in painel.comparacao:
        comparacao.append(
            {
                "categoria": linha.categoria,
                "cor": _cor(linha.categoria),
                "este_mes": float(linha.este_mes),
                "mes_anterior": float(linha.mes_anterior),
                "media_3_meses": float(linha.media_3_meses),
                "vs_anterior": _num(linha.vs_anterior),
                "vs_media": _num(linha.vs_media),
            }
        )
    return {
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "freq": freq,
        "categorias": categorias,
        "avisos": list(painel.avisos),
        "periodos": list(painel.periodos),
        "empilhado": empilhado,
        "comerciantes": [{"nome": nome, "valor": float(valor)} for nome, valor in painel.comerciantes],
        "subscricoes": [
            {
                "nome": item.nome,
                "periodicidade": item.periodicidade,
                "custo_anual": float(item.custo_anual),
            }
            for item in painel.subscricoes
        ],
        "dias": [{"nome": nome, "valor": float(valor)} for nome, valor in painel.dias],
        "taxa_poupanca": _num(painel.taxa),
        "receitas_gastos": _receitas_gastos(db, inicio, fim),
        "comparacao": comparacao,
        "frases": list(estatisticas_curiosas(db, inicio, fim)),
    }


def movimentos(db, params: dict) -> dict:
    limite = int(_texto(params, "limite") or 500)
    if limite < 0:
        raise ValueError("limite inválido")
    linhas = listar_movimentos(
        db,
        fonte=_texto(params, "fonte"),
        categoria=_texto(params, "categoria"),
        tipo=_texto(params, "tipo"),
        texto=_texto(params, "texto"),
        inicio=_data(_texto(params, "inicio")),
        fim=_data(_texto(params, "fim")),
    )
    fontes = []
    if _tabela(db, "transactions"):
        for (fonte,) in db.execute(
            "SELECT DISTINCT source FROM transactions WHERE source IS NOT NULL ORDER BY source"
        ):
            fontes.append({"fonte": fonte, "nome": display_name(fonte)})
    return {
        "linhas": [
            {
                "data": linha.data.isoformat(),
                "fonte": linha.fonte,
                "nome_fonte": display_name(linha.fonte),
                "conta": linha.conta,
                "descricao": linha.descricao,
                "montante": float(linha.valor),
                "moeda": linha.moeda,
                "tipo": linha.tipo,
                "categoria": linha.categoria,
                "cor": _cor(linha.categoria),
            }
            for linha in linhas[:limite]
        ],
        "total": len(linhas),
        "limite": limite,
        "fontes": fontes,
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
