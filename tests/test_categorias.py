"""Classificação de movimentos: regras pessoais, genéricas e gastos."""
from datetime import date
from decimal import Decimal

from financas import config, store


def _sem_pessoais(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")


def _inserir(db, ext_id, booked, amount, description, account="conta-teste"):
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (
            "santander",
            account,
            booked,
            str(amount),
            "EUR",
            description,
            ext_id,
            1,
        ),
    )


def test_regra_pessoal_ganha_a_generica(monkeypatch, tmp_path):
    """Uma regra pessoal sobre 'lidl' deixa de ser Supermercado."""
    from financas.categorias import Classe, carregar_regras, classify

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA", data)
    (data / "categorias.toml").write_text(
        """
contas_proprias = ["PT50000000000000000000000"]

[[regra]]
categoria = "Mercearia"
tipo = "gasto"
padrao = "lidl"
""",
        encoding="utf-8",
    )

    regras = carregar_regras()
    assert classify("santander", "conta", "LIDL CAMPO GRANDE", Decimal("-12.40"), regras) == Classe(
        "gasto", "Mercearia"
    )
    assert classify("santander", "conta", "Pingo Doce Alvalade", Decimal("-8"), regras) == Classe(
        "gasto", "Supermercado"
    )


def test_interna_e_investimento_fora_dos_gastos(monkeypatch, tmp_path, capsys):
    """Transferências entre contas e corretoras não entram na percentagem."""
    from financas import cli
    from financas.categorias import classify

    _sem_pessoais(monkeypatch, tmp_path)
    data = config.DATA
    data.mkdir()
    (data / "categorias.toml").write_text(
        'contas_proprias = ["PT50000000000000000000000"]\n',
        encoding="utf-8",
    )

    assert classify(
        "revolut", "conta", "TRADE REPUBLIC", Decimal("-1000")
    ).tipo == "investimento"
    assert classify(
        "revolut", "conta", "Top-up", Decimal("-20")
    ).tipo == "interna"
    assert classify(
        "santander",
        "conta",
        "TRF para PT50000000000000000000000",
        Decimal("-50"),
    ).tipo == "interna"

    db = store.connect(tmp_path / "data" / "financas.db")
    _inserir(db, "g1", "2026-06-02", Decimal("-30.00"), "Lidl Benfica")
    _inserir(db, "g2", "2026-06-03", Decimal("-70.00"), "Uber Eats Lisboa")
    _inserir(db, "i1", "2026-06-04", Decimal("-999.99"), "Interactive Brokers")
    _inserir(db, "n1", "2026-06-05", Decimal("-123.45"), "Carregamento")
    _inserir(db, "n2", "2026-06-06", Decimal("-40.00"), "TRF PT50000000000000000000000")
    db.commit()

    assert cli.main(["categorias"]) == 0
    out = capsys.readouterr().out
    assert "Supermercado → 30,0%" in out
    assert "Restauração → 70,0%" in out
    assert "Investimento" not in out
    assert "interna" not in out.lower()
    assert "999.99" not in out
    assert "123.45" not in out


def test_sem_regra_vai_para_outros(monkeypatch, tmp_path):
    from financas.categorias import Classe, classify

    _sem_pessoais(monkeypatch, tmp_path)
    assert classify("santander", "conta", "xyzzy pagamento raro", Decimal("-4")) == Classe(
        "gasto", "Outros"
    )
    assert classify("santander", "conta", "xyzzy pagamento raro", Decimal("4")) == Classe(
        "receita", "Outros"
    )
    assert classify("santander", "conta", "xyzzy pagamento raro", Decimal("0")) == Classe(
        "receita", "Outros"
    )


def test_ficheiro_pessoal_ausente(monkeypatch, tmp_path):
    from financas.categorias import Classe, carregar_regras, classify

    _sem_pessoais(monkeypatch, tmp_path)
    regras = carregar_regras()
    assert regras.contas_proprias == ()
    assert classify("santander", "conta", "auchan", Decimal("-3"), regras) == Classe(
        "gasto", "Supermercado"
    )


def test_regex_ignora_maiusculas_e_acentos(monkeypatch, tmp_path):
    from financas.categorias import Classe, classify

    _sem_pessoais(monkeypatch, tmp_path)
    assert classify("santander", "conta", "MINIPREÇO TELHEIRAS", Decimal("-6")) == Classe(
        "gasto", "Supermercado"
    )
    assert classify("santander", "conta", "minipreco", Decimal("-6")) == Classe(
        "gasto", "Supermercado"
    )
    assert classify("santander", "conta", "FARMÁCIA SANTOS", Decimal("-2")) == Classe(
        "gasto", "Saúde"
    )


def test_cli_sem_categoria_e_desde(monkeypatch, tmp_path, capsys):
    from financas import cli

    _sem_pessoais(monkeypatch, tmp_path)
    db = store.connect(tmp_path / "data" / "financas.db")
    _inserir(db, "old", "2026-01-15", Decimal("-50.00"), "Lidl antigo")
    _inserir(db, "g1", "2026-06-02", Decimal("-80.00"), "Lidl recente")
    _inserir(db, "o1", "2026-06-03", Decimal("-15.00"), "PAGAMENTO RARO")
    _inserir(db, "o2", "2026-06-04", Decimal("-5.00"), "PAGAMENTO RARO")
    _inserir(db, "o3", "2026-01-20", Decimal("-7.77"), "PAGAMENTO ANTIGO")
    _inserir(db, "rec", "2026-06-05", Decimal("1500.00"), "vencimento junho")
    db.commit()

    assert cli.main(["categorias", "--sem-categoria", "--desde", "2026-06-01"]) == 0
    out = capsys.readouterr().out
    assert "Supermercado → 80,0%" in out
    assert "Outros → 20,0%" in out
    assert "PAGAMENTO RARO" in out
    assert "PAGAMENTO ANTIGO" not in out
    assert "Lidl antigo" not in out
    assert "15.00" not in out
    assert "1500" not in out
    # contagem da descrição, sem repetir o texto
    assert out.count("PAGAMENTO RARO") == 1
    raro = next(line for line in out.splitlines() if "PAGAMENTO RARO" in line)
    assert "2" in raro.split("PAGAMENTO RARO")[0]


def test_recurso_generico_no_pacote():
    from importlib.resources import files

    texto = files("financas.regras").joinpath("categorias.toml").read_text(encoding="utf-8")
    assert 'categoria = "Supermercado"' in texto
