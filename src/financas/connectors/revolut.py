"""Revolut via Enable Banking. Só leitura."""
from __future__ import annotations

from datetime import date

from financas.connectors.enablebanking import SECRETS, fetch_bank, setup_bank
from financas.models import FetchResult

NAME = "revolut"
ASPSP_HINT = "Revolut"


def fetch(since: date) -> FetchResult:
    return fetch_bank(NAME, since)


def setup() -> None:
    setup_bank(NAME, ASPSP_HINT)
