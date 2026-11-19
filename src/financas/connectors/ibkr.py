"""Interactive Brokers Flex Web Service v3 (só leitura)."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import httpx

from financas import config, secrets
from financas.models import Balance, Cashflow, FetchResult, NeedsUserAction, Position, Transaction

NAME = "ibkr"
SECRETS = {"ibkr_flex_token": "Token do Flex Web Service do IBKR"}

_sleep = time.sleep

_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_SEND = f"{_BASE}/SendRequest"
_STATEMENT = f"{_BASE}/GetStatement"
_USER_AGENT = "financas/0.1"

_USER_ACTION = {
    "1012": "O token Flex do IBKR expirou. Gere um novo token e guarde-o outra vez.",
    "1015": "O token Flex do IBKR é inválido.",
    "1013": "O IBKR recusou o pedido por restrição de IP.",
    "1014": "A consulta Flex do IBKR é inválida. Confirme o query id.",
}
_TRANSIENT = {"1018", "1001", "1004", "1009", "1021"}

_last_mono = 0.0


def setup() -> None:
    query_id = input("ID da consulta Flex (query id): ").strip()
    if not query_id:
        raise SystemExit("vazio, nada guardado")
    config.update_section(NAME, {"query_id": query_id})
    print("query id guardado na configuração")


def _pace() -> None:
    """No máximo um pedido por segundo."""
    global _last_mono
    now = time.monotonic()
    if _last_mono:
        gap = 1.0 - (now - _last_mono)
        if gap > 0:
            _sleep(gap)
            now = time.monotonic()
    _last_mono = now


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _named(parent, name: str):
    for el in parent.iter():
        if _local(el.tag) == name:
            yield el


def _plain(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _parse_date(raw: str) -> date:
    head = raw.strip().split(";", 1)[0].split(",", 1)[0].strip()
    if len(head) >= 10 and head[4] == "-" and head[7] == "-":
        return date.fromisoformat(head[:10])
    if len(head) < 8 or not head[:8].isdigit():
        raise ValueError(f"Data inválida: {raw}")
    return date(int(head[0:4]), int(head[4:6]), int(head[6:8]))


def _classify(text: str):
    try:
        root = ET.fromstring(text.lstrip("\ufeff").strip())
    except ET.ParseError:
        raise NeedsUserAction("O IBKR devolveu uma resposta XML inválida.") from None
    tag = _local(root.tag)
    if tag == "FlexQueryResponse":
        return "statement", root
    if tag == "FlexStatementResponse":
        code = (root.findtext("ErrorCode") or "").strip()
        if code:
            message = (root.findtext("ErrorMessage") or "").strip()
            return "error", (code, message)
        reference = (root.findtext("ReferenceCode") or "").strip()
        if reference:
            return "reference", reference
    raise NeedsUserAction("A resposta do IBKR não foi reconhecida.")


def _get(client: httpx.Client, url: str, token: str, query: str, extra: dict | None = None):
    _pace()
    params = {"t": token, "q": query, "v": "3"}
    if extra:
        params.update(extra)
    response = client.get(
        url,
        params=params,
        headers={"User-Agent": _USER_AGENT},
    )
    if response.status_code >= 400:
        raise NeedsUserAction(f"O IBKR respondeu com o erro HTTP {response.status_code}.")
    return _classify(response.text)


def _raise_for_code(code: str) -> None:
    if code in _USER_ACTION:
        raise NeedsUserAction(_USER_ACTION[code])
    raise NeedsUserAction(
        f"O IBKR devolveu o erro {code}. Confirme o token e a consulta Flex."
    )


def _reference(client: httpx.Client, token: str, query_id: str, extra: dict | None = None) -> str:
    for attempt in range(6):
        kind, payload = _get(client, _SEND, token, query_id, extra)
        if kind == "reference":
            return payload
        if kind == "statement":
            raise NeedsUserAction("O IBKR devolveu o extrato em vez do código de referência.")
        code, _message = payload
        if code in _TRANSIENT and attempt < 5:
            _sleep(2)
            continue
        _raise_for_code(code)
    raise NeedsUserAction("O IBKR não aceitou o pedido Flex. Tente outra vez daqui a uns minutos.")


def _statement(client: httpx.Client, token: str, reference: str):
    waited = 0
    transient = 0
    while True:
        kind, payload = _get(client, _STATEMENT, token, reference)
        if kind == "statement":
            return payload
        if kind == "reference":
            raise NeedsUserAction("O IBKR ainda não devolveu o extrato Flex.")
        code, _message = payload
        if code == "1019":
            if waited >= 120:
                raise NeedsUserAction(
                    "O extrato Flex do IBKR ainda está a ser gerado. "
                    "Tente outra vez daqui a uns minutos."
                )
            _sleep(5)
            waited += 5
            continue
        if code in _TRANSIENT and transient < 5:
            transient += 1
            _sleep(2)
            continue
        _raise_for_code(code)


def _parse(root, since: date) -> FetchResult:
    balances: list[Balance] = []
    positions: list[Position] = []
    transactions: list[Transaction] = []
    cashflows: list[Cashflow] = []
    warnings: list[str] = []
    rates: dict = {}
    statements = [el for el in root.iter() if _local(el.tag) == "FlexStatement"]
    if not statements:
        warnings.append("O extrato Flex não contém contas.")

    for stmt in statements:
        account = (stmt.attrib.get("accountId") or "IBKR").strip() or "IBKR"
        _positions(stmt, account, positions, warnings)
        _cash(stmt, account, balances)
        _trades(stmt, account, since, transactions, warnings)
        _cash_txs(stmt, account, since, transactions, cashflows, warnings, rates)

    transactions.sort(key=lambda item: (item.booked, item.ext_id))
    cashflows.sort(key=lambda item: (item.booked, item.ext_id))
    return FetchResult(
        source=NAME,
        balances=balances,
        positions=positions,
        transactions=transactions,
        warnings=warnings,
        cashflows=cashflows,
    )


def _positions(stmt, account: str, positions: list[Position], warnings: list[str]) -> None:
    for el in _named(stmt, "OpenPosition"):
        level = (el.attrib.get("levelOfDetail") or "").strip()
        if level and level.upper() != "SUMMARY":
            continue
        symbol = (el.attrib.get("symbol") or "").strip()
        quantity = el.attrib.get("position")
        value = el.attrib.get("positionValue")
        currency = (el.attrib.get("currency") or "").strip()
        if not symbol or quantity is None or value is None or not currency:
            warnings.append("Posição Flex ignorada por faltarem campos.")
            continue
        cost_raw = el.attrib.get("costBasisMoney")
        positions.append(
            Position(
                source=NAME,
                account=account,
                symbol=symbol,
                name=(el.attrib.get("description") or symbol).strip(),
                quantity=Decimal(quantity),
                value=Decimal(value),
                currency=currency,
                cost_basis=Decimal(cost_raw) if cost_raw not in (None, "") else None,
            )
        )


def _cash(stmt, account: str, balances: list[Balance]) -> None:
    for el in _named(stmt, "CashReportCurrency"):
        currency = (el.attrib.get("currency") or "").strip()
        if not currency or currency.upper() == "BASE_SUMMARY":
            continue
        ending = el.attrib.get("endingCash")
        if ending is None or ending == "":
            continue
        balances.append(
            Balance(
                source=NAME,
                account=account,
                currency=currency,
                amount=Decimal(ending),
                kind="cash",
            )
        )


def _trades(stmt, account: str, since: date, transactions: list[Transaction], warnings: list[str]) -> None:
    for el in _named(stmt, "Trade"):
        ext_id = (el.attrib.get("tradeID") or "").strip()
        raw_date = el.attrib.get("tradeDate")
        net = el.attrib.get("netCash")
        currency = (el.attrib.get("currency") or "").strip()
        if not ext_id or not raw_date or net is None or not currency:
            warnings.append("Negócio Flex ignorado por faltarem campos.")
            continue
        try:
            booked = _parse_date(raw_date)
        except ValueError:
            warnings.append("Negócio Flex ignorado por data inválida.")
            continue
        if booked < since:
            continue
        side = (el.attrib.get("buySell") or "").strip().upper()
        symbol = (el.attrib.get("symbol") or "").strip()
        quantity = _plain(Decimal(el.attrib.get("quantity") or "0"))
        price = _plain(Decimal(el.attrib.get("tradePrice") or "0"))
        transactions.append(
            Transaction(
                source=NAME,
                account=account,
                booked=booked,
                amount=Decimal(net),
                currency=currency,
                description=f"{side} {quantity} {symbol} @ {price}",
                ext_id=ext_id,
            )
        )


def _cash_txs(
    stmt,
    account: str,
    since: date,
    transactions: list[Transaction],
    cashflows: list[Cashflow],
    warnings: list[str],
    rates: dict,
) -> None:
    for el in _named(stmt, "CashTransaction"):
        ext_id = (el.attrib.get("transactionID") or "").strip()
        raw_date = el.attrib.get("dateTime") or el.attrib.get("settleDate")
        amount = el.attrib.get("amount")
        currency = (el.attrib.get("currency") or "").strip()
        if not ext_id or not raw_date or amount is None or not currency:
            warnings.append("Movimento de caixa Flex ignorado por faltarem campos.")
            continue
        try:
            booked = _parse_date(raw_date)
        except ValueError:
            warnings.append("Movimento de caixa Flex ignorado por data inválida.")
            continue
        if booked < since:
            continue
        description = (el.attrib.get("description") or el.attrib.get("type") or "Movimento de caixa").strip()
        value = Decimal(amount)
        transactions.append(
            Transaction(
                source=NAME,
                account=account,
                booked=booked,
                amount=value,
                currency=currency,
                description=description,
                ext_id=ext_id,
            )
        )
        kind = _cashflow_kind(el.attrib.get("type") or "", value)
        if kind is None or value == 0:
            continue
        eur = _em_eur(value, currency, _fx_rate(el, stmt), rates, warnings)
        if eur is None:
            continue
        cashflows.append(
            Cashflow(
                source=NAME,
                booked=booked,
                amount_eur=_com_sinal(kind, eur),
                kind=kind,
                description=description,
                ext_id=ext_id,
            )
        )


def _fx_rate(el, stmt) -> Decimal | None:
    for node in (el, stmt):
        for key, raw in node.attrib.items():
            if key.casefold() == "fxratetobase" and raw not in (None, ""):
                return Decimal(raw)
    return None


def _cashflow_kind(type_name: str, amount: Decimal) -> str | None:
    text = " ".join(type_name.replace("_", " ").replace("-", " ").casefold().split())
    if "payment in lieu" in text or "dividend" in text:
        return "dividendo"
    if "broker interest" in text:
        return "juros"
    if "withholding" in text or "fee" in text:
        return "custo"
    deposit = "deposit" in text
    withdraw = "withdraw" in text
    if deposit and withdraw:
        if amount > 0:
            return "deposito"
        if amount < 0:
            return "levantamento"
        return None
    if withdraw:
        return "levantamento"
    if deposit:
        return "deposito"
    return None


def _com_sinal(kind: str, amount: Decimal) -> Decimal:
    magnitude = abs(amount)
    if kind in {"levantamento", "custo"}:
        return -magnitude
    return magnitude


def _em_eur(amount: Decimal, currency: str, rate: Decimal | None, box: dict, warnings: list[str]) -> Decimal | None:
    code = currency.upper()
    if code == "EUR":
        return amount
    if rate is not None:
        return amount * rate
    rates = _lazy_rates(box)
    if rates is None:
        _avisar_cambio(box, warnings)
        return None
    try:
        return rates.to_eur(amount, code)
    except Exception:
        _avisar_cambio(box, warnings)
        return None


def _avisar_cambio(box: dict, warnings: list[str]) -> None:
    if box.get("warned"):
        return
    box["warned"] = True
    warnings.append("Não foi possível converter um movimento do IBKR para euros.")


def _lazy_rates(box: dict):
    if "rates" in box or box.get("failed"):
        return box.get("rates")
    try:
        from financas.fx import Rates

        box["rates"] = Rates.fetch()
    except Exception:
        box["failed"] = True
        box["rates"] = None
    return box.get("rates")


def _has_activity(root) -> bool:
    for el in root.iter():
        if _local(el.tag) in {"Trade", "CashTransaction"}:
            return True
    return False


def _years_back(desde: date, today: date):
    """Um intervalo por ano civil, do mais recente para o mais antigo."""
    if today < desde:
        return
    year = today.year
    while year >= desde.year:
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        if start < desde:
            start = desde
        if end > today:
            end = today
        if start <= end:
            yield start, end
        year -= 1


def _period_params(fd: date | None, td: date | None) -> dict | None:
    extra: dict[str, str] = {}
    if fd is not None:
        extra["fd"] = fd.strftime("%Y%m%d")
    if td is not None:
        extra["td"] = td.strftime("%Y%m%d")
    return extra or None


def _download(since: date, fd: date | None, td: date | None) -> tuple[FetchResult, bool]:
    token = secrets.get("ibkr_flex_token")
    if not token or not token.strip():
        raise NeedsUserAction("Falta o token do Flex Web Service do IBKR.")
    query_id = str(config.section(NAME).get("query_id") or "").strip()
    if not query_id:
        raise NeedsUserAction("Falta o ID da consulta Flex do IBKR. Confirme o query id.")

    with httpx.Client(timeout=30, headers={"User-Agent": _USER_AGENT}) as client:
        reference = _reference(client, token.strip(), query_id, _period_params(fd, td))
        root = _statement(client, token.strip(), reference)
    return _parse(root, since), _has_activity(root)


def fetch(since: date, *, fd: date | None = None, td: date | None = None) -> FetchResult:
    global _last_mono
    _last_mono = 0.0
    result, _active = _download(since, fd, td)
    return result


def fetch_history(since: date, today: date | None = None) -> FetchResult:
    """Um pedido Flex por ano (fd/td), do presente para trás, até um ano vazio."""
    global _last_mono
    _last_mono = 0.0
    today = today or date.today()
    merged = FetchResult(source=NAME)
    for start, end in _years_back(since, today):
        part, active = _download(start, start, end)
        if not active:
            break
        merged.transactions.extend(part.transactions)
        merged.cashflows.extend(part.cashflows)
        merged.warnings.extend(part.warnings)
    merged.transactions.sort(key=lambda item: (item.booked, item.ext_id))
    merged.cashflows.sort(key=lambda item: (item.booked, item.ext_id))
    return merged
