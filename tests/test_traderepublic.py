"""Trade Republic via pytr: CSV da carteira e dos movimentos, sem rede."""
from __future__ import annotations

import subprocess
from datetime import date
from decimal import Decimal
from hashlib import sha1
from pathlib import Path

import pytest

from financas.models import NeedsUserAction


PORTFOLIO_SEMI = """\
Name;ISIN;quantity;price;avgCost;netValue
MSCI World;IE00B4L5Y983;10;100,50;90,25;1.005,00
Cash;;0;0;0;123,45
"""

TX_SEMI = """\
Date;Type;Value;Note;ISIN;Shares;Fees
2024-02-01;Buy;-1.234,56;Compra ETF;IE00B4L5Y983;10;1,00
2024-02-01;Dividend;12,50;Dividendo;IE00B4L5Y983;10;0
2023-12-01;Interest;1,00;Juros;;0;0
"""

PORTFOLIO_COMMA = """\
Instrument Name,ISIN,Shares,Price,Average Cost,Net Value
Apple,US0378331005,2,190.00,180.50,380.00
"""

TX_COMMA = """\
Datum,Type,Amount,Description,ISIN,Shares,Fees
02.05.2024,Sell,380.00,Venda Apple,US0378331005,2,1.00
"""


def _install_pytr(monkeypatch, writer):
    import financas.connectors.traderepublic as tr

    monkeypatch.setattr(tr.shutil, "which", lambda name: "/usr/bin/pytr")
    monkeypatch.setattr(tr.config, "section", lambda name: {})

    def fake_run(cmd, **kwargs):
        assert isinstance(cmd, list)
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("timeout") == 300
        assert kwargs.get("shell") in (None, False)
        writer(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    return tr


def test_missing_binary_asks_to_install(monkeypatch):
    import financas.connectors.traderepublic as tr

    monkeypatch.setattr(tr.shutil, "which", lambda name: None)
    monkeypatch.setattr(tr.config, "section", lambda name: {})

    def boom(*args, **kwargs):
        raise AssertionError("pytr não devia ser executado")

    monkeypatch.setattr(tr.subprocess, "run", boom)
    with pytest.raises(NeedsUserAction) as exc:
        tr.fetch(date(2024, 1, 1))
    assert str(exc.value) == "Instala o pytr: uv tool install pytr==0.4.10"


def test_pytr_path_from_config_when_which_misses(monkeypatch):
    import financas.connectors.traderepublic as tr

    seen = []
    monkeypatch.setattr(tr.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        tr.config, "section", lambda name: {"pytr_path": "/opt/bin/pytr"}
    )

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if cmd[1] == "portfolio":
            Path(cmd[cmd.index("-o") + 1]).write_text(PORTFOLIO_COMMA, encoding="utf-8")
        else:
            outdir = Path(cmd[cmd.index("--outputdir") + 1])
            (outdir / "movimentos.csv").write_text(TX_COMMA, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    tr.fetch(date(2024, 1, 1))
    assert seen[0][0] == "/opt/bin/pytr"


def test_login_failure_needs_user(monkeypatch):
    import financas.connectors.traderepublic as tr

    monkeypatch.setattr(tr.shutil, "which", lambda name: "/usr/bin/pytr")
    monkeypatch.setattr(tr.config, "section", lambda name: {})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="login: credentials rejected 401"
        )

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    with pytest.raises(NeedsUserAction) as exc:
        tr.fetch(date(2024, 1, 1))
    assert str(exc.value) == (
        "Trade Republic: corre `pytr login --store_credentials --v2` no Terminal"
    )


def test_zero_exit_with_login_word_still_needs_user(monkeypatch):
    import financas.connectors.traderepublic as tr

    monkeypatch.setattr(tr.shutil, "which", lambda name: "/usr/bin/pytr")
    monkeypatch.setattr(tr.config, "section", lambda name: {})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="Please Login again", stderr="")

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    with pytest.raises(NeedsUserAction):
        tr.fetch(date(2024, 1, 1))


def test_semicolon_comma_decimals(monkeypatch):
    commands = []

    def writer(cmd):
        commands.append(list(cmd))
        if cmd[1] == "portfolio":
            assert cmd[2] == "-o"
            Path(cmd[3]).write_text(PORTFOLIO_SEMI, encoding="utf-8")
        else:
            assert cmd[1:] == [
                "export_transactions",
                "--last_days",
                str((date.today() - date(2024, 1, 1)).days + 1),
                "--format",
                "csv",
                "--outputdir",
                cmd[-1],
            ]
            (Path(cmd[-1]) / "transactions.csv").write_text(TX_SEMI, encoding="utf-8")

    tr = _install_pytr(monkeypatch, writer)
    result = tr.fetch(date(2024, 1, 1))

    assert commands[0][1] == "portfolio"
    assert len(result.positions) == 1
    pos = result.positions[0]
    assert pos.currency == "EUR"
    assert pos.symbol == "IE00B4L5Y983"
    assert pos.name == "MSCI World"
    assert pos.quantity == Decimal("10")
    assert pos.value == Decimal("1005.00")
    assert pos.cost_basis == Decimal("902.50")
    assert pos.source == "traderepublic"

    assert len(result.balances) == 1
    cash = result.balances[0]
    assert cash.kind == "cash"
    assert cash.currency == "EUR"
    assert cash.amount == Decimal("123.45")
    assert result.warnings == []

    assert len(result.transactions) == 2
    buy, div = result.transactions
    assert buy.booked == date(2024, 2, 1)
    assert buy.amount == Decimal("-1234.56")
    assert buy.currency == "EUR"
    assert "Compra ETF" in buy.description
    assert div.amount == Decimal("12.50")
    assert buy.ext_id != div.ext_id
    assert buy.ext_id == sha1(
        "2024-02-01|Buy|-1.234,56|Compra ETF|0".encode()
    ).hexdigest()
    assert div.ext_id == sha1(
        "2024-02-01|Dividend|12,50|Dividendo|1".encode()
    ).hexdigest()
    assert all(tx.booked >= date(2024, 1, 1) for tx in result.transactions)


def test_comma_delimiter_dot_decimals_and_missing_cash(monkeypatch):
    def writer(cmd):
        if cmd[1] == "portfolio":
            Path(cmd[3]).write_text(PORTFOLIO_COMMA, encoding="utf-8")
        else:
            (Path(cmd[-1]) / "export.csv").write_text(TX_COMMA, encoding="utf-8")

    tr = _install_pytr(monkeypatch, writer)
    result = tr.fetch(date(2024, 1, 1))

    assert len(result.positions) == 1
    pos = result.positions[0]
    assert pos.currency == "EUR"
    assert pos.symbol == "US0378331005"
    assert pos.name == "Apple"
    assert pos.quantity == Decimal("2")
    assert pos.value == Decimal("380.00")
    assert pos.cost_basis == Decimal("361.00")
    assert result.balances == []
    assert result.warnings
    assert "caixa" in result.warnings[0].lower()

    assert len(result.transactions) == 1
    tx = result.transactions[0]
    assert tx.booked == date(2024, 5, 2)
    assert tx.amount == Decimal("380.00")
    assert "Venda Apple" in tx.description
    assert tx.currency == "EUR"


def test_pytr_never_reads_stdin(monkeypatch):
    import financas.connectors.traderepublic as tr

    monkeypatch.setattr(tr.shutil, "which", lambda name: "/usr/bin/pytr")
    monkeypatch.setattr(tr.config, "section", lambda name: {})
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs.get("stdin"))
        return subprocess.CompletedProcess(cmd, 1, stdout="Please enter your phone number", stderr="")

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    with pytest.raises(NeedsUserAction):
        tr.fetch(date(2024, 1, 1))
    assert seen and all(value is subprocess.DEVNULL for value in seen)


REAL_PORTFOLIO_STDOUT = """\
19:44:16 Using credentials from file /Users/x/.pytr/credentials. Phone: +3519********, PIN: ****
19:44:16 Trying to resume websession...
19:44:16 Websession resumed.
Depot                                        0.00 ->       0.00       0.00     0.0%
Cash EUR                                 10394.58
Total                                    10394.58 ->   10394.58
Wrote 1 lines to p.csv
"""


def test_resumed_session_log_is_not_a_login_request_and_cash_from_stdout(monkeypatch):
    import financas.connectors.traderepublic as tr

    monkeypatch.setattr(tr.shutil, "which", lambda name: "/usr/bin/pytr")
    monkeypatch.setattr(tr.config, "section", lambda name: {})

    def fake_run(cmd, **kwargs):
        if cmd[1] == "portfolio":
            Path(cmd[3]).write_text("Name;ISIN;quantity;price;avgCost;netValue\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=REAL_PORTFOLIO_STDOUT, stderr="")
        (Path(cmd[-1]) / "t.csv").write_text(TX_SEMI, encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Using credentials from file /Users/x/.pytr/credentials.", stderr=""
        )

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    result = tr.fetch(date(2024, 1, 1))
    assert [b.amount for b in result.balances] == [Decimal("10394.58")]
    assert result.warnings == []
