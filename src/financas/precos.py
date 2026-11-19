"""Preços diários de referência: IWDA.AS (Yahoo), IWDA.UK (Stooq) e Bitcoin em euros."""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO

import httpx

from financas import store

STOOQ = "https://stooq.com/q/d/l/?s={symbol}&i=d"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
MEMPOOL = "https://mempool.space/api/v1/historical-price?currency=EUR&timestamp={timestamp}"
_USER_AGENT = "financas/0.1"
_YAHOO_AGENT = "Mozilla/5.0"
_YAHOO_DESDE = int(datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp())


def atualizar_benchmark(db, symbol: str = "IWDA.UK") -> int:
    """Descarrega o fecho diário e grava-o.

    IWDA.AS vem da Yahoo Finance, em EUR (adjclose, ou close se faltar).
    O símbolo por omissão continua IWDA.UK na Stooq, em USD: há testes que
    chamam esta função sem símbolo e esperam esse CSV.
    """
    if symbol.upper() == "IWDA.AS":
        return _atualizar_yahoo(db, symbol)
    text = _http_get(STOOQ.format(symbol=symbol.lower()))
    # iwda.uk está cotado em dólares na Stooq.
    return store.save_prices(db, symbol, "USD", _parse_stooq(text))


def _atualizar_yahoo(db, symbol: str) -> int:
    agora = int(datetime.now(timezone.utc).timestamp())
    url = f"{YAHOO.format(symbol=symbol)}?period1={_YAHOO_DESDE}&period2={agora}&interval=1d"
    rows, currency = _parse_yahoo(_http_get(url, user_agent=_YAHOO_AGENT))
    return store.save_prices(db, symbol, currency, rows)


def btc_eur_diario(db, desde: date) -> int:
    """Preço BTC/EUR ao meio-dia UTC, um dia de cada vez. Dias já gravados não se repetem."""
    ate = date.today()
    if ate < desde:
        return 0
    have = {row[0] for row in db.execute("SELECT day FROM prices WHERE symbol=?", ("BTC",))}
    rows: dict[date, Decimal] = {}
    day = desde
    while day <= ate:
        if day.isoformat() not in have:
            try:
                rows[day] = _btc_no_dia(day)
            except Exception:
                if rows:
                    store.save_prices(db, "BTC", "EUR", rows)
                raise
        day += timedelta(days=1)
    return store.save_prices(db, "BTC", "EUR", rows)


def eur_historico(payload: dict, timestamp: int | None = None) -> Decimal | None:
    """Escolhe o preço em EUR mais próximo do instante pedido."""
    prices = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(prices, list):
        return None
    best = None
    best_dist: int | None = None
    for item in prices:
        if not isinstance(item, dict) or "EUR" not in item:
            continue
        if timestamp is None:
            return _dec(item["EUR"])
        try:
            dist = abs(int(item.get("time") or 0) - timestamp)
        except (TypeError, ValueError):
            continue
        if best is None or best_dist is None or dist < best_dist:
            best = item
            best_dist = dist
    if best is None:
        return None
    return _dec(best["EUR"])


def _btc_no_dia(day: date) -> Decimal:
    instant = datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)
    timestamp = int(instant.timestamp())
    text = _http_get(MEMPOOL.format(timestamp=timestamp))
    price = eur_historico(json.loads(text, parse_float=Decimal), timestamp)
    if price is None:
        raise ValueError("A mempool não devolveu o preço do Bitcoin em euros.")
    return price


def _parse_stooq(text: str) -> dict[date, Decimal]:
    sample = text.lstrip("\ufeff").strip()
    if not sample or not sample.splitlines()[0].lower().startswith("date"):
        raise ValueError("A resposta da Stooq não é um CSV de cotações.")
    reader = csv.DictReader(StringIO(sample))
    if not reader.fieldnames:
        raise ValueError("A resposta da Stooq não é um CSV de cotações.")
    fields = {name.casefold(): name for name in reader.fieldnames if name}
    day_key = fields.get("date")
    close_key = fields.get("close")
    if day_key is None or close_key is None:
        raise ValueError("A resposta da Stooq não é um CSV de cotações.")
    rows: dict[date, Decimal] = {}
    for raw in reader:
        try:
            booked = date.fromisoformat((raw.get(day_key) or "")[:10])
            close = Decimal((raw.get(close_key) or "").strip())
        except (ValueError, ArithmeticError):
            continue
        rows[booked] = close
    return rows


def _parse_yahoo(text: str) -> tuple[dict[date, Decimal], str]:
    try:
        payload = json.loads(text, parse_float=Decimal)
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("A resposta da Yahoo Finance não tem cotações.") from exc
    if not isinstance(result, dict):
        raise ValueError("A resposta da Yahoo Finance não tem cotações.")
    serie = _serie_yahoo(result.get("indicators") or {})
    moeda = str((result.get("meta") or {}).get("currency") or "EUR")
    rows: dict[date, Decimal] = {}
    for raw_ts, raw_close in zip(result.get("timestamp") or [], serie):
        if raw_ts is None or raw_close is None:
            continue
        try:
            dia = datetime.fromtimestamp(int(raw_ts), timezone.utc).date()
            close = raw_close if isinstance(raw_close, Decimal) else Decimal(str(raw_close))
        except (TypeError, ValueError, ArithmeticError, OSError, OverflowError):
            continue
        rows[dia] = close
    return rows, moeda


def _serie_yahoo(indicadores: dict) -> list:
    adj = indicadores.get("adjclose") if isinstance(indicadores, dict) else None
    if isinstance(adj, list) and adj and isinstance(adj[0], dict) and adj[0].get("adjclose") is not None:
        return list(adj[0]["adjclose"])
    quote = indicadores.get("quote") if isinstance(indicadores, dict) else None
    if isinstance(quote, list) and quote and isinstance(quote[0], dict) and quote[0].get("close") is not None:
        return list(quote[0]["close"])
    raise ValueError("A resposta da Yahoo Finance não tem cotações.")


def _http_get(url: str, user_agent: str | None = None) -> str:
    _recusar_rede_solta_nos_testes()
    with httpx.Client(timeout=30, headers={"User-Agent": user_agent or _USER_AGENT}) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _recusar_rede_solta_nos_testes() -> None:
    """Os testes não falam com a rede, salvo quando o respx está a simular o pedido."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        from respx.mocks import Mocker
    except ImportError as exc:
        raise ConnectionError("sem rede nos testes") from exc
    if not any(mocker.routers for mocker in Mocker.registry.values()):
        raise ConnectionError("sem rede nos testes")


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("preço tem de ser Decimal, int ou str")
    return Decimal(value)
