"""Certificados de Aforro (séries E e F). O valor é calculado, não vem de uma API."""
from __future__ import annotations

import calendar
import csv
import hashlib
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO

import httpx

from financas import config
from financas.models import Balance, FetchResult, NeedsUserAction, Transaction

NAME = "aforro"
SECRETS: dict[str, str] = {}

EURIBOR_URL = (
    "https://api.statistiken.bundesbank.de/rest/data/"
    "BBIG1/D.D0.EUR.MMKT.EURIBOR.M03.BID._Z"
)
_CACHE = "euribor3m.csv"
_NET = Decimal("0.72")
_HOLIDAYS: dict[int, frozenset[date]] = {}

# Prémio de permanência em pontos percentuais.
_PREMIUM_F: tuple[tuple[int, int, str], ...] = (
    (1, 1, "0"),
    (2, 5, "0.25"),
    (6, 9, "0.50"),
    (10, 11, "1.00"),
    (12, 13, "1.50"),
    (14, 15, "1.75"),
)
_PREMIUM_E: tuple[tuple[int, int, str], ...] = (
    (2, 5, "0.50"),
    (6, 10, "1.00"),
)


def business_days(start: date, end: date) -> list[date]:
    """Dias úteis TARGET em [start, end], inclusive."""
    if end < start:
        return []
    days: list[date] = []
    current = start
    while current <= end:
        if _is_business_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def fixing_date(year: int, month: int) -> date:
    """Antepenúltimo dia útil TARGET do mês (dia em que a taxa base é fixada)."""
    last = calendar.monthrange(year, month)[1]
    days = business_days(date(year, month, 1), date(year, month, last))
    if len(days) < 3:
        raise ValueError(f"O mês {year:04d}-{month:02d} não tem três dias úteis TARGET.")
    return days[-3]


def base_rate_f(month_start: date, euribor_series) -> Decimal:
    """Taxa base da Série F em vigor no mês de `month_start`, em fração (2,5% -> 0.025)."""
    percent = min(_e3_percent(month_start, euribor_series), Decimal("2.500"))
    return percent / Decimal("100")


def premium_f(years_held: int) -> Decimal:
    """Prémio de permanência da Série F, em fração (0,25 p.p. -> 0.0025)."""
    return _premium_pp(years_held, _PREMIUM_F) / Decimal("100")


def interest_dates(sub_date: date, until: date) -> list[date]:
    """Datas de capitalização trimestral até `until`, inclusive.

    O vencimento cai no dia da subscrição. Se esse dia não existir no mês,
    passa para o dia 1 do mês seguinte.
    """
    dates: list[date] = []
    for quarter in range(1, 121):
        pay = _add_months(sub_date, 3 * quarter)
        if pay > until:
            break
        if dates and pay <= dates[-1]:
            break
        dates.append(pay)
    return dates


def value_at(sub, on_date, euribor_series) -> tuple[Decimal, list[tuple[date, Decimal]]]:
    """Valor capitalizado e lista (data, juro líquido) até `on_date`."""
    on = _as_date(on_date)
    sub_date = _as_date(sub["data"])
    serie = _serie(sub)
    value = _as_decimal(sub["valor"])
    known_date, known_value = _checkpoint(sub)
    snap = known_value is not None and known_date is not None and known_date <= on
    events: list[tuple[date, Decimal]] = []
    quarter_start = sub_date
    max_year = 10 if serie == "E" else 15
    for index, pay in enumerate(interest_dates(sub_date, on)):
        years = index // 4 + 1
        if years > max_year:
            break
        if snap and known_date is not None and known_date < pay:
            value = known_value  # type: ignore[assignment]
            snap = False
        rate = _quarter_rate(serie, quarter_start, years, euribor_series)
        net = value * rate / Decimal(4) * _NET
        value += net
        events.append((pay, net))
        quarter_start = pay
    if snap and known_value is not None:
        value = known_value
    return value, events


def parse_bundesbank_csv(text: str) -> dict[date, Decimal]:
    """Série diária do Bundesbank. Ignora cabeçalhos; decimais com vírgula."""
    if not text.strip():
        return {}
    delimiter = ";" if ";" in text else ","
    parsed: dict[date, Decimal] = {}
    for row in csv.reader(StringIO(text), delimiter=delimiter):
        if not row:
            continue
        cell = row[0].strip().strip('"').replace("\ufeff", "")
        if len(cell) < 10:
            continue
        try:
            observed = date.fromisoformat(cell[:10])
        except ValueError:
            continue
        if len(row) < 2:
            continue
        raw = row[1].strip().strip('"')
        if not raw or raw in {".", "-", "–"}:
            continue
        try:
            parsed[observed] = _parse_decimal(raw)
        except (ValueError, ArithmeticError):
            continue
    return parsed


def load_euribor(today: date | None = None) -> dict[date, Decimal]:
    """Lê a cache e, se o último dia for anterior a hoje-3, pede o CSV ao Bundesbank."""
    today = today or _today()
    path = config.DATA / _CACHE
    series = parse_bundesbank_csv(path.read_text(encoding="utf-8")) if path.exists() else {}
    last = max(series) if series else None
    if last is not None and last >= today - timedelta(days=3):
        return series
    start = (last + timedelta(days=1)) if last else date(1999, 1, 4)
    url = f"{EURIBOR_URL}?startPeriod={start.isoformat()}&format=csv"
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(url)
            response.raise_for_status()
        fresh = parse_bundesbank_csv(response.text)
    except httpx.HTTPError:
        if series:
            return series
        raise NeedsUserAction(
            "Não foi possível obter a Euribor a 3 meses no Bundesbank."
        ) from None
    series.update(fresh)
    _write_cache(path, series)
    return series


def fetch(since: date) -> FetchResult:
    subs = _subscriptions()
    if not subs:
        raise NeedsUserAction(
            "Sem subscrições de Certificados de Aforro. Corre: financas setup aforro"
        )
    today = _today()
    series = load_euribor(today=today)
    result = FetchResult(source=NAME)
    for sub in subs:
        value, events = value_at(sub, today, series)
        sub_date = _as_date(sub["data"])
        account = f"Série {_serie(sub)} {sub_date.isoformat()}"
        known = sub.get("valor_atual_conhecido")
        if known is not None and sub.get("data_conhecida") in (None, ""):
            known_dec = _as_decimal(known)
            if abs(known_dec - value) > Decimal("0.01"):
                result.warnings.append(
                    f"Certificados de Aforro {account}: valor calculado {value} "
                    f"difere do valor conhecido {known_dec}."
                )
        result.balances.append(
            Balance(
                source=NAME,
                account=account,
                currency="EUR",
                amount=value,
                kind="savings",
            )
        )
        for pay, net in events:
            if pay < since:
                continue
            ext_id = hashlib.sha1(
                f"aforro|{account}|{pay.isoformat()}|{net}".encode()
            ).hexdigest()
            result.transactions.append(
                Transaction(
                    source=NAME,
                    account=account,
                    booked=pay,
                    amount=net,
                    currency="EUR",
                    description="Juros líquidos CA",
                    ext_id=ext_id,
                )
            )
    return result


def setup() -> None:
    print("Certificados de Aforro. Introduz cada subscrição (Enter na série para terminar).")
    subs: list[dict] = []
    while True:
        serie_raw = input("Série (E ou F; Enter para terminar): ").strip()
        if not serie_raw:
            break
        serie = _normalize_serie_input(serie_raw)
        if serie not in {"E", "F"}:
            print("Série inválida. Indica E ou F.")
            continue
        while True:
            data = input("Data de subscrição (AAAA-MM-DD): ").strip()
            try:
                date.fromisoformat(data)
                break
            except ValueError:
                print("Data inválida. Usa AAAA-MM-DD.")
        while True:
            valor_raw = input("Valor subscrito (EUR): ").strip()
            try:
                valor = _parse_decimal(valor_raw)
                break
            except (ValueError, ArithmeticError):
                print("Valor inválido. Usa um número, por exemplo 10000 ou 10.000,50.")
        stored: int | str
        if valor == valor.to_integral_value():
            stored = int(valor)
        else:
            stored = format(valor, "f")
        subs.append({"serie": serie, "data": data, "valor": stored})
        print(f"Subscrição Série {serie} de {data} registada.")
    config.update_section("aforro", {"subscricoes": subs})
    if len(subs) == 1:
        print("1 subscrição gravada.")
    else:
        print(f"{len(subs)} subscrições gravadas.")


def _today() -> date:
    return date.today()


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _holidays(year: int) -> frozenset[date]:
    cached = _HOLIDAYS.get(year)
    if cached is None:
        easter = _easter_sunday(year)
        cached = frozenset(
            {
                date(year, 1, 1),
                easter - timedelta(days=2),
                easter + timedelta(days=1),
                date(year, 5, 1),
                date(year, 12, 25),
                date(year, 12, 26),
            }
        )
        _HOLIDAYS[year] = cached
    return cached


def _is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _holidays(day.year)


def _e3_percent(month_start: date, euribor_series) -> Decimal:
    """Média Euribor 3M dos 10 dias úteis anteriores ao fixing, em percentagem."""
    previous = month_start.replace(day=1) - timedelta(days=1)
    fixing = fixing_date(previous.year, previous.month)
    window: list[date] = []
    cursor = fixing - timedelta(days=1)
    while len(window) < 10 and (fixing - cursor).days < 40:
        if _is_business_day(cursor):
            window.append(cursor)
        cursor -= timedelta(days=1)
    values = [
        _as_decimal(euribor_series[day])
        for day in window
        if day in euribor_series and euribor_series[day] is not None
    ]
    if not values:
        return Decimal("0.000")
    average = sum(values, start=Decimal("0")) / Decimal(len(values))
    rounded = average.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if rounded < 0:
        return Decimal("0.000")
    return rounded


def _premium_pp(years_held: int, table: tuple[tuple[int, int, str], ...]) -> Decimal:
    for low, high, points in table:
        if low <= years_held <= high:
            return Decimal(points)
    return Decimal("0")


def _base_rate_e(month_start: date, euribor_series) -> Decimal:
    percent = _e3_percent(month_start, euribor_series) + Decimal("1.000")
    if percent < 0:
        percent = Decimal("0")
    percent = min(percent, Decimal("3.500"))
    return percent / Decimal("100")


def _quarter_rate(serie: str, quarter_start: date, years: int, euribor_series) -> Decimal:
    if serie == "E":
        return _base_rate_e(quarter_start, euribor_series) + (
            _premium_pp(years, _PREMIUM_E) / Decimal("100")
        )
    return base_rate_f(quarter_start, euribor_series) + premium_f(years)


def _add_months(day: date, months: int) -> date:
    cursor = day.month - 1 + months
    year = day.year + cursor // 12
    month = cursor % 12 + 1
    last = calendar.monthrange(year, month)[1]
    if day.day <= last:
        return date(year, month, day.day)
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def _serie(sub) -> str:
    raw = str(sub.get("serie", "F"))
    normalized = _normalize_serie_input(raw)
    return "E" if normalized == "E" else "F"


def _normalize_serie_input(raw: str) -> str:
    return raw.strip().upper().replace("SÉRIE", "").replace("SERIE", "").strip()


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("valor inválido")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return _parse_decimal(str(value))


def _parse_decimal(raw: str) -> Decimal:
    text = raw.strip().replace("\u00a0", "").replace(" ", "").replace("€", "").replace("%", "")
    if not text or text in {".", ",", "-", "+"}:
        raise ValueError(f"número inválido: {raw!r}")
    neg = text[0] == "-"
    if text[0] in "+-":
        text = text[1:]
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    value = Decimal(text)
    return -value if neg else value


def _checkpoint(sub) -> tuple[date | None, Decimal | None]:
    if sub.get("valor_atual_conhecido") in (None, "") or not sub.get("data_conhecida"):
        return None, None
    return _as_date(sub["data_conhecida"]), _as_decimal(sub["valor_atual_conhecido"])


def _subscriptions() -> list[dict]:
    raw = config.section("aforro").get("subscricoes") or []
    return [
        item
        for item in raw
        if isinstance(item, dict) and "valor" in item and "data" in item
    ]


def _write_cache(path, series: dict[date, Decimal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for observed in sorted(series):
        rendered = format(series[observed], "f").rstrip("0").rstrip(".")
        lines.append(f"{observed.isoformat()};{rendered.replace('.', ',')}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
