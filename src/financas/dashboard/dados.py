"""Preparação de dados do dashboard. Funções puras, sem desenhar."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from financas.analise import (
    _normalizar_descricao,
    benchmarks,
    comparacao_mes,
    gastos_por_categoria,
    gastos_por_dia_semana,
    fontes_investimento,
    rendimentos_ate,
    rentabilidade,
    serie_capital,
    subscricoes,
    taxa_poupanca,
    top_comerciantes,
)
from financas.categorias import TIPOS, carregar_regras, classify
from financas.report import display_name

PALETA = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
)
COR_OUTROS = "#888780"
_ORDEM = (
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
    "Investimento",
    "Transferência interna",
    "Ações/ETFs",
    "Cash",
    "Bitcoin",
    "Dívida pública",
    "Santander",
    "Revolut",
    "Trade Republic",
    "IBKR",
    "eToro",
    "Certificados de Aforro",
)


def cor(nome: str) -> str:
    """Cor estável pelo nome. «Outros» é sempre cinzento, em qualquer posição."""
    texto = display_name((nome or "").strip())
    if texto.casefold() == "outros":
        return COR_OUTROS
    try:
        indice = _ORDEM.index(texto)
    except ValueError:
        indice = _hash_nome(texto)
    return PALETA[indice % len(PALETA)]


def _hash_nome(nome: str) -> int:
    acumulado = 2166136261
    for caractere in nome.casefold():
        acumulado ^= ord(caractere)
        acumulado = (acumulado * 16777619) & 0xFFFFFFFF
    return acumulado


def formatar_decimal(valor: Decimal, casas: int) -> str:
    quantizado = Decimal(valor).quantize(Decimal(10) ** -casas, rounding=ROUND_HALF_UP)
    if quantizado == 0:
        quantizado = Decimal(0)
    sinal = "-" if quantizado < 0 else ""
    texto = f"{abs(quantizado):.{casas}f}"
    inteiro, _, fraccao = texto.partition(".")
    grupos: list[str] = []
    while inteiro:
        grupos.append(inteiro[-3:])
        inteiro = inteiro[:-3]
    corpo = " ".join(reversed(grupos))
    if casas:
        return f"{sinal}{corpo},{fraccao}"
    return f"{sinal}{corpo}"


def formatar_eur(valor: Decimal) -> str:
    return f"{formatar_decimal(valor, 2)} €"


def formatar_data(dia: date) -> str:
    return dia.strftime("%d/%m/%Y")


def formatar_percentagem(razao: Decimal | None) -> str:
    if razao is None:
        return "—"
    return formatar_decimal(razao * Decimal(100), 1) + "%"


def formatar_indice(valor: Decimal) -> str:
    return formatar_decimal(valor, 1)


def mostrar_montante(valor: Decimal | None, esconder: bool) -> str:
    if esconder or valor is None:
        return "—"
    return formatar_eur(valor)


def mascara_serie(
    valores: list[Decimal],
    esconder: bool,
    base: Decimal | None = None,
) -> tuple[list[Decimal], list[str]]:
    """Euros, ou índice em que a base passa a 100."""
    lista = [Decimal(valor) for valor in valores]
    if not esconder:
        return lista, [formatar_eur(valor) for valor in lista]
    if base is None:
        base = next((valor for valor in lista if valor != 0), None)
    if base is None or base == 0:
        convertidos = [Decimal(0) for _ in lista]
    else:
        convertidos = [valor / base * Decimal(100) for valor in lista]
    return convertidos, [formatar_indice(valor) for valor in convertidos]


def _deslocar_meses(dia: date, meses: int) -> date:
    absoluto = dia.month - 1 + meses
    ano = dia.year + absoluto // 12
    mes = absoluto % 12 + 1
    ultimo = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, min(dia.day, ultimo))


def resolver_periodo(
    escolha: str,
    hoje: date,
    inicio: date | None = None,
    fim: date | None = None,
) -> tuple[date, date]:
    if escolha == "este mês":
        return date(hoje.year, hoje.month, 1), hoje
    if escolha == "mês passado":
        fim_passado = date(hoje.year, hoje.month, 1) - timedelta(days=1)
        return date(fim_passado.year, fim_passado.month, 1), fim_passado
    if escolha == "últimos 3 meses":
        return _deslocar_meses(hoje, -3), hoje
    if escolha == "últimos 12 meses":
        return _deslocar_meses(hoje, -12), hoje
    if escolha == "personalizado":
        if inicio is None or fim is None:
            raise ValueError("intervalo personalizado sem datas")
        if inicio > fim:
            raise ValueError("a data de início é posterior à de fim")
        return inicio, fim
    raise ValueError(f"período desconhecido: {escolha}")


def _periodos(inicio: date, fim: date, freq: str):
    if freq == "mes":
        cursor = date(inicio.year, inicio.month, 1)
        while cursor <= fim:
            fim_mes = _deslocar_meses(cursor, 1) - timedelta(days=1)
            chave = f"{cursor.year:04d}-{cursor.month:02d}"
            yield chave, max(cursor, inicio), min(fim_mes, fim)
            cursor = fim_mes + timedelta(days=1)
        return
    if freq != "semana":
        raise ValueError(f"frequência inválida: {freq}")
    cursor = inicio - timedelta(days=inicio.weekday())
    while cursor <= fim:
        fim_semana = cursor + timedelta(days=6)
        if fim_semana >= inicio:
            iso = cursor.isocalendar()
            chave = f"{iso.year}-W{iso.week:02d}"
            yield chave, max(cursor, inicio), min(fim_semana, fim)
        cursor = fim_semana + timedelta(days=1)


@dataclass(frozen=True)
class VariacaoPatrimonio:
    total: Decimal
    dia: date | None
    anterior: Decimal | None
    dia_anterior: date | None
    delta_anterior: Decimal | None
    ha_30_dias: Decimal | None
    dia_30: date | None
    delta_30: Decimal | None


def _comuns(esquerda: dict, direita: dict) -> tuple[Decimal, Decimal] | None:
    chaves = set(esquerda) & set(direita)
    if not chaves:
        return None
    base = sum((esquerda[chave] for chave in chaves), start=Decimal(0))
    atual = sum((direita[chave] for chave in chaves), start=Decimal(0))
    return base, atual


def variacao_patrimonio(serie) -> VariacaoPatrimonio:
    if not serie:
        return VariacaoPatrimonio(Decimal(0), None, None, None, None, None, None, None)
    dia, fontes, total = serie[-1]
    anterior = dia_anterior = delta_anterior = None
    if len(serie) >= 2:
        dia_anterior, fontes_anteriores, _total_anterior = serie[-2]
        comparado = _comuns(fontes_anteriores, fontes)
        if comparado is not None:
            anterior, atual = comparado
            delta_anterior = atual - anterior
    limite = dia - timedelta(days=30)
    candidatos = [ponto for ponto in serie if ponto[0] <= limite]
    ha_30 = dia_30 = delta_30 = None
    if candidatos:
        dia_30, fontes_30, _total_30 = candidatos[-1]
        comparado = _comuns(fontes_30, fontes)
        if comparado is not None:
            ha_30, atual = comparado
            delta_30 = atual - ha_30
    return VariacaoPatrimonio(
        total, dia, anterior, dia_anterior, delta_anterior, ha_30, dia_30, delta_30
    )


def variacao_relativa(delta: Decimal | None, base: Decimal | None) -> Decimal | None:
    if delta is None or base in (None, 0):
        return None
    return delta / base


def veredicto_benchmark(xirr_tu: Decimal | None, xirr_indice: Decimal | None) -> str:
    if xirr_tu is None or xirr_indice is None:
        return "Sem XIRR para comparar com o índice."
    pontos = ((xirr_tu - xirr_indice) * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if pontos == 0:
        return "Empatou com o índice."
    texto = formatar_decimal(abs(pontos), 1)
    if pontos > 0:
        return f"Bateu o índice por {texto} pp/ano."
    return f"Ficou atrás do índice por {texto} pp/ano."


@dataclass(frozen=True)
class Fatia:
    nome: str
    valor: Decimal
    texto: str
    cor: str


def fatias(totais: dict[str, Decimal], esconder: bool) -> list[Fatia]:
    itens = [(nome, Decimal(valor)) for nome, valor in totais.items() if Decimal(valor) != 0]
    total = sum((valor for _nome, valor in itens), start=Decimal(0))
    if total == 0:
        return []
    itens.sort(key=lambda item: (-item[1], item[0]))
    resultado = []
    for nome, valor in itens:
        peso = valor / total
        if esconder:
            resultado.append(Fatia(nome, peso * Decimal(100), formatar_percentagem(peso), cor(nome)))
        else:
            texto = f"{formatar_eur(valor)} ({formatar_percentagem(peso)})"
            resultado.append(Fatia(nome, valor, texto, cor(nome)))
    return resultado


@dataclass(frozen=True)
class ComparacaoVista:
    categoria: str
    este_mes: str
    mes_anterior: str
    media_3_meses: str
    vs_anterior: str
    vs_media: str


def _variacao_texto(atual: Decimal, base: Decimal) -> str:
    if base == 0:
        return "—"
    return formatar_percentagem((atual - base) / base)


def apresentar_comparacao(linhas, esconder: bool) -> list[ComparacaoVista]:
    return [
        ComparacaoVista(
            linha.categoria,
            mostrar_montante(linha.este_mes, esconder),
            mostrar_montante(linha.mes_anterior, esconder),
            mostrar_montante(linha.media_3_meses, esconder),
            _variacao_texto(linha.este_mes, linha.mes_anterior),
            _variacao_texto(linha.este_mes, linha.media_3_meses),
        )
        for linha in linhas
    ]


def _toml(texto: str) -> str:
    escapado = texto.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escapado}"'


def gravar_regra(caminho: Path, descricao: str, categoria: str, tipo: str) -> None:
    """Regra nova no início do TOML. O resto do ficheiro fica como estava."""
    if tipo not in TIPOS:
        raise ValueError(f"tipo desconhecido: {tipo}")
    normalizada = _normalizar_descricao(descricao or "")
    if not normalizada.strip():
        raise ValueError("A descrição não tem texto para uma regra.")
    bloco = (
        "[[regra]]\n"
        f"categoria = {_toml(categoria)}\n"
        f"tipo = {_toml(tipo)}\n"
        f"padrao = {_toml(re.escape(normalizada))}\n\n"
    )
    resto = caminho.read_text(encoding="utf-8") if caminho.is_file() else ""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(_inserir_regra(resto, bloco), encoding="utf-8")


def _inserir_regra(resto: str, bloco: str) -> str:
    """A regra nova fica a primeira, sem engolir chaves de topo como contas_proprias."""
    if not resto.strip():
        return bloco
    linhas = resto.splitlines(keepends=True)
    for indice, linha in enumerate(linhas):
        if linha.lstrip().startswith("[[regra]]"):
            return "".join(linhas[:indice]) + bloco + "".join(linhas[indice:])
    texto = resto if resto.endswith("\n") else resto + "\n"
    return texto + "\n" + bloco


def ligar():
    from financas import config, store

    return store.connect(config.DATA / "financas.db")


def avisos_ultimo_run(db) -> list[tuple[str, str, str]]:
    existe = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_status'"
    ).fetchone()
    if existe is None:
        return []
    linha = db.execute("SELECT MAX(run_id) FROM run_status").fetchone()
    if linha is None or linha[0] is None:
        return []
    return [
        (fonte, estado or "", avisos or "")
        for fonte, estado, avisos in db.execute(
            "SELECT source, status, warnings FROM run_status WHERE run_id = ? ORDER BY source",
            (linha[0],),
        )
    ]


@dataclass(frozen=True)
class PainelGastos:
    por_categoria: dict[str, Decimal]
    avisos: tuple[str, ...]
    periodos: tuple[str, ...]
    empilhado: dict[str, dict[str, Decimal]]
    comerciantes: tuple[tuple[str, Decimal], ...]
    subscricoes: tuple
    dias: tuple[tuple[str, Decimal], ...]
    taxa: Decimal | None
    comparacao: tuple


def preparar_gastos(db, inicio: date, fim: date, freq: str, classify=None) -> PainelGastos:
    mapa = gastos_por_categoria(db, inicio, fim, classify=classify)
    avisos = list(mapa.avisos)
    periodos: list[str] = []
    empilhado: dict[str, dict[str, Decimal]] = {}
    for chave, inicio_periodo, fim_periodo in _periodos(inicio, fim, freq):
        parcial = gastos_por_categoria(db, inicio_periodo, fim_periodo, classify=classify)
        for aviso in parcial.avisos:
            if aviso not in avisos:
                avisos.append(aviso)
        if not any(valor != 0 for valor in parcial.values()):
            continue
        periodos.append(chave)
        for categoria, valor in parcial.items():
            if valor == 0:
                continue
            empilhado.setdefault(categoria, {})[chave] = valor
    return PainelGastos(
        dict(mapa),
        tuple(avisos),
        tuple(periodos),
        empilhado,
        tuple(top_comerciantes(db, inicio, fim, classify=classify)),
        tuple(subscricoes(db, fim, classify=classify)),
        tuple(gastos_por_dia_semana(db, inicio, fim, classify=classify)),
        taxa_poupanca(db, inicio, fim, classify=classify),
        tuple(comparacao_mes(db, fim, classify=classify)),
    )


@dataclass(frozen=True)
class MovimentoLinha:
    fonte: str
    conta: str
    data: date
    valor: Decimal
    moeda: str
    descricao: str
    tipo: str
    categoria: str


def listar_movimentos(
    db,
    *,
    fonte: str | None = None,
    conta: str | None = None,
    categoria: str | None = None,
    tipo: str | None = None,
    texto: str | None = None,
    inicio: date | None = None,
    fim: date | None = None,
    classify_fn=None,
) -> list[MovimentoLinha]:
    regras = None if classify_fn is not None else carregar_regras()
    clausulas: list[str] = []
    parametros: list[str] = []
    if fonte:
        clausulas.append("source = ?")
        parametros.append(fonte)
    if conta:
        clausulas.append("account = ?")
        parametros.append(conta)
    if inicio is not None:
        clausulas.append("booked >= ?")
        parametros.append(inicio.isoformat())
    if fim is not None:
        clausulas.append("booked <= ?")
        parametros.append(fim.isoformat())
    sql = "SELECT source, account, booked, amount, currency, description FROM transactions"
    if clausulas:
        sql += " WHERE " + " AND ".join(clausulas)
    sql += " ORDER BY booked, ext_id"
    procura = (texto or "").casefold()
    linhas: list[MovimentoLinha] = []
    for source, account, booked, amount, currency, description in db.execute(sql, parametros):
        montante = Decimal(str(amount))
        descricao_txt = description or ""
        if classify_fn is not None:
            classe = classify_fn(source or "", account or "", descricao_txt, montante)
        else:
            classe = classify(source or "", account or "", descricao_txt, montante, regras)
        if categoria and classe.categoria != categoria:
            continue
        if tipo and classe.tipo != tipo:
            continue
        if procura and procura not in descricao_txt.casefold():
            continue
        linhas.append(
            MovimentoLinha(
                source or "",
                account or "",
                date.fromisoformat(str(booked)[:10]),
                montante,
                (currency or "EUR").upper(),
                descricao_txt,
                classe.tipo,
                classe.categoria,
            )
        )
    return linhas


def curva_capital(db, fonte: str | None) -> list[tuple[date, Decimal, Decimal]]:
    return serie_capital(db, fonte)


def montar_investimentos(db, symbol: str = "IWDA.AS"):
    """Tabela, curvas e benchmark das plataformas. Os preços lêem-se uma vez."""
    linhas = rentabilidade(db)
    fontes = [None if linha.fonte == "total" else linha.fonte for linha in linhas]
    referencias = benchmarks(db, fontes, symbol=symbol)
    blocos = []
    for linha, referencia in zip(linhas, referencias, strict=True):
        fonte = None if linha.fonte == "total" else linha.fonte
        nome = "Total" if fonte is None else display_name(linha.fonte)
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
    return blocos


def rendimentos_por_ano(db, fonte: str | None) -> list[tuple[int, Decimal]]:
    clausulas = ["kind IN ('dividendo', 'juros')"]
    parametros: list[str] = []
    if fonte is not None:
        clausulas.append("source = ?")
        parametros.append(fonte)
    else:
        fontes = fontes_investimento()
        if not fontes:
            return []
        clausulas.append(f"source IN ({','.join('?' * len(fontes))})")
        parametros.extend(fontes)
    sql = (
        "SELECT DISTINCT substr(booked, 1, 4) FROM cashflows WHERE "
        + " AND ".join(clausulas)
        + " ORDER BY 1"
    )
    anos = [int(ano) for (ano,) in db.execute(sql, parametros)]
    if not anos:
        return []
    acumulado = rendimentos_ate(db, fonte, date(anos[0] - 1, 12, 31))
    resultado = []
    for ano in anos:
        actual = rendimentos_ate(db, fonte, date(ano, 12, 31))
        resultado.append((ano, actual - acumulado))
        acumulado = actual
    return resultado
