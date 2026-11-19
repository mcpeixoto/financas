"""Bitcoin: derivação BIP84, saldo (chain+mempool) e netting com auto-envio para troco."""
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from financas.connectors import bitcoin
from financas.models import NeedsUserAction

# Vetor BIP84, conta m/84'/0'/0' (zpub público de teste).
ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
)
ADDR0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
CHANGE0 = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
EXTERNAL = "bc1qexternaladdr000000000000000000000000000"

TX_IN = "11" * 32
TX_SELF = "22" * 32


def _stats(funded, spent, tx_count, mem_funded=0, mem_spent=0, mem_tx=0):
    return {
        "chain_stats": {
            "funded_txo_sum": funded,
            "spent_txo_sum": spent,
            "tx_count": tx_count,
        },
        "mempool_stats": {
            "funded_txo_sum": mem_funded,
            "spent_txo_sum": mem_spent,
            "tx_count": mem_tx,
        },
    }


def _tx(txid, block_time, vin, vout):
    status = {"confirmed": block_time is not None}
    if block_time is not None:
        status["block_time"] = block_time
    return {"txid": txid, "status": status, "vin": vin, "vout": vout}


# Recebe 150000 sats; depois envia, ficando 100000 no troco e 40000 fora (fee 10000).
# Mempool do troco soma mais 5000 sats que não estão na lista de txs confirmadas.
TXS = {
    ADDR0: [
        _tx(
            TX_IN,
            1700000000,
            [{"prevout": {"scriptpubkey_address": EXTERNAL, "value": 200000}}],
            [
                {"scriptpubkey_address": ADDR0, "value": 150000},
                {"scriptpubkey_address": EXTERNAL, "value": 40000},
            ],
        ),
        _tx(
            TX_SELF,
            1700100000,
            [{"prevout": {"scriptpubkey_address": ADDR0, "value": 150000}}],
            [
                {"scriptpubkey_address": CHANGE0, "value": 100000},
                {"scriptpubkey_address": EXTERNAL, "value": 40000},
            ],
        ),
    ],
    CHANGE0: [
        _tx(
            TX_SELF,
            1700100000,
            [{"prevout": {"scriptpubkey_address": ADDR0, "value": 150000}}],
            [
                {"scriptpubkey_address": CHANGE0, "value": 100000},
                {"scriptpubkey_address": EXTERNAL, "value": 40000},
            ],
        ),
    ],
}

INFO = {
    ADDR0: _stats(150000, 150000, 2),
    CHANGE0: _stats(100000, 0, 1, mem_funded=5000, mem_tx=1),
}


def _install_mempool(rate_limit_once=False):
    state = {"limited": False}

    def handle(request: httpx.Request):
        path = request.url.path
        if path.endswith("/txs"):
            addr = path.split("/")[-2]
            return httpx.Response(200, json=TXS.get(addr, []))
        addr = path.rstrip("/").split("/")[-1]
        if rate_limit_once and addr == ADDR0 and not state["limited"]:
            state["limited"] = True
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=INFO.get(addr, _stats(0, 0, 0)))

    return respx.get(url__startswith="https://mempool.space/api/address/").mock(
        side_effect=handle
    )


def _patch(monkeypatch, zpub=ZPUB):
    monkeypatch.setattr(
        "financas.secrets.get",
        lambda name: zpub if name == "btc_zpub" else None,
    )
    monkeypatch.setattr("financas.connectors.bitcoin._sleep", lambda *_a, **_k: None)


def test_nome_e_segredo():
    assert bitcoin.NAME == "bitcoin"
    assert bitcoin.SECRETS["btc_zpub"] == "Chave pública mestra (zpub) do Electrum"


def test_sem_zpub_pede_acao(monkeypatch):
    _patch(monkeypatch, zpub=None)
    with pytest.raises(NeedsUserAction, match="[Zz]pub|Electrum"):
        bitcoin.fetch(date(2023, 1, 1))


@respx.mock
def test_saldo_e_netting_com_auto_envio_para_troco(monkeypatch):
    _patch(monkeypatch)
    route = _install_mempool()
    result = bitcoin.fetch(date(2023, 1, 1))

    assert len(result.balances) == 1
    bal = result.balances[0]
    assert bal.source == "bitcoin"
    assert bal.currency == "BTC"
    assert bal.kind == "crypto"
    # 100000 confirmados no troco + 5000 na mempool.
    assert bal.amount == Decimal("0.00105")

    by_id = {tx.ext_id: tx for tx in result.transactions}
    assert set(by_id) == {TX_IN, TX_SELF}
    incoming = by_id[TX_IN]
    assert incoming.amount == Decimal("0.0015")
    assert incoming.booked == date(2023, 11, 14)
    assert incoming.currency == "BTC"
    assert incoming.source == "bitcoin"
    moved = by_id[TX_SELF]
    # 100000 para o nosso troco − 150000 gastos = −50000 sats.
    assert moved.amount == Decimal("-0.0005")
    assert moved.booked == date(2023, 11, 16)
    assert moved.currency == "BTC"

    addr_calls = [c for c in route.calls if not c.request.url.path.endswith("/txs")]
    tx_calls = [c for c in route.calls if c.request.url.path.endswith("/txs")]
    assert addr_calls[0].request.url.path.endswith(ADDR0)
    assert any(c.request.url.path.endswith(CHANGE0) for c in addr_calls)
    # 1 usado + 20 vazios, em cada uma das duas cadeias.
    assert len(addr_calls) == 42
    assert len(tx_calls) == 2


@respx.mock
def test_since_exclui_movimento_anterior(monkeypatch):
    _patch(monkeypatch)
    _install_mempool()
    result = bitcoin.fetch(date(2023, 11, 15))
    assert [tx.ext_id for tx in result.transactions] == [TX_SELF]


@respx.mock
def test_repete_pedido_depois_de_429(monkeypatch):
    _patch(monkeypatch)
    route = _install_mempool(rate_limit_once=True)
    result = bitcoin.fetch(date(2023, 1, 1))
    assert result.balances[0].amount == Decimal("0.00105")
    assert any(c.response.status_code == 429 for c in route.calls)
    # O recebimento só está no primeiro endereço; sem retry perde-se o movimento.
    assert {tx.ext_id for tx in result.transactions} == {TX_IN, TX_SELF}
