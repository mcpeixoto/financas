"""Classificação de movimentos bancários por tipo e categoria."""
from __future__ import annotations

import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from importlib.resources import files

from financas import config

TIPOS = frozenset({"gasto", "receita", "interna", "investimento"})
CATEGORIA_INTERNA = "Transferência interna"


@dataclass(frozen=True)
class Classe:
    tipo: str
    categoria: str


@dataclass(frozen=True)
class Regra:
    categoria: str
    tipo: str
    padrao: re.Pattern[str]


@dataclass(frozen=True)
class Regras:
    pessoais: tuple[Regra, ...]
    genericas: tuple[Regra, ...]
    contas_proprias: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resumo:
    percentagens: tuple[tuple[str, Decimal], ...]
    outros: tuple[tuple[str, int], ...]


def _compilar(categoria: str, tipo: str, padrao: str) -> Regra:
    if tipo not in TIPOS:
        raise ValueError(f"tipo desconhecido em {categoria!r}: {tipo}")
    try:
        compilado = re.compile(padrao, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"padrão inválido em {categoria!r}: {padrao}") from exc
    return Regra(categoria, tipo, compilado)


def _ler_regras(documento: dict, *, contas: bool) -> tuple[tuple[Regra, ...], tuple[str, ...]]:
    regras = tuple(
        _compilar(str(item["categoria"]), str(item["tipo"]), str(item["padrao"]))
        for item in documento.get("regra") or []
    )
    if not contas:
        return regras, ()
    proprias = tuple(
        str(conta).strip()
        for conta in documento.get("contas_proprias") or []
        if str(conta).strip()
    )
    return regras, proprias


def _genericas() -> tuple[Regra, ...]:
    texto = files("financas.regras").joinpath("categorias.toml").read_text(encoding="utf-8")
    regras, _contas = _ler_regras(tomllib.loads(texto), contas=False)
    return regras


def carregar_regras() -> Regras:
    """Regras genéricas do pacote, mais data/categorias.toml se existir."""
    genericas = _genericas()
    caminho = config.DATA / "categorias.toml"
    if not caminho.is_file():
        return Regras((), genericas, ())
    documento = tomllib.loads(caminho.read_text(encoding="utf-8"))
    pessoais, contas = _ler_regras(documento, contas=True)
    return Regras(pessoais, genericas, contas)


def _primeira(descricao: str, regras: tuple[Regra, ...]) -> Classe | None:
    for regra in regras:
        if regra.padrao.search(descricao):
            return Classe(regra.tipo, regra.categoria)
    return None


def _conta_propria(descricao: str, contas: tuple[str, ...]) -> bool:
    texto = descricao.casefold()
    return any(conta.casefold() in texto for conta in contas)


def classify(
    source: str,
    account: str,
    description: str,
    amount: Decimal,
    regras: Regras | None = None,
) -> Classe:
    """Primeira regra que bate: pessoais, conta própria, genéricas."""
    regras = carregar_regras() if regras is None else regras
    texto = description or ""
    encontrada = _primeira(texto, regras.pessoais)
    if encontrada is not None:
        return encontrada
    if _conta_propria(texto, regras.contas_proprias):
        return Classe("interna", CATEGORIA_INTERNA)
    encontrada = _primeira(texto, regras.genericas)
    if encontrada is not None:
        return encontrada
    tipo = "gasto" if amount < 0 else "receita"
    return Classe(tipo, "Outros")


def _fmt_pct(percentagem: Decimal) -> str:
    texto = f"{percentagem.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}"
    return texto.replace(".", ",") + "%"


def resumir(db, desde: date | None = None, regras: Regras | None = None) -> Resumo:
    """Percentagens só de gastos, e descrições ainda em Outros."""
    regras = carregar_regras() if regras is None else regras
    if desde is None:
        linhas = db.execute("SELECT source, account, description, amount FROM transactions")
    else:
        linhas = db.execute(
            "SELECT source, account, description, amount FROM transactions WHERE booked >= ?",
            (desde.isoformat(),),
        )
    from financas.analise import fontes_bancarias

    bancarias = set(fontes_bancarias())
    totais: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    outros: Counter[str] = Counter()
    for source, account, description, amount in linhas:
        if source not in bancarias:
            continue
        valor = Decimal(amount)
        classe = classify(source, account or "", description or "", valor, regras)
        if classe.categoria == "Outros":
            outros[description or ""] += 1
        if classe.tipo == "gasto":
            totais[classe.categoria] += abs(valor)
    total = sum(totais.values(), start=Decimal(0))
    if total == 0:
        percentagens: tuple[tuple[str, Decimal], ...] = ()
    else:
        ordenadas = sorted(totais.items(), key=lambda item: (-item[1], item[0]))
        percentagens = tuple(
            (categoria, parte / total * Decimal(100)) for categoria, parte in ordenadas
        )
    lista_outros = tuple(sorted(outros.items(), key=lambda item: (-item[1], item[0])))
    return Resumo(percentagens, lista_outros)


def escrever_resumo(resumo: Resumo, sem_categoria: bool) -> None:
    if not resumo.percentagens:
        print("Sem gastos.")
    else:
        for categoria, percentagem in resumo.percentagens:
            print(f"{categoria} → {_fmt_pct(percentagem)}")
    if sem_categoria:
        print("Sem categoria:")
        for descricao, contagem in resumo.outros:
            print(f"{contagem}  {descricao}")
