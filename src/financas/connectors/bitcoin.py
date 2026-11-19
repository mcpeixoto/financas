"""Saldos e movimentos de uma carteira Bitcoin (zpub BIP84, só leitura via mempool)."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
from embit import bip32
from embit.networks import NETWORKS
from embit.script import p2wpkh

from financas import secrets
from financas.models import Balance, FetchResult, NeedsUserAction, Transaction

NAME = "bitcoin"
SECRETS = {"btc_zpub": "Chave pública mestra (zpub) do Electrum"}

_sleep = time.sleep

_API = "https://mempool.space/api/address"
_GAP = 20
_SATS = Decimal(100_000_000)


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("valor em sats tem de ser Decimal, int ou str")
    return Decimal(value)


def _get_json(client: httpx.Client, url: str):
    delay = 1.0
    for _attempt in range(6):
        try:
            response = client.get(url)
        except httpx.TransportError:
            _sleep(delay)
            delay *= 2
            continue
        if response.status_code in (429, 502, 503, 504):
            _sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        _sleep(0.3)
        return json.loads(response.text, parse_float=Decimal)
    raise NeedsUserAction(
        "O mempool.space limitou os pedidos. Tente outra vez daqui a uns minutos."
    )


def _address(master, chain: int, index: int) -> str:
    child = master.derive([chain, index])
    return p2wpkh(child.key).address(NETWORKS["main"])


def _tx_count(info: dict) -> int:
    total = 0
    for key in ("chain_stats", "mempool_stats"):
        stats = info.get(key) or {}
        total += int(stats.get("tx_count") or 0)
    return total


def _funded_minus_spent(info: dict) -> Decimal:
    net = Decimal(0)
    for key in ("chain_stats", "mempool_stats"):
        stats = info.get(key) or {}
        net += _dec(stats.get("funded_txo_sum") or 0)
        net -= _dec(stats.get("spent_txo_sum") or 0)
    return net


def _net_sats(tx: dict, own: set[str]) -> Decimal:
    incoming = Decimal(0)
    outgoing = Decimal(0)
    for vout in tx.get("vout") or []:
        if vout.get("scriptpubkey_address") in own:
            incoming += _dec(vout.get("value") or 0)
    for vin in tx.get("vin") or []:
        prev = vin.get("prevout") or {}
        if prev.get("scriptpubkey_address") in own:
            outgoing += _dec(prev.get("value") or 0)
    return incoming - outgoing


def _booked(tx: dict) -> date | None:
    raw = (tx.get("status") or {}).get("block_time")
    if raw is None:
        return None
    return datetime.fromtimestamp(int(raw), timezone.utc).date()


def fetch(since: date) -> FetchResult:
    zpub = secrets.get("btc_zpub")
    if not zpub or not zpub.strip():
        raise NeedsUserAction("Falta a chave pública mestra (zpub) do Electrum.")
    master = bip32.HDKey.from_string(zpub.strip())

    scanned: list[tuple[str, dict]] = []
    with httpx.Client(timeout=30) as client:
        for chain in (0, 1):
            gap = 0
            index = 0
            while gap < _GAP:
                addr = _address(master, chain, index)
                info = _get_json(client, f"{_API}/{addr}")
                scanned.append((addr, info))
                if _tx_count(info) == 0:
                    gap += 1
                else:
                    gap = 0
                index += 1

        own = {addr for addr, _info in scanned}
        by_txid: dict[str, dict] = {}
        for addr, info in scanned:
            if _tx_count(info) == 0:
                continue
            txs = _get_json(client, f"{_API}/{addr}/txs")
            for tx in txs:
                by_txid.setdefault(tx["txid"], tx)

    balance_sats = sum((_funded_minus_spent(info) for _addr, info in scanned), Decimal(0))
    transactions: list[Transaction] = []
    for txid, tx in by_txid.items():
        booked = _booked(tx)
        if booked is None or booked < since:
            continue
        net = _net_sats(tx, own)
        transactions.append(
            Transaction(
                source=NAME,
                account="Bitcoin",
                booked=booked,
                amount=net / _SATS,
                currency="BTC",
                description="Recebido" if net >= 0 else "Enviado",
                ext_id=txid,
            )
        )
    transactions.sort(key=lambda item: (item.booked, item.ext_id))

    return FetchResult(
        source=NAME,
        balances=[
            Balance(
                source=NAME,
                account="Bitcoin",
                currency="BTC",
                amount=balance_sats / _SATS,
                kind="crypto",
            )
        ],
        transactions=transactions,
    )
