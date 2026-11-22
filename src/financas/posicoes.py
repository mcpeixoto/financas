"""Posições abertas, ganhos realizados e histórico ao custo ou a mercado."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from financas.analise import (
    _cashflows,
    _fontes_investimento,
    _snapshots_valor,
    _tem_deposito,
    _ultimo_run_por_fonte,
    alocacao,
    benchmarks,
    rentabilidade,
)
from financas.report import display_name, ordered_sources

_FX = {"USD": "EURUSD=X", "GBP": "EURGBP=X"}
_INDICE = "IWDA.AS"
_FONTE_ROTULO = {"bitcoin": "Cold wallet"}


def nome_fonte_rotulo(fonte: str) -> str:
    """Nome da fonte para juntar a um rótulo repetido, ex.: «Bitcoin (Cold wallet)»."""
    return _FONTE_ROTULO.get(fonte, display_name(fonte))


@dataclass(frozen=True)
class PosicaoAtual:
    fonte: str
    simbolo: str
    nome: str
    classe: str
    quantidade: Decimal
    valor_eur: Decimal
    custo_eur: Decimal | None
    latente_eur: Decimal | None
    latente_pct: Decimal | None
    peso: Decimal
    realizado_eur: Decimal
    dividendos_eur: Decimal


@dataclass(frozen=True)
class RealizadoSimbolo:
    fonte: str
    simbolo: str
    nome: str
    vendas: int
    ganhos: int
    realizado_eur: Decimal
    ainda_aberta: bool


@dataclass(frozen=True)
class EstatisticasNegocios:
    fechados: int
    taxa_acerto: Decimal | None
    ganho_medio_eur: Decimal | None
    perda_media_eur: Decimal | None
    melhor: tuple[str, Decimal] | None
    pior: tuple[str, Decimal] | None
    fator_lucro: Decimal | None
    realizado_total_eur: Decimal


@dataclass(frozen=True)
class Historico:
    pontos: list[tuple[date, dict[str, Decimal]]]
    sem_preco: list[str]


@dataclass(frozen=True)
class _Venda:
    fonte: str
    simbolo: str
    nome: str
    dia: date
    pnl: Decimal


@dataclass
class _Lote:
    qty: Decimal
    custo: Decimal
    nome: str


def posicoes_atuais(db) -> list[PosicaoAtual]:
    """Último snapshot de cada fonte, somado por instrumento."""
    grupos: dict[tuple[str, str], dict] = {}
    for fonte, run_id in _ultimo_run_por_fonte(db).items():
        for symbol, name, quantity, value, cost_basis, value_eur in db.execute(
            """SELECT symbol, name, quantity, value, cost_basis, value_eur
               FROM positions WHERE run_id = ? AND source = ?""",
            (run_id, fonte),
        ):
            chave = (fonte, symbol or "")
            grupo = grupos.get(chave)
            if grupo is None:
                grupo = {
                    "quantidade": Decimal(0),
                    "valor": Decimal(0),
                    "valor_eur": Decimal(0),
                    "custo": Decimal(0),
                    "tem_custo": False,
                    "nome": "",
                }
                grupos[chave] = grupo
            grupo["quantidade"] += _num(quantity)
            grupo["valor"] += _num(value)
            grupo["valor_eur"] += _num(value_eur)
            custo = _opc(cost_basis)
            if custo is not None:
                grupo["custo"] += custo
                grupo["tem_custo"] = True
            if name and not grupo["nome"]:
                grupo["nome"] = name

    meta = _meta_negocios(db)
    realizados = _soma_realizado(db)
    dividendos = _soma_dividendos(db)
    total = sum((grupo["valor_eur"] for grupo in grupos.values()), start=Decimal(0))
    linhas: list[PosicaoAtual] = []
    for (fonte, simbolo), grupo in grupos.items():
        nome_neg, classe = meta.get((fonte, simbolo), ("", ""))
        valor = grupo["valor_eur"]
        if grupo["tem_custo"] and grupo["valor"] != 0:
            custo_eur: Decimal | None = grupo["custo"] * valor / grupo["valor"]
        else:
            custo_eur = None
        if custo_eur is None:
            latente = None
            latente_pct = None
        else:
            latente = valor - custo_eur
            latente_pct = None if custo_eur == 0 else latente / custo_eur
        linhas.append(
            PosicaoAtual(
                fonte,
                simbolo,
                nome_neg or grupo["nome"] or simbolo,
                classe or ("cripto" if fonte == "bitcoin" else "acao"),
                grupo["quantidade"],
                valor,
                custo_eur,
                latente,
                latente_pct,
                valor / total if total != 0 else Decimal(0),
                realizados.get((fonte, simbolo), Decimal(0)),
                dividendos.get((fonte, simbolo), Decimal(0)),
            )
        )
    linhas.sort(key=lambda item: (-item.valor_eur, item.fonte, item.simbolo))
    return linhas


def realizado_por_simbolo(db, source: str | None = None) -> list[RealizadoSimbolo]:
    """Vendas com ganho conhecido, por instrumento. Ignora câmbios."""
    grupos: dict[tuple[str, str], dict] = {}
    for venda in _vendas(db, source):
        chave = (venda.fonte, venda.simbolo)
        grupo = grupos.get(chave)
        if grupo is None:
            grupo = {"vendas": 0, "ganhos": 0, "total": Decimal(0), "nome": ""}
            grupos[chave] = grupo
        grupo["vendas"] += 1
        if venda.pnl > 0:
            grupo["ganhos"] += 1
        grupo["total"] += venda.pnl
        if venda.nome:
            grupo["nome"] = venda.nome
    abertas = _quantidades(db, source)
    linhas = [
        RealizadoSimbolo(
            fonte,
            simbolo,
            grupo["nome"] or simbolo,
            grupo["vendas"],
            grupo["ganhos"],
            grupo["total"],
            abertas.get((fonte, simbolo), Decimal(0)) > 0,
        )
        for (fonte, simbolo), grupo in grupos.items()
    ]
    linhas.sort(key=lambda item: (-item.realizado_eur, item.fonte, item.simbolo))
    return linhas


def realizado_por_ano(db, source: str | None = None) -> list[tuple[int, Decimal]]:
    """Soma do realizado por ano civil. Ignora câmbios e vendas sem PnL."""
    totais: dict[int, Decimal] = {}
    for venda in _vendas(db, source):
        totais[venda.dia.year] = totais.get(venda.dia.year, Decimal(0)) + venda.pnl
    return sorted(totais.items())


def estatisticas_negocios(db, source: str | None = None) -> EstatisticasNegocios:
    """Taxa de acerto, médias e extremos das vendas com PnL conhecido."""
    vendas = _vendas(db, source)
    if not vendas:
        return EstatisticasNegocios(0, None, None, None, None, None, None, Decimal(0))
    ganhos = [venda.pnl for venda in vendas if venda.pnl > 0]
    perdas = [venda.pnl for venda in vendas if venda.pnl < 0]
    melhor = min(vendas, key=lambda venda: (-venda.pnl, venda.simbolo))
    pior = min(vendas, key=lambda venda: (venda.pnl, venda.simbolo))
    fechados = len(vendas)
    return EstatisticasNegocios(
        fechados,
        Decimal(len(ganhos)) / Decimal(fechados),
        sum(ganhos, start=Decimal(0)) / len(ganhos) if ganhos else None,
        sum(perdas, start=Decimal(0)) / len(perdas) if perdas else None,
        (melhor.simbolo, melhor.pnl),
        (pior.simbolo, pior.pnl),
        sum(ganhos, start=Decimal(0)) / abs(sum(perdas, start=Decimal(0))) if perdas else None,
        sum((venda.pnl for venda in vendas), start=Decimal(0)),
    )


def historico_custo(db, freq: str = "mes") -> Historico:
    """Capital ainda investido, em euros, no fim de cada mês ou semana."""
    return _historico(db, freq, valorizar=False)


def historico_valor(db, freq: str = "mes") -> Historico:
    """Valor de mercado no fim de cada período. Sem cotação, fica ao custo."""
    return _historico(db, freq, valorizar=True)


@dataclass(frozen=True)
class PontoPlataforma:
    dia: date
    depositado: Decimal
    valor: Decimal
    indice: Decimal | None


def historico_plataforma(db, source: str | None = None, freq: str = "mes") -> list[PontoPlataforma]:
    """Depositado, valor estimado e os mesmos fluxos no MSCI World, no fim de cada período.

    ``source=None`` soma as plataformas de investimento com depósitos. Uma fonte sem
    depósitos devolve uma lista vazia. No último ponto o valor é o do último snapshot.
    """
    if freq not in {"mes", "semana"}:
        raise ValueError("A frequência tem de ser 'mes' ou 'semana'.")
    if source is None:
        fontes = plataformas_com_depositos(db)
    else:
        fontes = [source] if _tem_deposito(db, source, None) else []
    if not fontes:
        return []
    todos = _negocios(db)
    eventos = {}
    for fonte in fontes:
        fluxos = _cashflows(db, fonte, None)
        negocios = [linha for linha in todos if linha[1] == fonte]
        eventos[fonte] = (fluxos, negocios)
    primeiros = [
        item[0]
        for fluxos, negocios in eventos.values()
        for item in (fluxos[:1] + negocios[:1])
    ]
    inicio = min(primeiros)
    fim = date.today()
    if fim < inicio:
        fim = inicio
    marcos = _fins(inicio, fim, freq)
    tickers = _tickers(db)
    precos = _precos(db)
    somas: list[list] = [[Decimal(0), Decimal(0), Decimal(0), True] for _ in marcos]
    for fonte in fontes:
        fluxos, negocios = eventos[fonte]
        snapshots = _snapshots_valor(db, fonte)
        ultimo = snapshots[-1][1] if snapshots else None
        serie = _serie_plataforma(fluxos, negocios, marcos, tickers, precos, ultimo)
        for soma, (depositado, valor, indice) in zip(somas, serie, strict=True):
            soma[0] += depositado
            soma[1] += valor
            if indice is None:
                soma[3] = False
            else:
                soma[2] += indice
    return [
        PontoPlataforma(dia, soma[0], soma[1], soma[2] if soma[3] else None)
        for dia, soma in zip(marcos, somas, strict=True)
    ]


def plataformas_com_depositos(db) -> list[str]:
    """Plataformas de investimento com pelo menos um depósito."""
    return ordered_sources(
        fonte for fonte in _fontes_investimento(db, None, None) if _tem_deposito(db, fonte, None)
    )


def plataformas_sem_depositos(db) -> list[str]:
    """Plataformas de investimento sem nenhum depósito registado."""
    return ordered_sources(
        fonte for fonte in _fontes_investimento(db, None, None) if not _tem_deposito(db, fonte, None)
    )


def _serie_plataforma(fluxos, negocios, marcos, tickers, precos, ultimo_snapshot):
    referencia = precos.get(_INDICE) or []
    estados: dict[tuple[str, str], _Lote] = {}
    depositado = Decimal(0)
    caixa = Decimal(0)
    unidades = Decimal(0)
    replicavel = bool(referencia)
    i_fluxo = 0
    i_negocio = 0
    pontos = []
    for dia in marcos:
        while i_fluxo < len(fluxos) and fluxos[i_fluxo][0] <= dia:
            quando, montante, kind = fluxos[i_fluxo]
            caixa += montante
            if kind in {"deposito", "levantamento"}:
                depositado += montante
                preco = _preco_indice(referencia, quando, precos, primeiro=True)
                if preco is None:
                    replicavel = False
                else:
                    unidades += montante / preco
            i_fluxo += 1
        while i_negocio < len(negocios) and negocios[i_negocio][0] <= dia:
            _quando, fonte, simbolo, nome, quantidade, net_cash, cambio = negocios[i_negocio]
            caixa += net_cash * cambio
            _aplicar(estados, (fonte, simbolo), nome, quantidade, net_cash, cambio)
            i_negocio += 1
        mercado = sum(
            (_valorizar(lote, chave, dia, tickers, precos)[0] for chave, lote in estados.items()),
            start=Decimal(0),
        )
        indice = None
        if replicavel:
            preco = _preco_indice(referencia, dia, precos, primeiro=False)
            if preco is not None:
                indice = unidades * preco
        pontos.append([depositado, caixa + mercado, indice])
    if pontos and ultimo_snapshot is not None:
        pontos[-1][1] = ultimo_snapshot
    return [tuple(ponto) for ponto in pontos]


def _preco_indice(serie, dia: date, precos, primeiro: bool) -> Decimal | None:
    """Preço em EUR do índice no dia (ou antes). Com ``primeiro``, aceita o primeiro preço a seguir."""
    if not serie:
        return None
    cotacao = _preco_ate(serie, dia)
    quando = dia
    if cotacao is None:
        if not primeiro:
            return None
        quando, close, moeda = serie[0]
        cotacao = (close, moeda)
    close, moeda = cotacao
    valor = _em_eur(close, moeda or "EUR", quando, precos)
    if valor is None or valor == 0:
        return None
    return valor


def veredicto(db) -> list[str]:
    """Frases curtas, só com percentagens, sobre o que os dados permitem dizer."""
    frases: list[str] = []
    linhas = rentabilidade(db)
    total = next((linha for linha in linhas if linha.fonte == "total"), None)
    if total is not None and total.xirr is not None:
        referencias = benchmarks(db, [None], symbol="IWDA.AS")
        if referencias and referencias[0].xirr is not None:
            frases.append(
                f"A rentabilidade anual total foi de {_pct(total.xirr)}%, "
                f"contra {_pct(referencias[0].xirr)}% do MSCI World."
            )

    posicoes = posicoes_atuais(db)
    com_latente = [posicao for posicao in posicoes if posicao.latente_pct is not None]
    if com_latente:
        melhor = max(com_latente, key=lambda posicao: (posicao.latente_pct, posicao.valor_eur, posicao.nome))
        pior = min(com_latente, key=lambda posicao: (posicao.latente_pct, -posicao.valor_eur, posicao.nome))
        if (melhor.fonte, melhor.simbolo) == (pior.fonte, pior.simbolo):
            frases.append(
                f"A melhor posição aberta é {melhor.nome} ({_pct(melhor.latente_pct)}% de ganho latente)."
            )
        else:
            frases.append(
                f"A melhor posição aberta é {melhor.nome} ({_pct(melhor.latente_pct)}% de ganho latente) "
                f"e a pior é {pior.nome} ({_pct(pior.latente_pct)}%)."
            )
    if posicoes and posicoes[0].valor_eur != 0:
        maior = posicoes[0]
        alerta = ", acima de 25%" if maior.peso > Decimal("0.25") else ""
        frases.append(f"A maior posição, {maior.nome}, pesa {_pct(maior.peso)}%{alerta}.")

    patrimonio = alocacao(db)
    total_patrimonio = sum(patrimonio.values(), start=Decimal(0))
    if total_patrimonio > 0:
        cash = patrimonio.get("Cash", Decimal(0))
        frases.append(f"O cash representa {_pct(cash / total_patrimonio)}% do património.")

    stats = estatisticas_negocios(db)
    if stats.taxa_acerto is not None:
        frases.append(f"A taxa de acerto dos negócios fechados é de {_pct(stats.taxa_acerto)}%.")

    sem_depositos = [
        linha.fonte
        for linha in linhas
        if linha.fonte != "total" and linha.nota == "sem dados de depósitos"
    ]
    if sem_depositos:
        nomes = [display_name(fonte) for fonte in ordered_sources(sem_depositos)]
        frases.append(f"Sem dados de depósitos em {_juntar(nomes)}.")
    return frases


def _historico(db, freq: str, valorizar: bool) -> Historico:
    if freq not in {"mes", "semana"}:
        raise ValueError("A frequência tem de ser 'mes' ou 'semana'.")
    negocios = _negocios(db)
    if not negocios:
        return Historico([], [])
    inicio = negocios[0][0]
    fim = date.today()
    if fim < inicio:
        fim = inicio
    marcos = _fins(inicio, fim, freq)
    estados: dict[tuple[str, str], _Lote] = {}
    fotos: list[dict[tuple[str, str], Decimal]] = []
    sem_chaves: set[tuple[str, str]] = set()
    tickers = _tickers(db) if valorizar else {}
    precos = _precos(db) if valorizar else {}
    indice = 0

    def fotografar(dia: date) -> None:
        valores: dict[tuple[str, str], Decimal] = {}
        for chave, lote in estados.items():
            if not valorizar:
                valores[chave] = lote.custo
                continue
            valor, sem = _valorizar(lote, chave, dia, tickers, precos)
            valores[chave] = valor
            if sem:
                sem_chaves.add(chave)
        fotos.append(valores)

    for dia, fonte, simbolo, nome, quantidade, net_cash, cambio in negocios:
        while indice < len(marcos) and dia > marcos[indice]:
            fotografar(marcos[indice])
            indice += 1
        _aplicar(estados, (fonte, simbolo), nome, quantidade, net_cash, cambio)
    while indice < len(marcos):
        fotografar(marcos[indice])
        indice += 1
    return _agrupar(estados, fotos, marcos, sem_chaves)


def _aplicar(estados, chave, nome, quantidade, net_cash, cambio) -> None:
    lote = estados.get(chave)
    if lote is None:
        lote = _Lote(Decimal(0), Decimal(0), chave[1])
        estados[chave] = lote
    if nome:
        lote.nome = nome
    if quantidade > 0:
        lote.custo += abs(net_cash) * cambio
        lote.qty += quantidade
        return
    if quantidade < 0:
        if lote.qty > 0:
            restante = lote.qty + quantidade
            lote.custo = Decimal(0) if restante <= 0 else lote.custo * restante / lote.qty
        else:
            lote.custo = Decimal(0)
        lote.qty += quantidade


def _agrupar(estados, fotos, marcos, sem_chaves) -> Historico:
    nomes = _nomes(estados)
    if not fotos:
        return Historico([], sorted(nomes[chave] for chave in sem_chaves))
    ultimo = fotos[-1]
    ordenado = sorted(estados, key=lambda chave: (-ultimo.get(chave, Decimal(0)), nomes[chave]))
    principais = ordenado[:10]
    resto = ordenado[10:]
    pontos = []
    for dia, foto in zip(marcos, fotos, strict=True):
        valores = {nomes[chave]: foto.get(chave, Decimal(0)) for chave in principais}
        if resto:
            valores["Outras"] = sum((foto.get(chave, Decimal(0)) for chave in resto), start=Decimal(0))
        pontos.append((dia, valores))
    return Historico(pontos, sorted({nomes[chave] for chave in sem_chaves}))


def _nomes(estados: dict[tuple[str, str], _Lote]) -> dict[tuple[str, str], str]:
    base = {chave: (lote.nome.strip() or chave[1] or chave[0]) for chave, lote in estados.items()}
    grupos: dict[str, list[tuple[str, str]]] = {}
    for chave, nome in base.items():
        grupos.setdefault(nome, []).append(chave)
    usados: set[str] = set()
    final: dict[tuple[str, str], str] = {}
    for chave in sorted(base):
        nome = base[chave]
        chaves = grupos[nome]
        fontes = {item[0] for item in chaves}
        if len(fontes) > 1:
            mesma_fonte = sum(1 for item in chaves if item[0] == chave[0])
            if mesma_fonte > 1:
                candidato = f"{nome} ({chave[1] or chave[0]}, {nome_fonte_rotulo(chave[0])})"
            else:
                candidato = f"{nome} ({nome_fonte_rotulo(chave[0])})"
        elif len(chaves) > 1 or nome == "Outras":
            candidato = f"{nome} ({chave[1] or chave[0]})"
        else:
            candidato = nome
        if candidato in usados or candidato == "Outras":
            candidato = f"{candidato} ({chave[0]})"
        usados.add(candidato)
        final[chave] = candidato
    return final


def _valorizar(lote, chave, dia, tickers, precos) -> tuple[Decimal, bool]:
    if lote.qty == 0:
        return Decimal(0), False
    yahoo, moeda_ticker = tickers.get(chave, ("", ""))
    if not yahoo:
        return lote.custo, True
    cotacao = _preco_ate(precos.get(yahoo), dia)
    if cotacao is None:
        return lote.custo, True
    close, moeda_preco = cotacao
    valor = _em_eur(lote.qty * close, moeda_preco or moeda_ticker or "EUR", dia, precos)
    if valor is None:
        return lote.custo, True
    return valor, False


def _em_eur(montante: Decimal, moeda: str, dia: date, precos) -> Decimal | None:
    if moeda == "GBp":
        montante = montante / Decimal(100)
        moeda = "GBP"
    else:
        moeda = (moeda or "EUR").upper()
    if moeda == "EUR":
        return montante
    par = _FX.get(moeda)
    if par is None:
        return None
    taxa = _preco_ate(precos.get(par), dia)
    if taxa is None or taxa[0] == 0:
        return None
    return montante / taxa[0]


def _preco_ate(serie, dia: date):
    if not serie:
        return None
    escolhido = None
    for cotacao_dia, close, moeda in serie:
        if cotacao_dia <= dia:
            escolhido = (close, moeda)
        else:
            break
    return escolhido


def _tickers(db) -> dict[tuple[str, str], tuple[str, str]]:
    return {
        (source or "", symbol or ""): (yahoo or "", currency or "")
        for source, symbol, yahoo, currency in db.execute(
            "SELECT source, symbol, yahoo, currency FROM tickers"
        )
    }


def _precos(db) -> dict[str, list[tuple[date, Decimal, str]]]:
    series: dict[str, list[tuple[date, Decimal, str]]] = {}
    for symbol, day, close, currency in db.execute(
        "SELECT symbol, day, close, currency FROM prices ORDER BY symbol, day"
    ):
        series.setdefault(symbol, []).append(
            (_data(day), Decimal(str(close)), currency or "")
        )
    return series


def _negocios(db):
    linhas = []
    for source, booked, symbol, name, quantity, net_cash, fx in db.execute(
        """SELECT source, booked, symbol, name, quantity, net_cash, fx_to_eur
           FROM trades
           WHERE COALESCE(asset_class, '') != 'fx'
           ORDER BY booked, ext_id"""
    ):
        cambio = _opc(fx)
        linhas.append(
            (
                _data(booked),
                source or "",
                symbol or "",
                (name or "").strip(),
                _num(quantity),
                _num(net_cash),
                Decimal(1) if cambio is None else cambio,
            )
        )
    return linhas


def _fins(inicio: date, fim: date, freq: str) -> list[date]:
    if freq == "semana":
        datas = []
        cursor = inicio + timedelta(days=(6 - inicio.weekday()))
        while cursor < fim:
            datas.append(cursor)
            cursor += timedelta(days=7)
        datas.append(fim)
        return datas
    datas = []
    ano, mes = inicio.year, inicio.month
    while True:
        seguinte = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)
        fim_mes = seguinte - timedelta(days=1)
        if fim_mes >= fim:
            datas.append(fim)
            break
        datas.append(fim_mes)
        ano, mes = seguinte.year, seguinte.month
    return datas


def _vendas(db, source: str | None) -> list[_Venda]:
    clausulas = [
        "realized_pnl_eur IS NOT NULL",
        "realized_pnl_eur != ''",
        "COALESCE(asset_class, '') != 'fx'",
    ]
    parametros: list[str] = []
    if source is not None:
        clausulas.append("source = ?")
        parametros.append(source)
    sql = (
        "SELECT source, symbol, name, booked, realized_pnl_eur FROM trades WHERE "
        + " AND ".join(clausulas)
        + " ORDER BY booked, ext_id"
    )
    vendas = []
    for fonte, simbolo, nome, booked, pnl in db.execute(sql, parametros):
        valor = _opc(pnl)
        if valor is None:
            continue
        vendas.append(_Venda(fonte or "", simbolo or "", (nome or "").strip(), _data(booked), valor))
    return vendas


def _quantidades(db, source: str | None) -> dict[tuple[str, str], Decimal]:
    clausulas = ["COALESCE(asset_class, '') != 'fx'"]
    parametros: list[str] = []
    if source is not None:
        clausulas.append("source = ?")
        parametros.append(source)
    sql = "SELECT source, symbol, quantity FROM trades WHERE " + " AND ".join(clausulas)
    totais: dict[tuple[str, str], Decimal] = {}
    for fonte, simbolo, quantity in db.execute(sql, parametros):
        chave = (fonte or "", simbolo or "")
        totais[chave] = totais.get(chave, Decimal(0)) + _num(quantity)
    return totais


def _meta_negocios(db) -> dict[tuple[str, str], tuple[str, str]]:
    meta: dict[tuple[str, str], tuple[str, str]] = {}
    for source, symbol, name, asset_class in db.execute(
        """SELECT source, symbol, name, asset_class FROM trades
           WHERE COALESCE(asset_class, '') != 'fx'
           ORDER BY booked, ext_id"""
    ):
        meta[(source or "", symbol or "")] = ((name or "").strip(), (asset_class or "").strip())
    return meta


def _soma_realizado(db) -> dict[tuple[str, str], Decimal]:
    totais: dict[tuple[str, str], Decimal] = {}
    for source, symbol, pnl in db.execute("SELECT source, symbol, realized_pnl_eur FROM trades"):
        valor = _opc(pnl)
        if valor is None:
            continue
        chave = (source or "", symbol or "")
        totais[chave] = totais.get(chave, Decimal(0)) + valor
    return totais


def _soma_dividendos(db) -> dict[tuple[str, str], Decimal]:
    totais: dict[tuple[str, str], Decimal] = {}
    for source, description, amount in db.execute(
        "SELECT source, description, amount_eur FROM cashflows WHERE kind = 'dividendo'"
    ):
        texto = (description or "").lstrip()
        simbolo, separador, _resto = texto.partition("(")
        if not simbolo or not separador:
            continue
        chave = (source or "", simbolo)
        totais[chave] = totais.get(chave, Decimal(0)) + _num(amount)
    return totais


def _juntar(nomes: list[str]) -> str:
    if len(nomes) == 1:
        return nomes[0]
    if len(nomes) == 2:
        return f"{nomes[0]} e {nomes[1]}"
    return f"{', '.join(nomes[:-1])} e {nomes[-1]}"


def _pct(razao: Decimal) -> str:
    valor = (razao * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if valor == 0:
        valor = Decimal(0)
    return f"{valor:.1f}".replace(".", ",")


def _num(value) -> Decimal:
    convertido = _opc(value)
    return Decimal(0) if convertido is None else convertido


def _opc(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _data(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
