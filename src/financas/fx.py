"""Taxas de câmbio: referência diária do BCE e preço BTC/EUR da mempool."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from decimal import Decimal

import httpx

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
BTC_URL = "https://mempool.space/api/v1/prices"


def _dec(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("montante tem de ser Decimal, int ou str")
    return Decimal(value)


def _parse_ecb(text: str) -> dict[str, Decimal]:
    rates: dict[str, Decimal] = {}
    for el in ET.fromstring(text).iter():
        currency = el.attrib.get("currency")
        rate = el.attrib.get("rate")
        if currency and rate:
            rates[currency.upper()] = Decimal(rate)
    return rates


class Rates:
    """Taxas do BCE: unidades de moeda estrangeira por 1 EUR. BTC_EUR é EUR por 1 BTC."""

    def __init__(self, rates: dict[str, Decimal], btc_eur: Decimal | None):
        self._rates = rates
        self._btc_eur = btc_eur

    @classmethod
    def from_dict(cls, data: dict) -> Rates:
        rates: dict[str, Decimal] = {}
        btc_eur: Decimal | None = None
        for key, value in data.items():
            if key.upper() == "BTC_EUR":
                btc_eur = _dec(value)
            else:
                rates[key.upper()] = _dec(value)
        return cls(rates, btc_eur)

    @classmethod
    def fetch(cls) -> Rates:
        with httpx.Client(timeout=30) as client:
            ecb = client.get(ECB_URL)
            ecb.raise_for_status()
            prices_resp = client.get(BTC_URL)
            prices_resp.raise_for_status()
        prices = json.loads(prices_resp.text, parse_float=Decimal)
        if "EUR" not in prices:
            raise ValueError("Sem cotação de BTC em EUR")
        return cls(_parse_ecb(ecb.text), _dec(prices["EUR"]))

    def to_eur(self, amount: Decimal, currency: str) -> Decimal:
        code = currency.upper()
        if code == "EUR":
            return amount
        if code == "BTC":
            if self._btc_eur is None:
                raise ValueError("Sem cotação de BTC em EUR")
            return amount * self._btc_eur
        try:
            rate = self._rates[code]
        except KeyError:
            raise ValueError(f"Moeda desconhecida: {code}") from None
        return amount / rate
