"""Relatórios: markdown com milhares e total, Excel lido de volta."""
from datetime import datetime
from decimal import Decimal

from openpyxl import load_workbook

from financas import store
from financas.report import write_excel, write_markdown


def _ymd(value):
    return (value.year, value.month, value.day)


def _db(path):
    db = store.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS run_status(run_id INT, source TEXT, status TEXT, warnings TEXT)"
    )
    return db


def _insert_balance(db, run_id, source, amount, amount_eur, currency="EUR", account="conta"):
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run_id, source, account, currency, str(amount), "cash", str(amount_eur)),
    )


def _insert_position(
    db, run_id, source, name, symbol, quantity, value, currency, value_eur, cost, account="conta"
):
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            source,
            account,
            symbol,
            name,
            str(quantity),
            str(value),
            currency,
            None if cost is None else str(cost),
            str(value_eur),
        ),
    )


def test_markdown_patrimonio_espacos_notas_e_total(tmp_path):
    """13 341 € com espaço normal, nota em $, total a negrito, posições por valor."""
    db = _db(tmp_path / "financas.db")
    run_id = store.new_run(db, at=datetime(2026, 11, 18, 9, 30, 0))
    _insert_balance(db, run_id, "santander", Decimal("10000"), Decimal("10000"))
    _insert_balance(db, run_id, "santander", Decimal("3341"), Decimal("3341"), account="poupanca")
    _insert_balance(db, run_id, "revolut", Decimal("500"), Decimal("500"))
    _insert_balance(db, run_id, "ibkr", Decimal("10000"), Decimal("9100"), currency="USD")
    _insert_position(
        db, run_id, "ibkr", "Apple", "AAPL", Decimal("2"), Decimal("4000"), "USD", Decimal("3640"), Decimal("3000")
    )
    _insert_position(
        db, run_id, "ibkr", "Microsoft", "MSFT", Decimal("1"), Decimal("888"), "USD", Decimal("808"), None
    )
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (run_id, "santander", "ok", "usar taxa de ontem"),
    )
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (run_id, "revolut", "precisa de ti: renova o consentimento", ""),
    )
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (run_id, "ibkr", "ok", ""),
    )
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (run_id, "bitcoin", "erro: RuntimeError: falhou", ""),
    )
    db.commit()

    out = tmp_path / "Investimentos"
    path = write_markdown(db, run_id, out)

    assert path == out / "2026-11-18_Patrimonio_auto.md"
    assert path.read_text(encoding="utf-8") == (
        "# Património a 18/11/2026 (automático)\n"
        "\n"
        "| Onde | Valor | Notas |\n"
        "| --- | --- | --- |\n"
        "| Santander | 13 341 € | |\n"
        "| Revolut | 500 € | precisa de ti: renova o consentimento |\n"
        "| IBKR | 13 548 € | 14 888 $ |\n"
        "| Bitcoin | 0 € | erro: RuntimeError: falhou |\n"
        "| **Total** | **27 389 €** | |\n"
        "\n"
        "## IBKR: posições\n"
        "\n"
        "Apple 3 640 €, Microsoft 808 €\n"
        "\n"
        "## Avisos\n"
        "\n"
        "- Santander: usar taxa de ontem\n"
        "- Revolut: precisa de ti: renova o consentimento\n"
        "- Bitcoin: erro: RuntimeError: falhou\n"
    )


def test_markdown_arredonda_meia_unidade_para_cima_e_agrupa_milhares(tmp_path):
    db = _db(tmp_path / "financas.db")
    run_id = store.new_run(db, at=datetime(2026, 3, 1, 12, 0, 0))
    _insert_balance(db, run_id, "santander", Decimal("10.50"), Decimal("10.50"))
    _insert_balance(db, run_id, "revolut", Decimal("1234567.40"), Decimal("1234567.40"))
    _insert_balance(db, run_id, "bitcoin", Decimal("-1500"), Decimal("-1500"), currency="EUR")
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (run_id, "santander", "ok", ""))
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (run_id, "revolut", "ok", ""))
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (run_id, "bitcoin", "ok", ""))
    db.commit()

    text = write_markdown(db, run_id, tmp_path).read_text(encoding="utf-8")
    assert "| Santander | 11 € | |" in text
    assert "| Revolut | 1 234 567 € | |" in text
    assert "| Bitcoin | -1 500 € | |" in text
    assert "1.234" not in text
    assert "1,234" not in text
    assert "\u202f" not in text
    assert "\u00a0" not in text


def test_excel_folhas_valores_e_so_a_corrida_mais_recente(tmp_path):
    db = _db(tmp_path / "financas.db")
    run1 = store.new_run(db, at=datetime(2026, 10, 1, 8, 0, 0))
    run2 = store.new_run(db, at=datetime(2026, 11, 18, 8, 0, 0))
    _insert_balance(db, run1, "santander", Decimal("1000"), Decimal("1000"))
    _insert_balance(db, run2, "santander", Decimal("2500.50"), Decimal("2500.50"))
    _insert_position(
        db, run1, "ibkr", "Velha", "OLD", Decimal("1"), Decimal("9"), "USD", Decimal("0"), Decimal("7")
    )
    _insert_position(
        db, run2, "ibkr", "Apple", "AAPL", Decimal("1.5"), Decimal("400"), "USD", Decimal("360"), Decimal("350")
    )
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        ("santander", "Conta ordem", "2026-10-02", "-10", "EUR", "Compra antiga", "a", run1),
    )
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        ("santander", "Conta ordem", "2026-11-01", "25.5", "EUR", "Juro", "b", run2),
    )
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (run1, "santander", "erro: RuntimeError: antigo", ""))
    db.execute("INSERT INTO run_status VALUES(?,?,?,?)", (run2, "santander", "ok", ""))
    db.execute(
        "INSERT INTO run_status VALUES(?,?,?,?)",
        (run2, "ibkr", "precisa de ti: token", "aviso flex"),
    )
    db.commit()

    xlsx = tmp_path / "Patrimonio_historico.xlsx"
    write_excel(db, xlsx)
    wb = load_workbook(xlsx)
    assert wb.sheetnames == ["Resumo", "Posições", "Movimentos", "Estado"]

    resumo = wb["Resumo"]
    assert [c.value for c in resumo[1]] == ["Data", "Santander", "IBKR", "Total"]
    assert _ymd(resumo["A2"].value) == (2026, 10, 1)
    assert resumo["B2"].value == 1000
    assert resumo["C2"].value == 0
    assert resumo["D2"].value == 1000
    assert _ymd(resumo["A3"].value) == (2026, 11, 18)
    assert resumo["B3"].value == 2500.5
    assert resumo["C3"].value == 360
    assert resumo["D3"].value == 2860.5
    assert resumo["B2"].number_format == "#,##0 €"
    assert resumo["D3"].number_format == "#,##0 €"
    assert resumo["A1"].font.bold is True
    assert resumo.freeze_panes == "A2"
    assert resumo.column_dimensions["A"].width

    pos = wb["Posições"]
    assert [c.value for c in pos[1]] == [
        "Fonte",
        "Nome",
        "Símbolo",
        "Quantidade",
        "Valor",
        "Moeda",
        "Valor €",
        "Custo",
    ]
    assert pos.max_row == 2
    assert [c.value for c in pos[2]] == ["IBKR", "Apple", "AAPL", 1.5, 400, "USD", 360, 350]
    assert pos["G2"].number_format == "#,##0 €"
    assert pos["A1"].font.bold is True
    assert pos.freeze_panes == "A2"
    names = [pos.cell(row, 2).value for row in range(2, pos.max_row + 1)]
    assert "Velha" not in names

    mov = wb["Movimentos"]
    assert [c.value for c in mov[1]] == ["Data", "Fonte", "Conta", "Descrição", "Montante", "Moeda"]
    assert _ymd(mov["A2"].value) == (2026, 11, 1)
    assert [c.value for c in list(mov[2])[1:]] == ["Santander", "Conta ordem", "Juro", 25.5, "EUR"]
    assert _ymd(mov["A3"].value) == (2026, 10, 2)
    assert [c.value for c in list(mov[3])[1:]] == [
        "Santander",
        "Conta ordem",
        "Compra antiga",
        -10,
        "EUR",
    ]
    assert mov["A1"].font.bold is True
    assert mov.freeze_panes == "A2"

    estado = wb["Estado"]
    assert [c.value for c in estado[1]] == ["Fonte", "Estado", "Avisos"]
    assert [c.value for c in estado[2]][:2] == ["Santander", "ok"]
    assert not estado.cell(2, 3).value
    assert [c.value for c in estado[3]] == ["IBKR", "precisa de ti: token", "aviso flex"]
    assert "antigo" not in " ".join(str(c.value) for row in estado.iter_rows() for c in row if c.value)
    assert estado["A1"].font.bold is True
    assert estado.freeze_panes == "A2"

    db.execute("DELETE FROM transactions WHERE ext_id = 'b'")
    db.commit()
    write_excel(db, xlsx)
    de_novo = load_workbook(xlsx)["Movimentos"]
    descricoes = [de_novo.cell(row, 4).value for row in range(2, de_novo.max_row + 1)]
    assert descricoes == ["Compra antiga"]


def test_format_amount_btc_keeps_satoshis():
    from decimal import Decimal
    from financas.report import format_amount
    assert format_amount(Decimal("0.04978187"), "BTC") == "0,04978187 BTC"
