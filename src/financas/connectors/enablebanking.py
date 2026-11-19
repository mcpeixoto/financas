"""Cliente partilhado Enable Banking (Santander, Revolut). Só leitura."""
from __future__ import annotations

import hashlib
import json
import uuid
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt

from financas import config, secrets
from financas.models import Balance, FetchResult, NeedsUserAction, Transaction

BASE = "https://api.enablebanking.com"
DEFAULT_REDIRECT = "https://localhost:8765/callback"
_BALANCE_TYPES = ("CLBD", "ITAV", "XPCD", "ITBD", "CLAV")

SECRETS = {
    "enablebanking_app_id": "Application ID da Enable Banking",
    "enablebanking_private_key": "Chave privada PEM da Enable Banking (cola o conteúdo todo)",
}


def make_jwt(app_id: str, pem: str) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode(
        {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": now,
            "exp": now + 3600,
        },
        pem,
        algorithm="RS256",
        headers={"typ": "JWT", "alg": "RS256", "kid": app_id},
    )
    if isinstance(token, bytes):
        return token.decode()
    return token


def store_private_key_from_file(path: str | Path) -> None:
    text = Path(path).read_text()
    secrets.set_("enablebanking_private_key", text)


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError(value)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value).replace(" ", "").replace(",", "."))


def _loads(text: str):
    if not text or not text.strip():
        return {}
    return json.loads(text, parse_float=Decimal)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _mask_iban(iban: str) -> str:
    compact = iban.replace(" ", "")
    if len(compact) >= 8:
        return f"{compact[:4]}…{compact[-4:]}"
    return compact


def _renew(name: str) -> NeedsUserAction:
    return NeedsUserAction(f"Renovar consentimento: financas setup {name}")


class EBClient:
    """HTTP client. Cada pedido leva um JWT novo."""

    def __init__(self, app_id: str | None = None, pem: str | None = None):
        self.app_id = app_id or secrets.get("enablebanking_app_id")
        self.pem = pem or secrets.get("enablebanking_private_key")
        if not self.app_id or not self.pem:
            raise NeedsUserAction(
                "Falta a Application ID ou a chave privada da Enable Banking no Keychain."
            )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_jwt(self.app_id, self.pem)}"}

    def get(self, path: str, params: dict | None = None):
        return self._send("GET", path, params=params)

    def post(self, path: str, json: dict | None = None):
        return self._send("POST", path, json_body=json)

    def _send(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None):
        url = path if path.startswith("http") else f"{BASE}{path}"
        with httpx.Client(timeout=30) as client:
            response = client.request(
                method, url, params=params, json=json_body, headers=self._headers()
            )
        if response.status_code in (401, 403):
            raise NeedsUserAction(
                "A Enable Banking recusou o acesso. Verifica a Application ID e a chave privada."
            )
        response.raise_for_status()
        return _loads(response.text)


def _code_from_redirect(pasted: str) -> str:
    parsed = urlparse(pasted.strip())
    for query in (parsed.query, parsed.fragment):
        code = parse_qs(query).get("code")
        if code and code[0]:
            return code[0]
    raise NeedsUserAction(
        "O URL colado não contém o código de autorização. "
        "Copia o endereço completo da barra do browser."
    )


def setup_bank(name: str, aspsp_hint: str) -> None:
    """Interactive setup: start, open the bank in the browser, paste the redirect URL."""
    url, chosen = _start(name, aspsp_hint)
    print(
        "Abre este endereço no browser e autoriza o acesso ao banco.\n"
        "Se a página do retorno não carregar, copia o endereço da barra:\n"
        f"{url}"
    )
    try:
        webbrowser.open(url)
    except Exception:
        print("Não foi possível abrir o browser automaticamente.")
    finish_auth(
        name,
        input("Cola aqui o URL completo da barra de endereço: "),
        aspsp=chosen["name"],
        country=chosen.get("country", "PT"),
    )


def start_auth(name: str, aspsp_hint: str) -> str:
    """Step 1: ask Enable Banking for a bank authorisation URL. Remembers the bank in config."""
    url, chosen = _start(name, aspsp_hint)
    config.update_section(name, {"aspsp": chosen["name"], "country": chosen.get("country", "PT")})
    return url


def _start(name: str, aspsp_hint: str) -> tuple[str, dict]:
    redirect = config.section("enablebanking").get("redirect_url") or DEFAULT_REDIRECT
    client = EBClient()
    data = client.get("/aspsps", params={"country": "PT", "psu_type": "personal"})
    aspsps = data.get("aspsps") if isinstance(data, dict) else data
    hint = aspsp_hint.lower()
    matches = [item for item in aspsps or [] if hint in str(item.get("name", "")).lower()]
    if not matches:
        raise NeedsUserAction(
            f"Nenhum banco encontrado para «{aspsp_hint}» em Portugal. Confirma o nome e tenta outra vez."
        )
    if len(matches) == 1:
        chosen = matches[0]
    else:
        print("Vários bancos correspondem:")
        for index, item in enumerate(matches, 1):
            print(f"  {index}) {item.get('name')}")
        raw = input("Número do banco: ").strip()
        chosen = matches[int(raw) - 1]

    max_validity = int(chosen.get("maximum_consent_validity") or 0)
    cap = 180 * 24 * 3600
    seconds = min(cap, max_validity) if max_validity else cap
    valid_until = _rfc3339(datetime.now(timezone.utc) + timedelta(seconds=seconds))
    auth = client.post(
        "/auth",
        json={
            "access": {"valid_until": valid_until},
            "aspsp": {"name": chosen["name"], "country": chosen.get("country", "PT")},
            "state": str(uuid.uuid4()),
            "redirect_url": redirect,
            "psu_type": "personal",
        },
    )
    return auth["url"], chosen


def finish_auth(name: str, redirect_url: str, aspsp: str | None = None, country: str | None = None) -> None:
    """Step 2: exchange the code from the bank's redirect URL for a session and save it."""
    code = _code_from_redirect(redirect_url)
    session = EBClient().post("/sessions", json={"code": code})
    access_until = (session.get("access") or {}).get("valid_until")
    section = config.section(name)
    config.update_section(
        name,
        {
            "aspsp": aspsp or section.get("aspsp"),
            "country": country or section.get("country", "PT"),
            "session_id": session.get("session_id") or session.get("sessionId"),
            "valid_until": access_until,
        },
    )
    print(f"Consentimento de {name} guardado até {access_until}.")


def _pick_balance(balances: list[dict]) -> dict | None:
    if not balances:
        return None
    best: tuple[int, dict] | None = None
    for item in balances:
        kind = str(item.get("balance_type") or "")
        try:
            rank = _BALANCE_TYPES.index(kind)
        except ValueError:
            rank = len(_BALANCE_TYPES)
        if best is None or rank < best[0]:
            best = (rank, item)
    return best[1] if best else None


def _account_meta(client: EBClient, entry) -> tuple[str, dict]:
    if isinstance(entry, str):
        meta = client.get(f"/accounts/{entry}/details")
        uid = str(meta.get("uid") or entry) if isinstance(meta, dict) else entry
        return uid, meta if isinstance(meta, dict) else {}
    uid = entry.get("uid")
    if not uid and isinstance(entry.get("account_id"), str):
        uid = entry["account_id"]
    return str(uid), entry


def _label(meta: dict, uid: str) -> str:
    account_id = meta.get("account_id")
    iban = account_id.get("iban") if isinstance(account_id, dict) else None
    if iban:
        return _mask_iban(str(iban))
    return str(meta.get("name") or meta.get("product") or uid)


def _party_name(value) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name else None
    return None


def _description(tx: dict) -> str:
    indicator = str(tx.get("credit_debit_indicator") or "").upper()
    creditor = _party_name(tx.get("creditor"))
    debtor = _party_name(tx.get("debtor"))
    party = (creditor or debtor) if indicator == "DBIT" else (debtor or creditor)
    remittance = tx.get("remittance_information") or []
    if isinstance(remittance, str):
        remittance = [remittance]
    parts = [party, *[str(item) for item in remittance if item]]
    return " ".join(part for part in parts if part)


def _signed_amount(tx: dict) -> Decimal:
    raw = (tx.get("transaction_amount") or {}).get("amount")
    amount = _dec(raw)
    indicator = str(tx.get("credit_debit_indicator") or "").upper()
    if indicator == "DBIT":
        return -abs(amount)
    if indicator == "CRDT":
        return abs(amount)
    return amount


def _transactions(client: EBClient, name: str, uid: str, label: str, since: date) -> list[Transaction]:
    params: dict[str, str] = {"date_from": since.isoformat()}
    found: list[Transaction] = []
    per_day: dict[date, int] = {}
    while True:
        payload = client.get(f"/accounts/{uid}/transactions", params=params)
        for tx in payload.get("transactions") or []:
            if str(tx.get("status") or "").upper() != "BOOK":
                continue
            raw_date = tx.get("booking_date") or tx.get("value_date")
            if not raw_date:
                continue
            booked = date.fromisoformat(str(raw_date)[:10])
            if booked < since:
                continue
            amount = _signed_amount(tx)
            description = _description(tx)
            index = per_day.get(booked, 0)
            per_day[booked] = index + 1
            reference = tx.get("entry_reference")
            if reference:
                ext_id = str(reference)
            else:
                raw = f"{booked.isoformat()}|{amount}|{description}|{index}"
                ext_id = hashlib.sha1(raw.encode()).hexdigest()
            currency = str((tx.get("transaction_amount") or {}).get("currency") or "EUR")
            found.append(
                Transaction(
                    source=name,
                    account=label,
                    booked=booked,
                    amount=amount,
                    currency=currency,
                    description=description,
                    ext_id=ext_id,
                )
            )
        token = payload.get("continuation_key") if isinstance(payload, dict) else None
        if not token:
            break
        params = {"date_from": since.isoformat(), "continuation_key": str(token)}
    return found


def fetch_bank(name: str, since: date) -> FetchResult:
    cfg = config.section(name)
    session_id = cfg.get("session_id")
    if not session_id:
        raise _renew(name)
    client = EBClient()
    session = client.get(f"/sessions/{session_id}")
    if not isinstance(session, dict) or session.get("status") != "AUTHORIZED":
        raise _renew(name)
    raw_until = (session.get("access") or {}).get("valid_until") or cfg.get("valid_until")
    warnings: list[str] = []
    if raw_until:
        deadline = _parse_dt(str(raw_until))
        now = datetime.now(timezone.utc)
        if deadline <= now:
            raise _renew(name)
        if deadline - now < timedelta(days=14):
            warnings.append(
                f"O consentimento expira em {deadline.date().isoformat()} (menos de 14 dias). "
                f"Renova com: financas setup {name}"
            )
    balances: list[Balance] = []
    transactions: list[Transaction] = []
    for entry in session.get("accounts") or []:
        uid, meta = _account_meta(client, entry)
        label = _label(meta, uid)
        payload = client.get(f"/accounts/{uid}/balances")
        chosen = _pick_balance((payload or {}).get("balances") or [])
        if chosen:
            money = chosen.get("balance_amount") or {}
            balances.append(
                Balance(
                    source=name,
                    account=label,
                    currency=str(money.get("currency") or "EUR"),
                    amount=_dec(money.get("amount")),
                    kind="cash",
                )
            )
        transactions.extend(_transactions(client, name, uid, label, since))
    return FetchResult(source=name, balances=balances, transactions=transactions, warnings=warnings)
