"""Cotações ECB e BTC: EUR passa, fiat divide pela taxa, BTC multiplica pelo preço."""
from decimal import Decimal

import httpx
import pytest
import respx

from financas.fx import Rates

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2026-11-17">
      <Cube currency="USD" rate="1.0989"/>
      <Cube currency="GBP" rate="0.8600"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


def test_eur_devolve_o_mesmo_montante():
    rates = Rates.from_dict({"USD": Decimal("1.0989"), "BTC_EUR": Decimal("85000")})
    assert rates.to_eur(Decimal("12.50"), "EUR") == Decimal("12.50")


def test_usd_divide_pela_taxa_ecb():
    rates = Rates.from_dict({"USD": Decimal("1.0989"), "BTC_EUR": Decimal("85000")})
    assert rates.to_eur(Decimal("10"), "USD") == Decimal("10") / Decimal("1.0989")


def test_btc_multiplica_pelo_preco_eur():
    rates = Rates.from_dict({"USD": Decimal("1.0989"), "BTC_EUR": Decimal("85000")})
    assert rates.to_eur(Decimal("0.5"), "BTC") == Decimal("42500")


def test_moeda_desconhecida_levanta_valueerror():
    rates = Rates.from_dict({"USD": Decimal("1.0989"), "BTC_EUR": Decimal("85000")})
    with pytest.raises(ValueError, match="XYZ"):
        rates.to_eur(Decimal("1"), "XYZ")


@respx.mock
def test_fetch_le_xml_do_ecb_e_preco_btc():
    respx.get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml").mock(
        return_value=httpx.Response(200, text=ECB_XML)
    )
    respx.get("https://mempool.space/api/v1/prices").mock(
        return_value=httpx.Response(200, json={"USD": 98000, "EUR": 85000})
    )
    rates = Rates.fetch()
    assert rates.to_eur(Decimal("10"), "USD") == Decimal("10") / Decimal("1.0989")
    assert rates.to_eur(Decimal("86"), "GBP") == Decimal("100")
    assert rates.to_eur(Decimal("2"), "BTC") == Decimal("170000")
    assert rates.to_eur(Decimal("3"), "EUR") == Decimal("3")
