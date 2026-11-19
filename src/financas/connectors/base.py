"""Connector contract.

Each connector module in this package exposes:

    NAME: str                         # short id, e.g. "ibkr"
    SECRETS: dict[str, str]           # keychain name -> label shown when asking the user
    def fetch(since: date) -> FetchResult
        # read-only; transactions booked on/after `since`
        # raise NeedsUserAction(msg) when the human must do something
    def setup() -> None               # optional interactive one-time setup

Connectors read secrets only via financas.secrets.get() and config via
financas.config.load(). HTTP via httpx with timeout=30.
"""
