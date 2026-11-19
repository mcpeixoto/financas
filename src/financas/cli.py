"""Linha de comandos: correr, configurar e ver o estado."""
from __future__ import annotations

import argparse
import importlib
import subprocess
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal

from financas import config, secrets, store
from financas.connectors import enablebanking
from financas.fx import Rates
from financas.models import NeedsUserAction
from financas.report import (
    display_name,
    eur_totals,
    format_eur,
    write_excel,
    write_markdown,
)

CONNECTORS = [
    "santander",
    "revolut",
    "traderepublic",
    "ibkr",
    "etoro",
    "aforro",
    "bitcoin",
]


def default_since() -> date:
    """Primeiro dia do mês anterior."""
    previous = date.today().replace(day=1) - timedelta(days=1)
    return previous.replace(day=1)


def ensure_run_status(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS run_status(
            run_id INT, source TEXT, status TEXT, warnings TEXT)"""
    )
    db.commit()


def _parse_since(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("data inválida, usa AAAA-MM-DD") from exc


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _notify(total: Decimal, attention: int) -> None:
    if attention == 1:
        phrase = "1 conector precisa de atenção"
    else:
        phrase = f"{attention} conectores precisam de atenção"
    text = _escape_applescript(f"Total {format_eur(total)}. {phrase}.")
    subprocess.run(
        ["osascript", "-e", f'display notification "{text}" with title "Finanças"'],
        check=False,
    )


def _log_exception(log_path, name: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"=== {name} ===\n")
        traceback.print_exc(file=handle)


def load_connector(name: str):
    return importlib.import_module(f"financas.connectors.{name}")


def _selected(only: str | None) -> list[str]:
    if not only:
        return list(CONNECTORS)
    wanted = {part.strip() for part in only.split(",") if part.strip()}
    return [name for name in CONNECTORS if name in wanted]


def _run_one(db, run_id: int, name: str, since: date, rates: Rates, log_path) -> str:
    source = name
    warnings = ""
    try:
        module = load_connector(name)
        source = getattr(module, "NAME", name)
        secret_keys = list(getattr(module, "SECRETS", {}) or {})
        if any(secrets.get(key) is None for key in secret_keys):
            status = "por configurar"
        else:
            result = module.fetch(since)
            store.save(db, run_id, result, rates.to_eur)
            status = "ok"
            warnings = "\n".join(result.warnings)
            source = result.source or source
    except NeedsUserAction as exc:
        db.rollback()
        status = f"precisa de ti: {exc}"
    except Exception as exc:
        db.rollback()
        status = f"erro: {type(exc).__name__}: {exc}"
        _log_exception(log_path, name)
    db.execute(
        "INSERT INTO run_status(run_id, source, status, warnings) VALUES(?,?,?,?)",
        (run_id, source, status, warnings),
    )
    db.commit()
    return status


def _print_summary(db, run_id: int, names: list[str]) -> None:
    totals = eur_totals(db, run_id)
    statuses = {
        source: status
        for source, status in db.execute(
            "SELECT source, status FROM run_status WHERE run_id=?", (run_id,)
        )
    }
    print(f"{'Onde':<28} {'Valor':>14}  Estado")
    grand = sum((totals.get(name, Decimal(0)) for name in names), start=Decimal(0))
    for name in names:
        amount = totals.get(name, Decimal(0))
        print(f"{display_name(name):<28} {format_eur(amount):>14}  {statuses.get(name, '')}")
    print(f"{'Total':<28} {format_eur(grand):>14}")


def cmd_run(only: str | None, since: date | None, notify: bool) -> int:
    since = since or default_since()
    rates = Rates.fetch()
    db = store.connect(config.DATA / "financas.db")
    ensure_run_status(db)
    run_id = store.new_run(db)
    db.commit()
    log_path = config.DATA / "logs" / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    names = _selected(only)
    ok = 0
    for name in names:
        status = _run_one(db, run_id, name, since, rates, log_path)
        if status == "ok":
            ok += 1

    out_dir = config.CONTABILIDADE / "Investimentos"
    md_path = write_markdown(db, run_id, out_dir)
    xlsx_path = write_excel(db, out_dir / "Patrimonio_historico.xlsx")
    _print_summary(db, run_id, names)
    print(f"Relatório: {md_path}")
    print(f"Excel: {xlsx_path}")
    if notify:
        grand = sum(eur_totals(db, run_id).values(), start=Decimal(0))
        _notify(grand, len(names) - ok)
    return 0 if ok else 1


def cmd_setup(name: str) -> int:
    module = load_connector(name)
    for key, label in (getattr(module, "SECRETS", {}) or {}).items():
        if secrets.get(key) is not None:
            continue
        if key == "enablebanking_private_key":
            path = input("Caminho para o ficheiro .pem da Enable Banking: ").strip()
            if not path:
                raise SystemExit("vazio, nada guardado")
            enablebanking.store_private_key_from_file(path)
        else:
            secrets.prompt_and_store(key, label)
    setup = getattr(module, "setup", None)
    if setup is not None:
        setup()
    print(f"{display_name(getattr(module, 'NAME', name))} configurado.")
    return 0


def cmd_bank_auth(name: str, start: bool, finish: str | None) -> int:
    """Two-step, non-interactive consent for Enable Banking connectors."""
    module = load_connector(name)
    hint = getattr(module, "ASPSP_HINT", None)
    if hint is None:
        raise SystemExit(f"{name} não é um banco da Enable Banking")
    if start:
        print(enablebanking.start_auth(getattr(module, "NAME", name), hint))
    if finish:
        enablebanking.finish_auth(getattr(module, "NAME", name), finish)
    return 0


def _format_secrets(module) -> str:
    items = getattr(module, "SECRETS", {}) or {}
    if not items:
        return "segredos: nenhum"
    parts = []
    for key in items:
        state = "sim" if secrets.get(key) is not None else "não"
        parts.append(f"{key} {state}")
    return "segredos: " + ", ".join(parts)


def _format_config(section: dict) -> str:
    if not section:
        return "config: (vazia)"
    parts = []
    for key, value in section.items():
        if "session" in str(key).lower():
            parts.append(str(key))
        else:
            parts.append(f"{key}={value}")
    return "config: " + ", ".join(parts)


def _format_consent(section: dict, today: date) -> str:
    raw = section.get("valid_until") if section else None
    if not raw:
        return "consentimento: em falta"
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return "consentimento: data inválida"
    until = parsed.date()
    days = (until - today).days
    shown = until.strftime("%d/%m/%Y")
    if days >= 0:
        return f"consentimento válido até {shown} ({days} dias)"
    return f"consentimento expirado em {shown} (há {-days} dias)"


def _last_status(db, source: str) -> str:
    row = db.execute(
        "SELECT status FROM run_status WHERE source=? ORDER BY run_id DESC LIMIT 1",
        (source,),
    ).fetchone()
    if row is None:
        return "sem corridas"
    return row[0]


def cmd_status() -> int:
    db = store.connect(config.DATA / "financas.db")
    ensure_run_status(db)
    today = date.today()
    for name in CONNECTORS:
        module = load_connector(name)
        section = config.section(name) or {}
        print(display_name(name))
        print(f"  {_format_secrets(module)}")
        print(f"  {_format_config(section)}")
        print(f"  último estado: {_last_status(db, getattr(module, 'NAME', name))}")
        if name in ("santander", "revolut"):
            print(f"  {_format_consent(section, today)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="financas", description="Saldos e património, só leitura.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="buscar saldos e gravar relatórios")
    run.add_argument("--only", help="conectores separados por vírgulas")
    run.add_argument("--since", type=_parse_since, help="AAAA-MM-DD; omissão: dia 1 do mês anterior")
    run.add_argument("--no-notify", action="store_true", help="não mostrar notificação do macOS")

    setup = sub.add_parser("setup", help="guardar segredos e preparar um conector")
    setup.add_argument("name")
    setup.add_argument("--start", action="store_true",
                       help="bancos: só imprime o URL de autorização (não interativo)")
    setup.add_argument("--finish", metavar="URL",
                       help="bancos: conclui com o URL de retorno do banco (não interativo)")

    sub.add_parser("status", help="segredos, configuração e última corrida")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args.only, args.since, notify=not args.no_notify)
    if args.cmd == "setup":
        if args.start or args.finish:
            return cmd_bank_auth(args.name, args.start, args.finish)
        return cmd_setup(args.name)
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
