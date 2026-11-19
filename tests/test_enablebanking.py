"""Enable Banking (Santander, Revolut): JWT, consentimento e movimentos."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from financas.connectors import enablebanking, revolut, santander
from financas.models import NeedsUserAction

BASE = "https://api.enablebanking.com"
FAR = "2099-01-01T00:00:00Z"


def _pem() -> tuple[str, object]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def test_make_jwt_header_and_claims():
    import jwt

    pem, public = _pem()
    before = int(datetime.now(timezone.utc).timestamp())
    token = enablebanking.make_jwt("app-42", pem)
    after = int(datetime.now(timezone.utc).timestamp())

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["typ"] == "JWT"
    assert header["kid"] == "app-42"

    claims = jwt.decode(
        token, public, algorithms=["RS256"], audience="api.enablebanking.com"
    )
    assert claims["iss"] == "enablebanking.com"
    assert claims["aud"] == "api.enablebanking.com"
    assert before <= claims["iat"] <= after
    assert claims["exp"] - claims["iat"] == 3600


def test_store_private_key_from_file(tmp_path, monkeypatch, capsys):
    pem = "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n"
    path = tmp_path / "enable.pem"
    path.write_text(pem)
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        enablebanking.secrets, "set_", lambda name, value: stored.__setitem__(name, value)
    )
    enablebanking.store_private_key_from_file(path)
    assert stored == {"enablebanking_private_key": pem}
    assert "ABC" not in capsys.readouterr().out


def test_santander_and_revolut_delegate(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(santander, "fetch_bank", lambda name, since: calls.append(("f", name, since)) or "S")
    monkeypatch.setattr(santander, "setup_bank", lambda name, hint: calls.append(("s", name, hint)))
    monkeypatch.setattr(revolut, "fetch_bank", lambda name, since: calls.append(("f", name, since)) or "R")
    monkeypatch.setattr(revolut, "setup_bank", lambda name, hint: calls.append(("s", name, hint)))

    assert santander.NAME == "santander"
    assert revolut.NAME == "revolut"
    assert santander.SECRETS == revolut.SECRETS
    assert "enablebanking_app_id" in santander.SECRETS
    assert "enablebanking_private_key" in santander.SECRETS
    assert "PEM" in santander.SECRETS["enablebanking_private_key"]

    since = date(2026, 1, 1)
    assert santander.fetch(since) == "S"
    santander.setup()
    assert revolut.fetch(since) == "R"
    revolut.setup()
    assert calls == [
        ("f", "santander", since),
        ("s", "santander", "Santander"),
        ("f", "revolut", since),
        ("s", "revolut", "Revolut"),
    ]


def _install_secrets(monkeypatch, pem: str = "PEM") -> None:
    def fake_get(name: str):
        return {
            "enablebanking_app_id": "app-42",
            "enablebanking_private_key": pem,
        }.get(name)

    monkeypatch.setattr(enablebanking.secrets, "get", fake_get)
    monkeypatch.setattr(enablebanking, "make_jwt", lambda app_id, key: f"jwt-{app_id}")


def _session(status: str = "AUTHORIZED", valid_until: str = FAR, accounts=None) -> dict:
    return {
        "status": status,
        "accounts": accounts if accounts is not None else ["acc-1"],
        "access": {"valid_until": valid_until},
    }


def _balance(btype: str, amount: str, currency: str = "EUR") -> dict:
    return {
        "name": btype,
        "balance_type": btype,
        "balance_amount": {"amount": amount, "currency": currency},
        "reference_date": "2026-11-01",
    }


@respx.mock
def test_balance_type_preference_and_masked_iban(monkeypatch):
    _install_secrets(monkeypatch)
    monkeypatch.setattr(
        enablebanking.config,
        "section",
        lambda name: {"session_id": "sess-1", "valid_until": FAR},
    )
    respx.get(f"{BASE}/sessions/sess-1").mock(
        return_value=httpx.Response(200, json=_session(accounts=["acc-1", "acc-2", "acc-3"]))
    )
    respx.get(f"{BASE}/accounts/acc-1/details").mock(
        return_value=httpx.Response(
            200,
            json={
                "uid": "acc-1",
                "name": "Ordem",
                "account_id": {"iban": "PT50000201231234567890154"},
            },
        )
    )
    respx.get(f"{BASE}/accounts/acc-2/details").mock(
        return_value=httpx.Response(200, json={"uid": "acc-2", "name": "Poupança"})
    )
    respx.get(f"{BASE}/accounts/acc-3/details").mock(
        return_value=httpx.Response(200, json={"uid": "acc-3", "name": "Outra"})
    )
    respx.get(f"{BASE}/accounts/acc-1/balances").mock(
        return_value=httpx.Response(
            200,
            json={
                "balances": [
                    _balance("CLAV", "1.00"),
                    _balance("ITBD", "2.00"),
                    _balance("XPCD", "3.00"),
                    _balance("ITAV", "4.00"),
                    _balance("CLBD", "5.50"),
                ]
            },
        )
    )
    respx.get(f"{BASE}/accounts/acc-2/balances").mock(
        return_value=httpx.Response(
            200,
            json={"balances": [_balance("OTHER", "9.00"), _balance("CLAV", "8.25")]},
        )
    )
    respx.get(f"{BASE}/accounts/acc-3/balances").mock(
        return_value=httpx.Response(
            200, json={"balances": [_balance("XXXX", "7.10", "USD")]}
        )
    )
    respx.get(url__regex=r".*/transactions").mock(
        return_value=httpx.Response(200, json={"transactions": [], "continuation_key": None})
    )

    result = enablebanking.fetch_bank("santander", date(2026, 1, 1))
    by_account = {b.account: b for b in result.balances}
    assert by_account["PT50…0154"].amount == Decimal("5.50")
    assert by_account["PT50…0154"].currency == "EUR"
    assert by_account["PT50…0154"].kind == "cash"
    assert by_account["PT50…0154"].source == "santander"
    assert by_account["Poupança"].amount == Decimal("8.25")
    assert by_account["Outra"].amount == Decimal("7.10")
    assert by_account["Outra"].currency == "USD"
    assert result.warnings == []


@respx.mock
def test_transactions_paging_and_dbit(monkeypatch):
    _install_secrets(monkeypatch)
    monkeypatch.setattr(
        enablebanking.config, "section", lambda name: {"session_id": "sess-9"}
    )
    respx.get(f"{BASE}/sessions/sess-9").mock(
        return_value=httpx.Response(200, json=_session())
    )
    respx.get(f"{BASE}/accounts/acc-1/details").mock(
        return_value=httpx.Response(
            200, json={"uid": "acc-1", "account_id": {"iban": "PT50123456789012345678901"}}
        )
    )
    respx.get(f"{BASE}/accounts/acc-1/balances").mock(
        return_value=httpx.Response(200, json={"balances": [_balance("CLBD", "10.00")]})
    )

    seen: list[dict] = []

    def transactions(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        assert params["date_from"] == "2026-10-01"
        if "continuation_key" not in params:
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "entry_reference": "REF-1",
                            "transaction_amount": {"amount": "12.34", "currency": "EUR"},
                            "credit_debit_indicator": "DBIT",
                            "booking_date": "2026-10-02",
                            "status": "BOOK",
                            "remittance_information": ["Compra"],
                            "creditor": {"name": "Continente"},
                            "debtor": {"name": "Eu"},
                        },
                        {
                            "transaction_amount": {"amount": "1.00", "currency": "EUR"},
                            "credit_debit_indicator": "CRDT",
                            "booking_date": "2026-10-02",
                            "status": "PDNG",
                            "remittance_information": ["ignorar"],
                            "debtor": {"name": "Alguém"},
                        },
                    ],
                    "continuation_key": "page-2",
                },
            )
        assert params["continuation_key"] == "page-2"
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_amount": {"amount": "3.50", "currency": "EUR"},
                        "credit_debit_indicator": "CRDT",
                        "booking_date": "2026-10-03",
                        "status": "BOOK",
                        "remittance_information": ["Salário", "Outubro"],
                        "debtor": {"name": "Empresa"},
                    },
                    {
                        "transaction_amount": {"amount": "2.00", "currency": "EUR"},
                        "credit_debit_indicator": "DBIT",
                        "booking_date": "2026-10-03",
                        "status": "BOOK",
                        "remittance_information": ["Sem ref A"],
                        "creditor": {"name": "Loja"},
                    },
                    {
                        "transaction_amount": {"amount": "4.00", "currency": "EUR"},
                        "credit_debit_indicator": "DBIT",
                        "booking_date": "2026-10-03",
                        "status": "BOOK",
                        "remittance_information": ["Sem ref B"],
                        "creditor": {"name": "Loja"},
                    },
                ],
                "continuation_key": None,
            },
        )

    respx.get(f"{BASE}/accounts/acc-1/transactions").mock(side_effect=transactions)

    result = enablebanking.fetch_bank("santander", date(2026, 10, 1))
    assert [t.ext_id for t in result.transactions][0] == "REF-1"
    assert result.transactions[0].amount == Decimal("-12.34")
    assert result.transactions[0].description == "Continente Compra"
    assert result.transactions[0].booked == date(2026, 10, 2)
    assert result.transactions[0].account == "PT50…8901"
    assert all(t.booked >= date(2026, 10, 1) for t in result.transactions)
    assert len(result.transactions) == 4
    credit = next(t for t in result.transactions if t.amount == Decimal("3.50"))
    assert credit.description == "Empresa Salário Outubro"
    no_ref = [t for t in result.transactions if t.booked == date(2026, 10, 3) and t.amount < 0]
    assert len(no_ref) == 2
    assert no_ref[0].ext_id != no_ref[1].ext_id
    assert len(no_ref[0].ext_id) == 40
    assert len(seen) == 2


@respx.mock
def test_expired_session_needs_user(monkeypatch):
    _install_secrets(monkeypatch)
    monkeypatch.setattr(
        enablebanking.config, "section", lambda name: {"session_id": "sess-x"}
    )
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    respx.get(f"{BASE}/sessions/sess-x").mock(
        return_value=httpx.Response(200, json=_session(valid_until=past))
    )
    with pytest.raises(NeedsUserAction, match="financas setup santander"):
        enablebanking.fetch_bank("santander", date(2026, 1, 1))


@respx.mock
def test_unauthorized_status_needs_user(monkeypatch):
    _install_secrets(monkeypatch)
    monkeypatch.setattr(
        enablebanking.config, "section", lambda name: {"session_id": "sess-x"}
    )
    respx.get(f"{BASE}/sessions/sess-x").mock(
        return_value=httpx.Response(200, json=_session(status="EXPIRED"))
    )
    with pytest.raises(NeedsUserAction, match="Renovar consentimento"):
        enablebanking.fetch_bank("revolut", date(2026, 1, 1))


@respx.mock
def test_consent_expiring_within_14_days_warns(monkeypatch):
    _install_secrets(monkeypatch)
    soon = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        enablebanking.config, "section", lambda name: {"session_id": "sess-w"}
    )
    respx.get(f"{BASE}/sessions/sess-w").mock(
        return_value=httpx.Response(200, json=_session(valid_until=soon, accounts=[]))
    )
    result = enablebanking.fetch_bank("santander", date(2026, 1, 1))
    assert result.balances == []
    assert result.warnings
    text = " ".join(result.warnings).lower()
    assert "consentimento" in text or "expir" in text


@respx.mock
def test_setup_flow(monkeypatch, capsys):
    pem, _public = _pem()
    _install_secrets(monkeypatch, pem)
    saved: dict = {}
    monkeypatch.setattr(enablebanking.config, "section", lambda name: {})
    monkeypatch.setattr(
        enablebanking.config,
        "update_section",
        lambda name, values: saved.update(name=name, values=values),
    )
    opened: list[str] = []
    monkeypatch.setattr(enablebanking.webbrowser, "open", lambda url: opened.append(url))
    redirect = "https://localhost:8765/callback?code=AUTH-99&state=abc"
    monkeypatch.setattr("builtins.input", lambda *a, **k: redirect)

    respx.get(f"{BASE}/aspsps").mock(
        return_value=httpx.Response(
            200,
            json={
                "aspsps": [
                    {
                        "name": "Banco BPI",
                        "country": "PT",
                        "maximum_consent_validity": 10**7,
                    },
                    {
                        "name": "Banco Santander Totta",
                        "country": "PT",
                        "maximum_consent_validity": 90 * 24 * 3600,
                    },
                ]
            },
        )
    )

    def auth(request: httpx.Request) -> httpx.Response:
        body = request.read()
        import json

        payload = json.loads(body)
        assert payload["aspsp"] == {"name": "Banco Santander Totta", "country": "PT"}
        assert payload["psu_type"] == "personal"
        assert payload["redirect_url"] == "https://localhost:8765/callback"
        assert payload["state"]
        valid = datetime.fromisoformat(payload["access"]["valid_until"].replace("Z", "+00:00"))
        delta = valid - datetime.now(timezone.utc)
        assert timedelta(days=89) < delta < timedelta(days=91)
        assert request.headers["authorization"] == "Bearer jwt-app-42"
        return httpx.Response(
            200,
            json={"url": "https://bank.example/authorize?x=1", "authorization_id": "az-1"},
        )

    respx.post(f"{BASE}/auth").mock(side_effect=auth)

    def sessions(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.read())
        assert payload == {"code": "AUTH-99"}
        return httpx.Response(
            200,
            json={
                "session_id": "sess-new",
                "accounts": [],
                "access": {"valid_until": "2027-02-01T00:00:00Z"},
            },
        )

    respx.post(f"{BASE}/sessions").mock(side_effect=sessions)

    enablebanking.setup_bank("santander", "Santander")
    assert opened == ["https://bank.example/authorize?x=1"]
    assert "https://bank.example/authorize?x=1" in capsys.readouterr().out
    assert saved["name"] == "santander"
    assert saved["values"]["session_id"] == "sess-new"
    assert saved["values"]["valid_until"] == "2027-02-01T00:00:00Z"
    assert saved["values"]["aspsp"] == "Banco Santander Totta"
    assert saved["values"]["country"] == "PT"


@respx.mock
def test_start_then_finish_auth_two_steps(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(enablebanking, "make_jwt", lambda app_id, pem: "jwt")
    monkeypatch.setattr(enablebanking.secrets, "get", lambda key: "x")
    monkeypatch.setattr(enablebanking.config, "section", lambda name: dict(store.get(name, {})))
    monkeypatch.setattr(
        enablebanking.config,
        "update_section",
        lambda name, values: store.setdefault(name, {}).update(values),
    )
    respx.get(f"{BASE}/aspsps").mock(
        return_value=httpx.Response(200, json={"aspsps": [{"name": "Revolut", "country": "PT"}]})
    )
    respx.post(f"{BASE}/auth").mock(
        return_value=httpx.Response(200, json={"url": "https://bank.example/a", "authorization_id": "z"})
    )
    respx.post(f"{BASE}/sessions").mock(
        return_value=httpx.Response(
            200, json={"session_id": "s-1", "accounts": [], "access": {"valid_until": "2027-05-01T00:00:00Z"}}
        )
    )

    assert enablebanking.start_auth("revolut", "Revolut") == "https://bank.example/a"
    assert store["revolut"] == {"aspsp": "Revolut", "country": "PT"}

    enablebanking.finish_auth("revolut", "https://localhost:8765/callback?state=q&code=C-7")
    assert store["revolut"]["session_id"] == "s-1"
    assert store["revolut"]["valid_until"] == "2027-05-01T00:00:00Z"
    assert store["revolut"]["aspsp"] == "Revolut"
    assert respx.calls[-1].request.read() == b'{"code":"C-7"}'
