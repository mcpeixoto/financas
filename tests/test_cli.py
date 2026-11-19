"""Corrida, setup e estado: um conector não derruba os outros."""
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from financas import config, secrets, store
from financas.fx import Rates
from financas.models import Balance, FetchResult, NeedsUserAction, Transaction


class _Today(date):
    fixed = date(2026, 11, 18)

    @classmethod
    def today(cls):
        return cls.fixed


def _rates_fetch(counter):
    def fetch(cls):
        counter["n"] += 1
        return Rates.from_dict({"USD": "1", "BTC_EUR": "1"})

    return classmethod(fetch)


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "CONTABILIDADE", tmp_path / "Contabilidade")


def _patch_osascript(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("financas.cli.subprocess.run", fake_run)
    return calls


def _install_connectors(monkeypatch, modules, secrets_store):
    monkeypatch.setattr("financas.cli.CONNECTORS", list(modules))
    monkeypatch.setattr("financas.cli.load_connector", lambda name: modules[name])
    monkeypatch.setattr(secrets, "get", lambda key: secrets_store.get(key))


def _four_connectors(seen):
    def fetch_ok(since):
        seen.append(("ok", since))
        return FetchResult(
            source="ok",
            balances=[
                Balance(source="ok", account="conta", currency="EUR", amount=Decimal("1000"), kind="cash")
            ],
            transactions=[
                Transaction(
                    source="ok",
                    account="conta",
                    booked=date(2026, 10, 5),
                    amount=Decimal("-5"),
                    currency="EUR",
                    description="café",
                    ext_id="tx-1",
                )
            ],
            warnings=["cuidado"],
        )

    def fetch_needs(since):
        seen.append(("needs", since))
        raise NeedsUserAction("renova o consentimento")

    def fetch_boom(since):
        seen.append(("boom", since))
        raise RuntimeError("falhou a ligação")

    def fetch_missing(since):
        raise AssertionError("conector sem segredo não devia correr")

    return {
        "ok": SimpleNamespace(NAME="ok", SECRETS={"k": "Chave ok"}, fetch=fetch_ok),
        "needs": SimpleNamespace(NAME="needs", SECRETS={}, fetch=fetch_needs),
        "boom": SimpleNamespace(NAME="boom", SECRETS={"t": "Token"}, fetch=fetch_boom),
        "missing": SimpleNamespace(NAME="missing", SECRETS={"absent": "Em falta"}, fetch=fetch_missing),
    }


def test_run_estados_saida_base_de_dados_e_notificacao(monkeypatch, tmp_path, capsys):
    from financas import cli

    seen = []
    counter = {"n": 0}
    _patch_paths(monkeypatch, tmp_path)
    osascript = _patch_osascript(monkeypatch)
    _install_connectors(
        monkeypatch,
        _four_connectors(seen),
        {"k": "segredo-ok", "t": "segredo-boom"},
    )
    monkeypatch.setattr("financas.cli.Rates.fetch", _rates_fetch(counter))
    monkeypatch.setattr(cli, "date", _Today)

    assert cli.main(["run", "--no-notify"]) == 0
    assert counter["n"] == 1
    assert seen == [("ok", date(2026, 10, 1)), ("needs", date(2026, 10, 1)), ("boom", date(2026, 10, 1))]
    assert osascript == []

    db = store.connect(tmp_path / "data" / "financas.db")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert db.execute("SELECT source, status, warnings FROM run_status ORDER BY rowid").fetchall() == [
        ("ok", "ok", "cuidado"),
        ("needs", "precisa de ti: renova o consentimento", ""),
        ("boom", "erro: RuntimeError: falhou a ligação", ""),
        ("missing", "por configurar", ""),
    ]
    assert db.execute("SELECT source, amount_eur FROM balances").fetchall() == [("ok", "1000")]
    assert db.execute("SELECT ext_id FROM transactions").fetchall() == [("tx-1",)]

    out = capsys.readouterr().out
    assert "precisa de ti: renova o consentimento" in out
    assert "erro: RuntimeError: falhou a ligação" in out
    assert "por configurar" in out
    assert "segredo-ok" not in out
    assert "segredo-boom" not in out

    logs = list((tmp_path / "data" / "logs").glob("run-*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "Traceback" in log
    assert "boom" in log
    assert "falhou a ligação" in log
    assert "segredo-boom" not in log

    md = list((tmp_path / "Contabilidade" / "Investimentos").glob("*_Patrimonio_auto.md"))
    xlsx = tmp_path / "Contabilidade" / "Investimentos" / "Patrimonio_historico.xlsx"
    assert len(md) == 1
    assert "Património" in md[0].read_text(encoding="utf-8")
    assert xlsx.is_file()


def test_run_notifica_total_e_quem_precisa_de_atencao(monkeypatch, tmp_path):
    from financas import cli

    _patch_paths(monkeypatch, tmp_path)
    osascript = _patch_osascript(monkeypatch)
    _install_connectors(
        monkeypatch,
        _four_connectors([]),
        {"k": "segredo-ok", "t": "segredo-boom"},
    )
    monkeypatch.setattr("financas.cli.Rates.fetch", _rates_fetch({"n": 0}))
    monkeypatch.setattr(cli, "date", _Today)

    assert cli.main(["run"]) == 0
    assert osascript == [
        [
            "osascript",
            "-e",
            'display notification "Total 1 000 €. 3 conectores precisam de atenção." with title "Finanças"',
        ]
    ]


def test_run_sem_sucesso_termina_com_1(monkeypatch, tmp_path):
    from financas import cli

    modules = _four_connectors([])
    del modules["ok"]
    del modules["needs"]
    _patch_paths(monkeypatch, tmp_path)
    _patch_osascript(monkeypatch)
    _install_connectors(monkeypatch, modules, {"t": "segredo-boom"})
    monkeypatch.setattr("financas.cli.Rates.fetch", _rates_fetch({"n": 0}))

    assert cli.main(["run", "--no-notify", "--only", "boom,missing"]) == 1
    db = store.connect(tmp_path / "data" / "financas.db")
    statuses = [row[1] for row in db.execute("SELECT source, status FROM run_status ORDER BY rowid")]
    assert statuses == ["erro: RuntimeError: falhou a ligação", "por configurar"]
    assert db.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 0


def test_run_since_explicito_e_janeiro(monkeypatch, tmp_path):
    from financas import cli

    seen = []

    def fetch(since):
        seen.append(since)
        return FetchResult(source="ok")

    modules = {"ok": SimpleNamespace(NAME="ok", SECRETS={}, fetch=fetch)}
    _patch_paths(monkeypatch, tmp_path)
    _patch_osascript(monkeypatch)
    _install_connectors(monkeypatch, modules, {})
    monkeypatch.setattr("financas.cli.Rates.fetch", _rates_fetch({"n": 0}))

    assert cli.main(["run", "--no-notify", "--since", "2026-05-02"]) == 0
    assert seen == [date(2026, 5, 2)]

    seen.clear()
    january = type("Jan", (date,), {"today": classmethod(lambda cls: date(2026, 1, 10))})
    monkeypatch.setattr(cli, "date", january)
    assert cli.main(["run", "--no-notify"]) == 0
    assert seen == [date(2025, 12, 1)]


def test_notificacao_singular_e_aspas_no_texto(monkeypatch, tmp_path):
    from financas import cli

    def fetch_ok(since):
        return FetchResult(
            source='ok "a"',
            balances=[
                Balance(source='ok "a"', account="a", currency="EUR", amount=Decimal("10"), kind="cash")
            ],
        )

    def fetch_needs(since):
        raise NeedsUserAction('token "x"')

    modules = {
        "ok": SimpleNamespace(NAME='ok "a"', SECRETS={}, fetch=fetch_ok),
        "needs": SimpleNamespace(NAME="needs", SECRETS={}, fetch=fetch_needs),
    }
    _patch_paths(monkeypatch, tmp_path)
    osascript = _patch_osascript(monkeypatch)
    _install_connectors(monkeypatch, modules, {})
    monkeypatch.setattr("financas.cli.Rates.fetch", _rates_fetch({"n": 0}))

    assert cli.main(["run"]) == 0
    script = osascript[0][2]
    assert script == (
        'display notification "Total 10 €. 1 conector precisa de atenção." '
        'with title "Finanças"'
    )
    assert 'token "x"' not in script


def test_setup_pede_segredos_em_falta_e_pem(monkeypatch):
    from financas import cli

    order = []

    def prompt(key, label):
        order.append(("prompt", key, label))

    def store_pem(path):
        order.append(("pem", path))

    def setup():
        order.append(("setup",))

    module = SimpleNamespace(
        NAME="santander",
        SECRETS={
            "enablebanking_app_id": "Application ID",
            "enablebanking_private_key": "PEM",
            "other": "Outro",
        },
        setup=setup,
    )
    monkeypatch.setattr(secrets, "get", lambda key: None)
    monkeypatch.setattr(secrets, "prompt_and_store", prompt)
    monkeypatch.setattr("financas.connectors.enablebanking.store_private_key_from_file", store_pem)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "/tmp/chave.pem")
    monkeypatch.setattr("financas.cli.load_connector", lambda name: module)

    assert cli.main(["setup", "santander"]) == 0
    assert order == [
        ("prompt", "enablebanking_app_id", "Application ID"),
        ("pem", "/tmp/chave.pem"),
        ("prompt", "other", "Outro"),
        ("setup",),
    ]


def test_setup_nao_repede_segredos_presentes_nem_pergunta_sem_setup(monkeypatch):
    from financas import cli

    prompted = []
    module = SimpleNamespace(NAME="bitcoin", SECRETS={"btc_zpub": "zpub"})
    monkeypatch.setattr(secrets, "get", lambda key: "ja-esta")
    monkeypatch.setattr(secrets, "prompt_and_store", lambda key, label: prompted.append(key))
    monkeypatch.setattr("builtins.input", lambda *a, **k: (_ for _ in ()).throw(AssertionError("input")))
    monkeypatch.setattr("financas.cli.load_connector", lambda name: module)

    assert cli.main(["setup", "bitcoin"]) == 0
    assert prompted == []


def test_status_nao_mostra_segredos_nem_sessao_e_conta_dias(monkeypatch, tmp_path, capsys):
    from financas import cli

    _patch_paths(monkeypatch, tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    db = store.connect(data / "financas.db")
    db.execute(
        "CREATE TABLE IF NOT EXISTS run_status(run_id INT, source TEXT, status TEXT, warnings TEXT)"
    )
    db.execute("INSERT INTO run_status VALUES(1, 'santander', 'erro: RuntimeError: antigo', '')")
    db.execute("INSERT INTO run_status VALUES(2, 'santander', 'ok', '')")
    db.execute("INSERT INTO run_status VALUES(2, 'ibkr', 'erro: ValueError: query', '')")
    db.commit()
    db.close()

    def get(name):
        if name == "enablebanking_app_id":
            return "super-secret-value"
        if name == "ibkr_flex_token":
            return "tok-secret"
        return None

    def section(name):
        if name == "santander":
            return {
                "aspsp_name": "Santander Totta",
                "session_id": "sess-secret",
                "valid_until": "2026-11-28T23:59:59Z",
            }
        if name == "revolut":
            return {"valid_until": "2026-11-08T00:00:00Z", "session_id": "rev-secret"}
        return {}

    monkeypatch.setattr(secrets, "get", get)
    monkeypatch.setattr(config, "section", section)
    monkeypatch.setattr(cli, "date", _Today)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "super-secret-value" not in out
    assert "tok-secret" not in out
    assert "sess-secret" not in out
    assert "rev-secret" not in out
    assert "enablebanking_app_id sim" in out
    assert "enablebanking_private_key não" in out
    assert "Santander Totta" in out
    assert "session_id" in out
    assert "consentimento válido até 28/11/2026 (10 dias)" in out
    assert "consentimento expirado em 08/11/2026 (há 10 dias)" in out
    assert "último estado: ok" in out
    assert "antigo" not in out
    assert "erro: ValueError: query" in out
    assert "sem corridas" in out
