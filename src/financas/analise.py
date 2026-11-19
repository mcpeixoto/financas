"""Estatísticas do dashboard e dos relatórios. Funções puras sobre a ligação SQLite."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

FONTES_BANCARIAS = ("santander", "revolut")
FONTES_INVESTIMENTO = ("ibkr", "etoro", "traderepublic", "aforro", "bitcoin")
_DIAS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)
_ACOES = {"ibkr", "etoro", "traderepublic"}
_CLASSES = ("Ações/ETFs", "Cash", "Bitcoin", "Dívida pública")
_CAPITAL = {"deposito", "levantamento"}
_RENDIMENTO = {"dividendo", "juros"}
_MENSAL = (25, 35)
_ANUAL = (350, 380)
_REF = re.compile(r"\b(?:refs?|refer[eê]ncias?|references?|trx)\b\.?", re.IGNORECASE)
_FALTA = object()


def fontes_bancarias() -> tuple[str, ...]:
    """Contas que entram nos gastos. ``analise.fontes_bancarias`` na config substitui a omissão."""
    return _fontes_config("fontes_bancarias", FONTES_BANCARIAS)


def fontes_investimento() -> tuple[str, ...]:
    """Plataformas da página de investimentos. ``analise.fontes_investimento`` substitui a omissão."""
    return _fontes_config("fontes_investimento", FONTES_INVESTIMENTO)


def _fontes_config(chave: str, omissao: tuple[str, ...]) -> tuple[str, ...]:
    from financas import config

    valor = config.section("analise").get(chave)
    if not isinstance(valor, (list, tuple)):
        return omissao
    return tuple(str(item) for item in valor)


class MapaGastos(dict):
    """Totais por categoria. ``avisos`` lista moedas ignoradas por falta de taxa."""

    def __init__(self, totais: dict[str, Decimal] | None = None, avisos: list[str] | None = None):
        super().__init__(totais or {})
        self.avisos = list(avisos or [])


@dataclass(frozen=True)
class Subscricao:
    nome: str
    periodicidade: str
    custo_anual: Decimal


@dataclass(frozen=True)
class ComparacaoCategoria:
    categoria: str
    este_mes: Decimal
    mes_anterior: Decimal
    media_3_meses: Decimal


@dataclass(frozen=True)
class Rentabilidade:
    fonte: str
    depositado_liquido: Decimal
    rendimentos: Decimal
    valor_atual: Decimal
    ganho_absoluto: Decimal
    ganho_pct: Decimal | None
    xirr: Decimal | None
    nota: str = ""


@dataclass(frozen=True)
class Benchmark:
    symbol: str
    unidades: Decimal
    valor_atual: Decimal
    xirr: Decimal | None
    depositado_liquido: Decimal


@dataclass(frozen=True)
class _Movimento:
    booked: date
    eur: Decimal
    tipo: str
    categoria: str
    comerciante: str


def fontes_bancarias() -> tuple[str, ...]:
    """Contas que entram nos gastos. A config `analise.fontes_bancarias` substitui a omissão."""
    return _fontes_config("fontes_bancarias", FONTES_BANCARIAS)


def fontes_investimento() -> tuple[str, ...]:
    """Plataformas da página de investimentos. A config `analise.fontes_investimento` substitui a omissão."""
    return _fontes_config("fontes_investimento", FONTES_INVESTIMENTO)


def _fontes_config(chave: str, omissao: tuple[str, ...]) -> tuple[str, ...]:
    from financas import config

    valor = config.section("analise").get(chave)
    if not isinstance(valor, (list, tuple)):
        return omissao
    return tuple(str(item) for item in valor)


def gastos_por_categoria(db, inicio: date, fim: date, classify=None) -> MapaGastos:
    """Gastos por categoria, em EUR e sempre positivos.

    Outras moedas usam a taxa ``amount_eur / amount`` do saldo mais recente.
    Sem taxa, a transação não entra e fica registada em ``avisos``.
    """
    movimentos, avisos = _movimentos(db, classify, inicio, fim)
    totais: dict[str, Decimal] = {}
    for movimento in movimentos:
        if movimento.tipo != "gasto":
            continue
        totais[movimento.categoria] = totais.get(movimento.categoria, Decimal(0)) + abs(movimento.eur)
    return MapaGastos(totais, avisos)


def gastos_por_periodo(db, inicio: date, fim: date, freq: str, classify=None) -> list[tuple[str, Decimal]]:
    """Gastos agregados por semana ISO (``2026-W45``), mês (``2026-10``) ou ano."""
    if freq not in {"semana", "mes", "ano"}:
        raise ValueError(f"frequência inválida: {freq}")
    totais: dict[str, Decimal] = {}
    for movimento in _gastos(db, classify, inicio, fim):
        chave = _chave_periodo(movimento.booked, freq)
        totais[chave] = totais.get(chave, Decimal(0)) + abs(movimento.eur)
    return sorted(totais.items())


def top_comerciantes(db, inicio: date, fim: date, n: int = 10, classify=None) -> list[tuple[str, Decimal]]:
    """Os ``n`` comerciantes com mais gastos, depois de normalizar a descrição."""
    grupos: dict[str, list] = {}
    for movimento in _gastos(db, classify, inicio, fim):
        if not movimento.comerciante:
            continue
        valor = abs(movimento.eur)
        chave = movimento.comerciante.casefold()
        grupo = grupos.get(chave)
        if grupo is None:
            grupos[chave] = [movimento.comerciante, valor, valor]
        else:
            grupo[1] += valor
            if valor > grupo[2]:
                grupo[0] = movimento.comerciante
                grupo[2] = valor
    ordenados = sorted(grupos.values(), key=lambda item: (-item[1], item[0]))
    return [(nome, total) for nome, total, _maior in ordenados[:n]]


def subscricoes(db, ate: date, classify=None) -> list[Subscricao]:
    """Comerciante repetido, valor a ±10% e intervalo mensal ou anual, em pelo menos 2 vezes."""
    grupos: dict[str, list[tuple[date, Decimal, str]]] = {}
    for movimento in _gastos(db, classify, None, ate):
        if not movimento.comerciante:
            continue
        chave = movimento.comerciante.casefold()
        grupos.setdefault(chave, []).append((movimento.booked, abs(movimento.eur), movimento.comerciante))

    encontradas: list[Subscricao] = []
    for itens in grupos.values():
        itens.sort()
        cadeia, periodicidade = _melhor_cadeia(itens)
        if periodicidade is None:
            continue
        soma = sum((valor for _, valor, _nome in cadeia), start=Decimal(0))
        media = soma / Decimal(len(cadeia))
        fator = Decimal(12) if periodicidade == "mensal" else Decimal(1)
        encontradas.append(Subscricao(cadeia[0][2], periodicidade, media * fator))
    encontradas.sort(key=lambda item: (-item.custo_anual, item.nome))
    return encontradas


def gastos_por_dia_semana(db, inicio: date, fim: date, classify=None) -> list[tuple[str, Decimal]]:
    """Gastos de segunda a domingo. Os dias sem movimentos ficam a zero."""
    totais = [Decimal(0)] * 7
    for movimento in _gastos(db, classify, inicio, fim):
        totais[movimento.booked.weekday()] += abs(movimento.eur)
    return list(zip(_DIAS, totais, strict=True))


def taxa_poupanca(db, inicio: date, fim: date, classify=None) -> Decimal | None:
    """(receitas − gastos) / receitas. ``None`` quando não há receitas."""
    receitas = Decimal(0)
    gastos = Decimal(0)
    for movimento in _movimentos(db, classify, inicio, fim)[0]:
        if movimento.tipo == "receita":
            receitas += abs(movimento.eur)
        elif movimento.tipo == "gasto":
            gastos += abs(movimento.eur)
    if receitas == 0:
        return None
    return (receitas - gastos) / receitas


def comparacao_mes(db, mes: date, classify=None) -> list[ComparacaoCategoria]:
    """Por categoria: este mês, o mês anterior e a média dos 3 meses anteriores."""
    inicio_mes = date(mes.year, mes.month, 1)
    meses = [_somar_meses(inicio_mes, deslocamento) for deslocamento in range(-3, 1)]
    indice = {(dia.year, dia.month): posicao for posicao, dia in enumerate(meses)}
    somas: dict[str, list[Decimal]] = {}
    fim = _somar_meses(inicio_mes, 1) - timedelta(days=1)
    for movimento in _gastos(db, classify, meses[0], fim):
        posicao = indice.get((movimento.booked.year, movimento.booked.month))
        if posicao is None:
            continue
        valores = somas.setdefault(movimento.categoria, [Decimal(0)] * 4)
        valores[posicao] += abs(movimento.eur)

    linhas = []
    for categoria in sorted(somas):
        valores = somas[categoria]
        if all(valor == 0 for valor in valores):
            continue
        media = (valores[0] + valores[1] + valores[2]) / Decimal(3)
        linhas.append(ComparacaoCategoria(categoria, valores[3], valores[2], media))
    return linhas


def patrimonio_serie(db) -> list[tuple[date, dict[str, Decimal], Decimal]]:
    """Património por dia, usando só o último run desse dia."""
    ultimo: dict[date, int] = {}
    for run_id, at in db.execute("SELECT id, at FROM runs ORDER BY at, id"):
        ultimo[_como_data(at)] = run_id
    serie = []
    for dia in sorted(ultimo):
        por_fonte = _totais_do_run(db, ultimo[dia])
        total = sum(por_fonte.values(), start=Decimal(0))
        serie.append((dia, por_fonte, total))
    return serie


def xirr(fluxos: list[tuple[date, Decimal]]) -> Decimal | None:
    """Taxa interna anual. Dinheiro investido entra negativo e o valor final positivo.

    Resolve por Newton-Raphson e, se não convergir, por bisseção. ``None`` quando
    não há mudança de sinal ou a taxa não existe.
    """
    agregados = _agregar_fluxos(fluxos)
    if len(agregados) < 2:
        return None
    if not any(valor > 0 for _, valor in agregados) or not any(valor < 0 for _, valor in agregados):
        return None
    with localcontext(Context(prec=50)):
        taxa = _newton(agregados)
        if not _raiz_valida(agregados, taxa):
            taxa = _bissecao(agregados)
        if not _raiz_valida(agregados, taxa):
            return None
        return taxa.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def rentabilidade(db, source: str | None = None, ate: date | None = None) -> list[Rentabilidade]:
    """Depositado líquido, rendimentos, valor atual, ganho e XIRR por plataforma.

    Nos cashflows, depósito > 0 é dinheiro que entrou na plataforma. No XIRR esse
    fluxo é negativo. Dividendos e juros contam como rendimento, não como capital.
    """
    fontes = _fontes_investimento(db, source, ate)
    if source is not None:
        return [_rentabilidade_de(db, source, ate)]
    linhas = [_rentabilidade_de(db, fonte, ate) for fonte in fontes]
    linhas.append(_rentabilidade_total(db, fontes, ate))
    return linhas


def benchmark(
    db,
    source: str | None = None,
    symbol: str = "IWDA.AS",
    fx=None,
    ate: date | None = None,
    precos=None,
) -> Benchmark:
    """E se cada depósito e levantamento tivesse comprado ou vendido ``symbol``.

    Usa o preço desse dia ou, se faltar, o do dia anterior mais próximo.
    ``fx(dia, moeda)`` devolve quantos EUR vale 1 unidade de ``moeda``.
    """
    if precos is None:
        precos = _ler_precos(db, symbol)
    else:
        precos = [
            (_como_data(dia), _dec(close), (moeda or "EUR").upper()) for dia, close, moeda in precos
        ]
    cache: dict[tuple[date, str], Decimal] = {}

    def preco_eur(dia: date) -> Decimal | None:
        escolhido = None
        for cotacao_dia, close, moeda in precos:
            if cotacao_dia > dia:
                break
            escolhido = (cotacao_dia, close, moeda)
        if escolhido is None:
            return None
        cotacao_dia, close, moeda = escolhido
        if moeda == "EUR":
            return close
        if fx is None:
            return None
        chave = (cotacao_dia, moeda)
        if chave not in cache:
            taxa = fx(cotacao_dia, moeda)
            cache[chave] = taxa if isinstance(taxa, Decimal) else Decimal(str(taxa))
        return close * cache[chave]

    dia_valor = ate
    if dia_valor is None and precos:
        dia_valor = precos[-1][0]

    unidades = Decimal(0)
    depositado = Decimal(0)
    fluxos: list[tuple[date, Decimal]] = []
    for booked, amount, kind in _fluxos_capital(db, source, ate):
        preco = preco_eur(booked)
        if preco is None or preco == 0:
            continue
        unidades += amount / preco
        depositado += amount
        fluxos.append((booked, -amount))

    valor = Decimal(0)
    if dia_valor is not None:
        preco_final = preco_eur(dia_valor)
        if preco_final is not None:
            valor = unidades * preco_final
            fluxos.append((dia_valor, valor))
    return Benchmark(symbol, unidades, valor, xirr(fluxos), depositado)


def benchmarks(db, fontes, symbol: str = "IWDA.AS", fx=None, ate: date | None = None) -> list[Benchmark]:
    """Um benchmark por fonte. A tabela de preços lê-se uma vez."""
    precos = _ler_precos(db, symbol)
    return [benchmark(db, source=fonte, symbol=symbol, fx=fx, ate=ate, precos=precos) for fonte in fontes]


def _ler_precos(db, symbol: str) -> list[tuple[date, Decimal, str]]:
    return [
        (_como_data(dia), _dec(close), (moeda or "EUR").upper())
        for dia, close, moeda in db.execute(
            "SELECT day, close, currency FROM prices WHERE symbol = ? ORDER BY day",
            (symbol,),
        )
    ]


def preencher_patrimonio(serie) -> tuple[list[tuple[date, dict[str, Decimal], Decimal]], bool]:
    """Preenche cada fonte para trás com o primeiro valor e para a frente com o último.

    Devolve a série somada outra vez e se algum valor foi preenchido.
    """
    pontos = [(dia, dict(por_fonte), total) for dia, por_fonte, total in serie]
    if not pontos:
        return [], False
    fontes: list[str] = []
    vistas: set[str] = set()
    for _dia, por_fonte, _total in pontos:
        for fonte in por_fonte:
            if fonte not in vistas:
                vistas.add(fonte)
                fontes.append(fonte)
    falta = object()
    houve = False
    for fonte in fontes:
        coluna = [linha[fonte] if fonte in linha else falta for _dia, linha, _total in pontos]
        if all(valor is not falta for valor in coluna):
            continue
        houve = True
        primeiro = next(valor for valor in coluna if valor is not falta)
        ultimo = None
        for indice, valor in enumerate(coluna):
            if valor is falta:
                coluna[indice] = primeiro if ultimo is None else ultimo
            else:
                ultimo = valor
        for indice, valor in enumerate(coluna):
            pontos[indice][1][fonte] = valor
    preenchida = []
    for dia, por_fonte, _total in pontos:
        total = sum(por_fonte.values(), start=Decimal(0))
        preenchida.append((dia, por_fonte, total))
    return preenchida, houve


def rendimentos_ate(db, fonte: str | None, ate: date) -> Decimal:
    """Dividendos e juros até ao dia, sem calcular o XIRR."""
    if fonte is not None:
        return _soma_rendimentos(db, fonte, ate)
    total = Decimal(0)
    for nome in _fontes_investimento(db, None, ate):
        if not _tem_deposito(db, nome, ate):
            continue
        total += _soma_rendimentos(db, nome, ate)
    return total


def _soma_rendimentos(db, fonte: str, ate: date | None) -> Decimal:
    total = Decimal(0)
    for _booked, amount, kind in _cashflows(db, fonte, ate):
        if kind in _RENDIMENTO:
            total += amount
    return total


def serie_capital(db, fonte: str | None) -> list[tuple[date, Decimal, Decimal]]:
    """Capital investido e valor por snapshot, sem calcular o XIRR."""
    serie = patrimonio_serie(db)
    if fonte is not None:
        dias = [dia for dia, por_fonte, _total in serie if fonte in por_fonte]
        universo = [fonte]
        so_com_deposito = False
    else:
        dias = [dia for dia, _por_fonte, _total in serie]
        universo = _fontes_investimento(db, None, None)
        so_com_deposito = True
    if not dias:
        return []
    preparados = []
    for nome in universo:
        fluxos = _cashflows(db, nome, None)
        preparados.append(
            (
                [(booked, amount) for booked, amount, kind in fluxos if kind in _CAPITAL],
                [booked for booked, _amount, kind in fluxos if kind == "deposito"],
                _snapshots_valor(db, nome),
            )
        )
    pontos = []
    for dia in dias:
        depositado = Decimal(0)
        valor = Decimal(0)
        for capital, depositos, snapshots in preparados:
            if so_com_deposito and not any(booked <= dia for booked in depositos):
                continue
            depositado += sum((amount for booked, amount in capital if booked <= dia), start=Decimal(0))
            montante, _quando = _valor_ate(snapshots, dia)
            valor += montante
        pontos.append((dia, depositado, valor))
    return pontos


def alocacao(db) -> dict[str, Decimal]:
    """Último snapshot de cada fonte, partido por classe de ativo."""
    totais = {classe: Decimal(0) for classe in _CLASSES}
    for fonte, run_id in _ultimo_run_por_fonte(db).items():
        for kind, amount in db.execute(
            "SELECT kind, amount_eur FROM balances WHERE run_id = ? AND source = ?",
            (run_id, fonte),
        ):
            classe = _classe_saldo(fonte, kind)
            if classe is not None:
                totais[classe] += _dec(amount)
        for (amount,) in db.execute(
            "SELECT value_eur FROM positions WHERE run_id = ? AND source = ?",
            (run_id, fonte),
        ):
            classe = _classe_posicao(fonte)
            if classe is not None:
                totais[classe] += _dec(amount)
    return totais


def estatisticas_curiosas(db, inicio: date, fim: date, classify=None) -> list[str]:
    """Frases curtas em português, com percentagens e sem valores absolutos."""
    totais = gastos_por_categoria(db, inicio, fim, classify=classify)
    total = sum(totais.values(), start=Decimal(0))
    frases: list[str] = []
    if total == 0:
        frases.append("Não há gastos neste período.")
    else:
        categoria, valor = min(totais.items(), key=lambda item: (-item[1], item[0]))
        frases.append(f"O maior gasto foi em {categoria} ({_percentagem(valor, total)}% do período).")
        dias = gastos_por_dia_semana(db, inicio, fim, classify=classify)
        indice = min(range(len(dias)), key=lambda posicao: (-dias[posicao][1], posicao))
        if dias[indice][1] > 0:
            frases.append(_frase_dia(dias[indice][0]))
        nomes = {item.nome.casefold() for item in subscricoes(db, fim, classify=classify)}
        if nomes:
            peso = sum(
                (abs(movimento.eur) for movimento in _gastos(db, classify, inicio, fim) if movimento.comerciante.casefold() in nomes),
                start=Decimal(0),
            )
            if peso > 0:
                frases.append(f"As subscrições custam {_percentagem(peso, total)}% dos teus gastos.")
    taxa = taxa_poupanca(db, inicio, fim, classify=classify)
    if taxa is not None:
        percentagem = int((taxa * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        frases.append(f"A taxa de poupança foi de {percentagem}%.")
    return frases


def _classificador(classify):
    if classify is not None:
        return classify
    from financas.categorias import carregar_regras, classify as classificar

    regras = carregar_regras()

    def aplicar(source, account, description, amount):
        return classificar(source, account, description, amount, regras)

    return aplicar


def _movimentos(db, classify, inicio: date | None, fim: date | None) -> tuple[list[_Movimento], list[str]]:
    taxas = _taxas_cambio(db)
    classificar = _classificador(classify)
    fontes = fontes_bancarias()
    if not fontes:
        return [], []
    clausulas = [f"source IN ({','.join('?' * len(fontes))})"]
    parametros: list[str] = list(fontes)
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
    ignoradas: dict[str, int] = {}
    movimentos: list[_Movimento] = []
    for source, account, booked, amount, currency, description in db.execute(sql, parametros):
        montante = _dec(amount)
        moeda = (currency or "EUR").upper()
        em_eur = _converter(montante, moeda, taxas)
        if em_eur is None:
            ignoradas[moeda] = ignoradas.get(moeda, 0) + 1
            continue
        classe = classificar(source or "", account or "", description or "", montante)
        movimentos.append(
            _Movimento(
                _como_data(booked),
                em_eur,
                classe.tipo,
                classe.categoria,
                _normalizar_descricao(description or ""),
            )
        )
    avisos = [_aviso_moeda(moeda, ignoradas[moeda]) for moeda in sorted(ignoradas)]
    return movimentos, avisos


def _gastos(db, classify, inicio: date | None, fim: date | None) -> list[_Movimento]:
    return [movimento for movimento in _movimentos(db, classify, inicio, fim)[0] if movimento.tipo == "gasto"]


def _aviso_moeda(moeda: str, quantidade: int) -> str:
    if quantidade == 1:
        return f"Ignorada 1 transação em {moeda}: sem taxa de câmbio nos saldos."
    return f"Ignoradas {quantidade} transações em {moeda}: sem taxa de câmbio nos saldos."


def _taxas_cambio(db) -> dict[str, Decimal]:
    taxas: dict[str, Decimal] = {}
    for currency, amount, amount_eur in db.execute(
        """SELECT b.currency, b.amount, b.amount_eur
           FROM balances b JOIN runs r ON r.id = b.run_id
           ORDER BY r.at, r.id"""
    ):
        codigo = (currency or "").upper()
        if codigo in {"", "EUR"}:
            continue
        montante = _dec(amount)
        if montante == 0:
            continue
        taxas[codigo] = _dec(amount_eur) / montante
    return taxas


def _converter(montante: Decimal, moeda: str, taxas: dict[str, Decimal]) -> Decimal | None:
    if moeda == "EUR":
        return montante
    taxa = taxas.get(moeda)
    if taxa is None:
        return None
    return montante * taxa


def _normalizar_descricao(description: str) -> str:
    texto = _REF.sub(" ", description)
    texto = re.sub(r"\d+", " ", texto)
    texto = re.sub(r"[^\w\s]", " ", texto, flags=re.UNICODE)
    partes = texto.split()

    def iguais(esquerda, direita) -> bool:
        return [parte.casefold() for parte in esquerda] == [parte.casefold() for parte in direita]

    while len(partes) >= 2:
        if len({parte.casefold() for parte in partes}) == 1:
            partes = partes[:1]
            break
        if len(partes) % 2 == 0:
            meio = len(partes) // 2
            if iguais(partes[:meio], partes[meio:]):
                partes = partes[:meio]
                continue
        break
    return " ".join(partes)


def _chave_periodo(dia: date, freq: str) -> str:
    if freq == "semana":
        iso = dia.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if freq == "mes":
        return f"{dia.year:04d}-{dia.month:02d}"
    return f"{dia.year:04d}"


def _valor_compativel(valores: list[Decimal], novo: Decimal) -> bool:
    conjunto = [*valores, novo]
    menor = min(conjunto)
    maior = max(conjunto)
    return menor > 0 and maior <= menor * Decimal("1.1")


def _periodicidade_do_intervalo(dias: int, actual: str | None) -> str | None:
    if _MENSAL[0] <= dias <= _MENSAL[1] and actual in {None, "mensal"}:
        return "mensal"
    if _ANUAL[0] <= dias <= _ANUAL[1] and actual in {None, "anual"}:
        return "anual"
    return None


def _melhor_cadeia(itens: list[tuple[date, Decimal, str]]) -> tuple[list[tuple[date, Decimal, str]], str | None]:
    melhor: list[tuple[date, Decimal, str]] = []
    periodicidade = None
    for inicio in range(len(itens)):
        cadeia = [itens[inicio]]
        periodo = None
        for candidato in itens[inicio + 1 :]:
            if not _valor_compativel([valor for _, valor, _nome in cadeia], candidato[1]):
                break
            periodo_novo = _periodicidade_do_intervalo((candidato[0] - cadeia[-1][0]).days, periodo)
            if periodo_novo is None:
                break
            cadeia.append(candidato)
            periodo = periodo_novo
        if len(cadeia) >= 2 and len(cadeia) > len(melhor):
            melhor = cadeia
            periodicidade = periodo
    return melhor, periodicidade


def _somar_meses(dia: date, meses: int) -> date:
    absoluto = dia.month - 1 + meses
    return date(dia.year + absoluto // 12, absoluto % 12 + 1, 1)


def _totais_do_run(db, run_id: int) -> dict[str, Decimal]:
    por_fonte: dict[str, Decimal] = {}
    for source, amount in db.execute("SELECT source, amount_eur FROM balances WHERE run_id = ?", (run_id,)):
        por_fonte[source] = por_fonte.get(source, Decimal(0)) + _dec(amount)
    for source, amount in db.execute("SELECT source, value_eur FROM positions WHERE run_id = ?", (run_id,)):
        por_fonte[source] = por_fonte.get(source, Decimal(0)) + _dec(amount)
    return por_fonte


def _agregar_fluxos(fluxos: list[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    somas: dict[date, Decimal] = {}
    for dia, valor in fluxos:
        dia = _como_data(dia)
        somas[dia] = somas.get(dia, Decimal(0)) + _dec(valor)
    return sorted((dia, valor) for dia, valor in somas.items() if valor != 0)


def _npv(fluxos: list[tuple[date, Decimal]], taxa: Decimal) -> Decimal | None:
    base = Decimal(1) + taxa
    if base <= 0:
        return None
    origem = fluxos[0][0]
    total = Decimal(0)
    for dia, valor in fluxos:
        expoente = Decimal((dia - origem).days) / Decimal(365)
        total += valor / (base ** expoente)
    return total


def _derivada_npv(fluxos: list[tuple[date, Decimal]], taxa: Decimal) -> Decimal | None:
    base = Decimal(1) + taxa
    if base <= 0:
        return None
    origem = fluxos[0][0]
    total = Decimal(0)
    for dia, valor in fluxos:
        expoente = Decimal((dia - origem).days) / Decimal(365)
        if expoente == 0:
            continue
        total += (-expoente) * valor / (base ** (expoente + 1))
    return total


def _newton(fluxos: list[tuple[date, Decimal]]) -> Decimal | None:
    taxa = Decimal("0.1")
    for _ in range(50):
        valor = _npv(fluxos, taxa)
        derivada = _derivada_npv(fluxos, taxa)
        if valor is None or derivada is None or derivada == 0:
            return None
        seguinte = taxa - valor / derivada
        if seguinte <= Decimal("-0.999999999"):
            return None
        if abs(seguinte - taxa) < Decimal("1e-14"):
            return seguinte
        taxa = seguinte
    return None


def _bissecao(fluxos: list[tuple[date, Decimal]]) -> Decimal | None:
    baixo = Decimal("-0.999999")
    alto = Decimal("0.1")
    f_baixo = _npv(fluxos, baixo)
    f_alto = _npv(fluxos, alto)
    if f_baixo is None or f_alto is None:
        return None
    for _ in range(40):
        if f_baixo == 0:
            return baixo
        if f_alto == 0:
            return alto
        if f_baixo * f_alto < 0:
            break
        alto = alto * 2 if alto > 0 else Decimal("0.1")
        if alto > Decimal("1000000"):
            return None
        f_alto = _npv(fluxos, alto)
        if f_alto is None:
            return None
    else:
        return None
    if f_baixo * f_alto >= 0:
        return None
    for _ in range(80):
        meio = (baixo + alto) / 2
        f_meio = _npv(fluxos, meio)
        if f_meio is None:
            return None
        if abs(f_meio) < Decimal("1e-12") or alto - baixo < Decimal("1e-14"):
            return meio
        if f_baixo * f_meio <= 0:
            alto, f_alto = meio, f_meio
        else:
            baixo, f_baixo = meio, f_meio
    return (baixo + alto) / 2


def _raiz_valida(fluxos: list[tuple[date, Decimal]], taxa: Decimal | None) -> bool:
    if taxa is None or taxa <= -1:
        return False
    valor = _npv(fluxos, taxa)
    return valor is not None and abs(valor) < Decimal("1e-6")


def _componentes(db, fonte: str, ate: date | None):
    depositos = Decimal(0)
    levantamentos = Decimal(0)
    rendimentos = Decimal(0)
    tem_deposito = False
    fluxos_capital: list[tuple[date, Decimal]] = []
    for booked, amount, kind in _cashflows(db, fonte, ate):
        if kind == "deposito":
            tem_deposito = True
            depositos += amount
            fluxos_capital.append((booked, -amount))
        elif kind == "levantamento":
            levantamentos += -amount
            fluxos_capital.append((booked, -amount))
        elif kind in _RENDIMENTO:
            rendimentos += amount
    valor, dia_valor = _valor_da_fonte(db, fonte, ate)
    return depositos, levantamentos, rendimentos, valor, dia_valor, fluxos_capital, tem_deposito


def _rentabilidade_de(db, fonte: str, ate: date | None) -> Rentabilidade:
    depositos, levantamentos, rendimentos, valor, dia_valor, fluxos, tem_deposito = _componentes(db, fonte, ate)
    if not tem_deposito:
        depositado = depositos - levantamentos
        return Rentabilidade(
            fonte,
            depositado,
            rendimentos,
            valor,
            valor - depositado,
            None,
            None,
            "sem dados de depósitos",
        )
    return _montar_rentabilidade(fonte, depositos, levantamentos, rendimentos, valor, dia_valor, fluxos, "")


def _rentabilidade_total(db, fontes: list[str], ate: date | None) -> Rentabilidade:
    depositos = Decimal(0)
    levantamentos = Decimal(0)
    rendimentos = Decimal(0)
    valor = Decimal(0)
    dia_valor = None
    fluxos: list[tuple[date, Decimal]] = []
    incluidas: list[str] = []
    for fonte in fontes:
        dep, lev, rend, val, dia, flux, tem_deposito = _componentes(db, fonte, ate)
        if not tem_deposito:
            continue
        incluidas.append(fonte)
        depositos += dep
        levantamentos += lev
        rendimentos += rend
        valor += val
        if dia is not None and (dia_valor is None or dia > dia_valor):
            dia_valor = dia
        fluxos.extend(flux)
    nota = _nota_total(incluidas)
    if not incluidas:
        return Rentabilidade("total", Decimal(0), Decimal(0), Decimal(0), Decimal(0), None, None, nota)
    return _montar_rentabilidade(
        "total", depositos, levantamentos, rendimentos, valor, dia_valor, fluxos, nota
    )


def _nota_total(fontes: list[str]) -> str:
    from financas.report import display_name, ordered_sources

    nomes = [display_name(fonte) for fonte in ordered_sources(fontes)]
    if not nomes:
        return "Sem plataformas com depósitos."
    if len(nomes) == 1:
        return f"Inclui {nomes[0]}."
    return f"Inclui {', '.join(nomes[:-1])} e {nomes[-1]}."


def _montar_rentabilidade(
    fonte, depositos, levantamentos, rendimentos, valor, dia_valor, fluxos_capital, nota
) -> Rentabilidade:
    # Ganho = valor atual + levantamentos − depósitos. Os juros capitalizados
    # (por exemplo nos Certificados de Aforro) já estão dentro do valor atual.
    depositado = depositos - levantamentos
    ganho = valor + levantamentos - depositos
    ganho_pct = None if depositado == 0 else ganho / depositado
    fluxos = list(fluxos_capital)
    if dia_valor is not None:
        fluxos.append((dia_valor, valor))
    return Rentabilidade(fonte, depositado, rendimentos, valor, ganho, ganho_pct, xirr(fluxos), nota)


def _cashflows(db, fonte: str | None, ate: date | None) -> list[tuple[date, Decimal, str]]:
    clausulas = []
    parametros: list[str] = []
    if fonte is not None:
        clausulas.append("source = ?")
        parametros.append(fonte)
    if ate is not None:
        clausulas.append("booked <= ?")
        parametros.append(ate.isoformat())
    sql = "SELECT booked, amount_eur, kind FROM cashflows"
    if clausulas:
        sql += " WHERE " + " AND ".join(clausulas)
    sql += " ORDER BY booked, ext_id"
    return [(_como_data(booked), _dec(amount), kind) for booked, amount, kind in db.execute(sql, parametros)]


def _fluxos_capital(db, fonte: str | None, ate: date | None) -> list[tuple[date, Decimal, str]]:
    if fonte is None:
        fluxos = []
        for nome in _fontes_investimento(db, None, ate):
            if not _tem_deposito(db, nome, ate):
                continue
            fluxos.extend(item for item in _cashflows(db, nome, ate) if item[2] in _CAPITAL)
        fluxos.sort(key=lambda item: (item[0],))
        return fluxos
    return [fluxo for fluxo in _cashflows(db, fonte, ate) if fluxo[2] in _CAPITAL]


def _tem_deposito(db, fonte: str, ate: date | None) -> bool:
    clausulas = ["source = ?", "kind = 'deposito'"]
    parametros: list[str] = [fonte]
    if ate is not None:
        clausulas.append("booked <= ?")
        parametros.append(ate.isoformat())
    linha = db.execute(
        "SELECT 1 FROM cashflows WHERE " + " AND ".join(clausulas) + " LIMIT 1",
        parametros,
    ).fetchone()
    return linha is not None


def _valor_da_fonte(db, fonte: str, ate: date | None) -> tuple[Decimal, date | None]:
    return _valor_ate(_snapshots_valor(db, fonte), ate)


def _snapshots_valor(db, fonte: str) -> list[tuple[date, Decimal]]:
    runs: dict[int, date] = {}
    totais: dict[int, Decimal] = {}
    consultas = (
        """SELECT r.id, r.at, b.amount_eur FROM balances b JOIN runs r ON r.id = b.run_id
           WHERE b.source = ?""",
        """SELECT r.id, r.at, p.value_eur FROM positions p JOIN runs r ON r.id = p.run_id
           WHERE p.source = ?""",
    )
    for sql in consultas:
        for run_id, at, amount in db.execute(sql, (fonte,)):
            runs[run_id] = _como_data(at)
            totais[run_id] = totais.get(run_id, Decimal(0)) + _dec(amount)
    por_dia: dict[date, tuple[int, Decimal]] = {}
    for run_id, total in totais.items():
        dia = runs[run_id]
        actual = por_dia.get(dia)
        if actual is None or run_id >= actual[0]:
            por_dia[dia] = (run_id, total)
    return sorted((dia, total) for dia, (_run, total) in por_dia.items())


def _valor_ate(snapshots, dia: date | None) -> tuple[Decimal, date | None]:
    escolhido = None
    for quando, total in snapshots:
        if dia is not None and quando > dia:
            break
        escolhido = (quando, total)
    if escolhido is None:
        return Decimal(0), None
    return escolhido[1], escolhido[0]


def _fontes_investimento(db, source: str | None, ate: date | None) -> list[str]:
    if source is not None:
        return [source]
    permitidas = set(fontes_investimento())
    fontes = set()
    for (fonte,) in db.execute("SELECT DISTINCT source FROM cashflows"):
        fontes.add(fonte)
    for fonte in _ultimo_run_por_fonte(db, ate):
        fontes.add(fonte)
    return sorted(fonte for fonte in fontes if fonte in permitidas)


def _ultimo_run_por_fonte(db, ate: date | None = None) -> dict[str, int]:
    candidatos: dict[str, tuple[date, int]] = {}
    consultas = (
        """SELECT b.source, r.id, r.at FROM balances b JOIN runs r ON r.id = b.run_id""",
        """SELECT p.source, r.id, r.at FROM positions p JOIN runs r ON r.id = p.run_id""",
    )
    for sql in consultas:
        for fonte, run_id, at in db.execute(sql):
            dia = _como_data(at)
            if ate is not None and dia > ate:
                continue
            actual = candidatos.get(fonte)
            if actual is None or (dia, run_id) >= actual:
                candidatos[fonte] = (dia, run_id)
    return {fonte: run_id for fonte, (_dia, run_id) in candidatos.items()}


def _classe_saldo(fonte: str, kind: str | None) -> str | None:
    if fonte == "aforro":
        return "Dívida pública"
    if fonte == "bitcoin" or kind == "crypto":
        return "Bitcoin"
    if kind == "cash":
        return "Cash"
    return None


def _classe_posicao(fonte: str) -> str | None:
    if fonte in _ACOES:
        return "Ações/ETFs"
    if fonte == "bitcoin":
        return "Bitcoin"
    if fonte == "aforro":
        return "Dívida pública"
    return None


def _frase_dia(dia: str) -> str:
    artigo = "ao" if dia in {"sábado", "domingo"} else "à"
    return f"Gastas mais {artigo} {dia}."


def _percentagem(parte: Decimal, total: Decimal) -> int:
    return int((parte * Decimal(100) / total).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def _como_data(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
