"""Relatórios HTML semanais e mensais, sem valores por omissão."""
import subprocess
from datetime import date, datetime
from decimal import Decimal

from financas import config, store
from financas.cli import main


def _ambiente(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "CONTABILIDADE", tmp_path / "contab")
    (tmp_path / "data").mkdir()
    chamadas = []

    def falso(args, **kwargs):
        chamadas.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("financas.cli.subprocess.run", falso)
    return chamadas


def _pasta(tmp_path):
    return tmp_path / "contab" / "Investimentos" / "Relatorios"


def _fixar_hoje(monkeypatch, dia: date) -> None:
    import financas.relatorios as relatorios

    class _Hoje(date):
        @classmethod
        def today(cls):
            return dia

    monkeypatch.setattr(relatorios, "date", _Hoje)


def _db(tmp_path):
    return store.connect(tmp_path / "data" / "financas.db")


def _tx(db, booked, amount, description, source="santander"):
    _tx.n += 1
    db.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
        (source, "conta", booked.isoformat(), str(amount), "EUR", description, f"tx{_tx.n}", 1),
    )


_tx.n = 0


def _saldo(db, run_id, source, amount):
    db.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (run_id, source, "conta", "EUR", str(amount), "cash", str(amount)),
    )


def _posicao(db, run_id, source, value):
    db.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, source, "conta", "IWDA", "ETF mundial", "1", str(value), "EUR", None, str(value)),
    )


def _fluxo(db, source, booked, amount, kind):
    _fluxo.n += 1
    db.execute(
        "INSERT INTO cashflows VALUES(?,?,?,?,?,?)",
        (source, booked.isoformat(), str(amount), kind, "", f"cf{_fluxo.n}"),
    )


_fluxo.n = 0


def _preco(db, day, close):
    db.execute(
        "INSERT INTO prices VALUES(?,?,?,?)",
        ("IWDA.UK", day.isoformat(), str(close), "EUR"),
    )


def _semear(db):
    """Semana 2026-W20 (11–17 de maio) e dezembro de 2025, com números redondos."""
    for dia in (date(2026, 4, 13), date(2026, 4, 20), date(2026, 4, 27), date(2026, 5, 4)):
        _tx(db, dia, Decimal("-40"), "LIDL")
    _tx(db, date(2026, 5, 12), Decimal("-50"), "LIDL")
    _tx(db, date(2026, 5, 13), Decimal("-20"), "LIDL")
    _tx(db, date(2026, 5, 14), Decimal("-10"), "CAFE CENTRAL")
    _tx(db, date(2026, 5, 15), Decimal("2000"), "VENCIMENTO")

    for dia in (date(2025, 9, 5), date(2025, 10, 5), date(2025, 11, 5), date(2025, 12, 5)):
        _tx(db, dia, Decimal("-9.99"), "NETFLIX")
    _tx(db, date(2025, 9, 10), Decimal("-20"), "PINGO DOCE")
    _tx(db, date(2025, 10, 10), Decimal("-20"), "CONTINENTE")
    _tx(db, date(2025, 11, 10), Decimal("-20"), "AUCHAN")
    _tx(db, date(2025, 12, 2), Decimal("-30"), "LIDL")
    _tx(db, date(2025, 12, 10), Decimal("1000"), "VENCIMENTO")

    novembro = store.new_run(db, at=datetime(2025, 11, 30, 9, 0, 0))
    _saldo(db, novembro, "santander", Decimal("1000"))
    dezembro = store.new_run(db, at=datetime(2025, 12, 31, 9, 0, 0))
    _saldo(db, dezembro, "santander", Decimal("1100"))
    _posicao(db, dezembro, "ibkr", Decimal("1100"))
    antes = store.new_run(db, at=datetime(2026, 5, 10, 9, 0, 0))
    _saldo(db, antes, "santander", Decimal("2000"))
    depois = store.new_run(db, at=datetime(2026, 5, 17, 9, 0, 0))
    _saldo(db, depois, "santander", Decimal("2200"))

    _fluxo(db, "ibkr", date(2025, 6, 1), Decimal("1000"), "deposito")
    _preco(db, date(2025, 6, 1), Decimal("100"))
    _preco(db, date(2025, 12, 31), Decimal("110"))
    db.commit()


def _notificou(chamadas, nome: str) -> bool:
    return any(args and args[0] == "osascript" and nome in " ".join(args) for args in chamadas)


def test_semanal_cria_html_no_caminho_certo_sem_euros_e_com_valores(monkeypatch, tmp_path):
    """Sem --com-valores o HTML da semana pedida não leva euros; com a flag leva."""
    chamadas = _ambiente(monkeypatch, tmp_path)
    db = _db(tmp_path)
    _semear(db)
    db.close()

    assert main(["relatorio", "--semanal", "--semana", "2026-W20"]) == 0
    caminho = _pasta(tmp_path) / "2026-W20.html"
    assert caminho.is_file()
    assert not (_pasta(tmp_path) / "2026-W19.html").exists()
    texto = caminho.read_text(encoding="utf-8")
    for titulo in (
        "Gastos por categoria",
        "Por dia",
        "Top comerciantes",
        "4 semanas anteriores",
        "Variação do património",
        "11 a 17 de maio de 2026",
    ):
        assert titulo in texto
    assert "O maior gasto foi em Supermercado (88% do período)." in texto
    assert "Gastas mais à terça-feira." in texto
    assert "LIDL" in texto
    assert "75,0%" in texto
    assert "10,0%" in texto
    assert "€" not in texto
    assert "70,00" not in texto
    assert "prefers-color-scheme" in texto
    assert "900px" in texto
    assert "width: 100%" in texto
    assert 'src="https://cdn.plot.ly' not in texto
    assert texto.count("plotly.js v") == 1
    assert _notificou(chamadas, "2026-W20.html")

    assert main(["relatorio", "--semanal", "--semana", "2026-W20", "--com-valores"]) == 0
    com_valores = caminho.read_text(encoding="utf-8")
    assert "70,00 €" in com_valores
    assert "200,00 €" in com_valores
    assert com_valores.count("plotly.js v") == 1


def test_mensal_tem_as_seccoes_e_a_mascara_de_valores(monkeypatch, tmp_path):
    """O mês pedido compara com o anterior, e os euros só aparecem com --com-valores."""
    chamadas = _ambiente(monkeypatch, tmp_path)
    db = _db(tmp_path)
    _semear(db)
    db.close()

    assert main(["relatorio", "--mensal", "--mes", "2025-12"]) == 0
    caminho = _pasta(tmp_path) / "2025-12.html"
    assert caminho.is_file()
    assert not (_pasta(tmp_path) / "2026-10.html").exists()
    texto = caminho.read_text(encoding="utf-8")
    for titulo in (
        "Dezembro de 2025",
        "Gastos por categoria",
        "Por dia",
        "Top comerciantes",
        "Variação do património",
        "Comparação com o mês anterior",
        "média 3 meses",
        "Taxa de poupança",
        "Subscrições",
        "Rentabilidade",
        "MSCI World",
        "Alocação",
        "Evolução do património",
        "Ações/ETFs",
        "IBKR",
        "NETFLIX",
    ):
        assert titulo in texto
    assert "O maior gasto foi em Supermercado (75% do período)." in texto
    assert "As subscrições custam 25% dos teus gastos." in texto
    assert "A taxa de poupança foi de 96%." in texto
    assert "€" not in texto
    assert "9,99 €" not in texto
    assert texto.count("plotly.js v") == 1
    assert _notificou(chamadas, "2025-12.html")

    assert main(["relatorio", "--mensal", "--mes", "2025-12", "--com-valores"]) == 0
    assert "9,99 €" in caminho.read_text(encoding="utf-8")


def test_semana_por_omissao_na_segunda_e_a_anterior(monkeypatch, tmp_path):
    """À segunda-feira a omissão é a semana ISO que acabou, também na viragem do ano."""
    _ambiente(monkeypatch, tmp_path)
    db = _db(tmp_path)
    db.close()

    _fixar_hoje(monkeypatch, date(2026, 11, 16))
    assert main(["relatorio", "--semanal"]) == 0
    assert (_pasta(tmp_path) / "2026-W46.html").is_file()
    assert not (_pasta(tmp_path) / "2026-W47.html").exists()
    assert "9 a 15 de novembro de 2026" in (_pasta(tmp_path) / "2026-W46.html").read_text(encoding="utf-8")

    _fixar_hoje(monkeypatch, date(2024, 12, 30))
    assert main(["relatorio", "--semanal"]) == 0
    assert (_pasta(tmp_path) / "2024-W52.html").is_file()
    assert not (_pasta(tmp_path) / "2025-W01.html").exists()
    assert "23 a 29 de dezembro de 2024" in (_pasta(tmp_path) / "2024-W52.html").read_text(encoding="utf-8")


def test_mes_por_omissao_em_janeiro_e_dezembro_do_ano_anterior(monkeypatch, tmp_path):
    """Em janeiro, o mês por omissão é dezembro do ano anterior."""
    _ambiente(monkeypatch, tmp_path)
    db = _db(tmp_path)
    db.close()

    _fixar_hoje(monkeypatch, date(2026, 1, 1))
    assert main(["relatorio", "--mensal"]) == 0
    assert (_pasta(tmp_path) / "2025-12.html").is_file()
    assert not (_pasta(tmp_path) / "2026-01.html").exists()
    texto = (_pasta(tmp_path) / "2025-12.html").read_text(encoding="utf-8")
    assert "Dezembro de 2025" in texto
    assert "Taxa de poupança" in texto
    assert "Evolução do património" in texto
