# Provider API spec (verified 2026-11-17)

All access is READ-ONLY. Secrets via `financas.secrets.get(name)`; non-secret config via `financas.config.section(NAME)`.

## Bitcoin (connector `bitcoin`)
- Secret `btc_zpub`: Electrum native-segwit zpub (BIP84 account level, depth 1).
- Derive with embit: `bip32.HDKey.from_string(zpub).derive([chain, i])`, address = `script.p2wpkh(k.key).address(NETWORKS["main"])`. Chains 0 (receive) and 1 (change); stop each chain after 20 consecutive addresses with tx_count == 0.
- `GET https://mempool.space/api/address/{addr}` -> `chain_stats`/`mempool_stats` each with `funded_txo_sum`, `spent_txo_sum` (sats), `tx_count`. Balance = sum(funded - spent) over both.
- Txs: `GET /api/address/{addr}/txs` (each tx: `txid`, `status.block_time`, `vin[].prevout.scriptpubkey_address/value`, `vout[].scriptpubkey_address/value`). Net per tx for the wallet = sum(vout to own addrs) - sum(vin prevout from own addrs). One Transaction per txid (dedupe across addresses), amount in BTC, ext_id = txid.
- Throttle ~0.3 s between calls; retry on 429 with backoff.
- Output: Balance(kind="crypto", currency="BTC"). EUR conversion is done by fx (see below), not by the connector.

## Interactive Brokers Flex Web Service v3 (connector `ibkr`)
- Secrets `ibkr_flex_token`; config `{"query_id": "..."}`.
- `GET https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t=TOKEN&q=QUERY_ID&v=3` -> XML `<FlexStatementResponse><Status>Success</Status><ReferenceCode>...</ReferenceCode>`; on failure `<ErrorCode>`/`<ErrorMessage>`.
- `GET .../GetStatement?t=TOKEN&q=REFERENCE_CODE&v=3` -> FlexQueryResponse XML, or a FlexStatementResponse with ErrorCode 1019 (still generating: sleep 5-10 s, retry up to ~2 min).
- Header `User-Agent: financas/0.1` mandatory. Limits: 1 req/s, 10 req/min.
- Errors: 1012 token expired / 1015 invalid / 1013 IP restriction / 1014 query invalid -> raise NeedsUserAction with Portuguese message. 1018 rate limit, 1001/1004/1009/1021 transient -> retry.
- Parse with ElementTree (no ibflex). Inside `FlexStatement`:
  - `OpenPositions/OpenPosition` attrs: `symbol`, `description`, `position`, `markPrice`, `positionValue`, `currency`, `costBasisMoney`, `fxRateToBase`, `assetCategory`, `levelOfDetail` (use only SUMMARY or missing).
  - `CashReport/CashReportCurrency` attrs `currency`, `endingCash`; use the row with `currency="BASE_SUMMARY"` only if no per-currency rows... simpler: take rows where currency != "BASE_SUMMARY".
  - `Trades/Trade`: `tradeID`, `tradeDate` (YYYYMMDD or YYYY-MM-DD), `symbol`, `buySell`, `quantity`, `tradePrice`, `netCash`, `currency`.
  - `CashTransactions/CashTransaction`: `transactionID`, `dateTime` or `settleDate`, `amount`, `currency`, `description`, `type`.
  - `accountId` attr on FlexStatement -> account label.
- Balances: one Balance(kind="cash") per cash currency; positions -> Position. Transactions from trades (amount=netCash, description "BUY 10 AAPL @ 180") and cash transactions.

## eToro public API (connector `etoro`)
- Secrets `etoro_api_key`, `etoro_user_key` (Read permission, Real environment).
- Base `https://public-api.etoro.com/api/v1`. Headers: `x-api-key`, `x-user-key`, `x-request-id: <uuid4 per request>`.
- `GET /trading/info/real/pnl` -> `clientPortfolio` {`positions`[], `credit`, `unrealizedPnL`, `mirrors`, `orders`}. Position fields: `positionID`, `instrumentID`, `isBuy`, `openRate`, `amount` (invested USD), `units`, `leverage`, `unrealizedPnL{pnL, closeRate, exposureInAccountCurrency}`. Field casing varies between docs and reality: read keys case-insensitively.
- Instrument names: `GET /market-data/instruments?instrumentIds=1,2,3` (response list with `instrumentID`/`instrumentDisplayName`/`symbolFull`); if it fails, fall back to `str(instrumentID)`.
- Value of a position = amount + pnL (USD). cost_basis = amount. Cash Balance = credit (USD).
- Trade history: `GET /trading/info/trade/history?minDate=YYYY-MM-DD&page=N&pageSize=100`, window < 1 year: walk 364-day windows from `since`. Fields `positionId`, `instrumentId`, `isBuy`, `units`, `openRate`, `openTimestamp`, `closeRate`, `closeTimestamp`, `investment`, `fees`, `netProfit`. Emit one Transaction per closed position on closeTimestamp: amount = netProfit, description "Fechou <name> (<units>)", ext_id = f"close-{positionId}".
- Limits 60 req/min reads; on 429 respect `Retry-After`. 401/403 -> NeedsUserAction.

## Enable Banking (connectors `santander`, `revolut`; shared module `enablebanking`)
- Secret `enablebanking_app_id` and `enablebanking_private_key` (PEM text). Config per connector: `{"aspsp": "<exact name>", "country": "PT", "session_id": "...", "valid_until": "ISO"}`.
- JWT RS256, header `{"typ":"JWT","alg":"RS256","kid":APP_ID}`, claims `iss="enablebanking.com"`, `aud="api.enablebanking.com"`, `iat=now`, `exp=now+3600`. `Authorization: Bearer <jwt>`. Base `https://api.enablebanking.com`.
- Setup (interactive, run by the human in a terminal):
  1. `GET /aspsps?country=PT&psu_type=personal` -> `aspsps[]` {`name`, `country`, `maximum_consent_validity` seconds}. Pick by substring ("Santander", "Revolut").
  2. `POST /auth` json `{"access":{"valid_until": now+min(180d, max_validity) RFC3339}, "aspsp":{"name":..,"country":..}, "state": uuid, "redirect_url": REDIRECT, "psu_type":"personal"}` -> `{url, authorization_id}`.
  3. Print/open `url` (webbrowser.open). Human authenticates in bank; bank redirects to REDIRECT?code=...&state=...
  4. Human pastes the full redirect URL into the terminal (input()); parse `code`.
  5. `POST /sessions` json `{"code": code}` -> `{session_id, accounts:[{uid, account_id{iban}, name, currency}], access{valid_until}}`. Save session_id + valid_until in config.
  - REDIRECT comes from config key `enablebanking.redirect_url` (default `https://localhost:8765/callback`; page will fail to load, that is fine, the human copies the URL from the address bar).
- Fetch: `GET /sessions/{session_id}` -> `accounts` (list of uids) and `status`; if status != "AUTHORIZED" or valid_until passed -> NeedsUserAction("Renovar consentimento: financas setup <name>"). Also warn (FetchResult.warnings) when valid_until < 14 days away.
- `GET /accounts/{uid}/balances` -> `balances[]` {`name`, `balance_amount{amount,currency}`, `balance_type`, `reference_date`}. Prefer balance_type in order: "CLBD", "ITAV", "XPCD", "ITBD", "CLAV", else first.
- `GET /accounts/{uid}/transactions?date_from=YYYY-MM-DD[&continuation_key=K]` -> `transactions[]`, `continuation_key` (loop while not null). Fields `entry_reference`, `transaction_amount{amount,currency}`, `credit_debit_indicator` ("DBIT" -> negative, "CRDT" positive), `booking_date`, `value_date`, `status` (keep "BOOK" only), `remittance_information` (list of str), `creditor{name}`, `debtor{name}`. Description = counterparty name + remittance joined. ext_id = entry_reference or sha1 of (booking_date, amount, description, index-in-day).
- Account label: IBAN last 4 masked like "PT50…1234" or account name.

## Trade Republic via pytr (connector `traderepublic`)
- pytr 0.4.10 is installed as a CLI tool by the human (`uv tool install pytr==0.4.10`). Session in `~/.pytr/` created by `pytr login --store_credentials` (human only; needs phone + PIN + app code).
- Fetch runs subprocesses (no shell): `pytr portfolio -o <tmp>/portfolio.csv` and `pytr export_transactions --from_timestamp <since ISO> --format csv --outputdir <tmp>` (find the produced csv in outputdir). Non-zero exit or output containing "login" / "credentials" / "401" -> NeedsUserAction("Trade Republic: corre `pytr login --store_credentials` no Terminal").
- portfolio.csv: `;`-separated? Detect delimiter with csv.Sniffer. Columns include Name, ISIN, quantity, price, avgCost, netValue (names vary by version: match case-insensitively by substring: name, isin, quantity|shares, avgcost|average, netvalue|value). Decimals may use comma.
- transactions csv: columns Date/Datum, Type, Value/Amount, Note/Description, ISIN, Shares, Fees... match case-insensitively; ext_id = sha1(date|type|value|note|row index within same date).
- Cash: if a row/field for cash isn't in portfolio, leave cash out and add warning.

## Certificados de Aforro (connector `aforro`)
- No API. Value is COMPUTED from config `aforro.subscricoes`: list of `{"serie": "F", "data": "YYYY-MM-DD", "valor": 10000}` plus optional `{"valor_atual_conhecido": X, "data": ...}` checkpoint to reconcile.
- Série F: base rate E3 = average 3M Euribor of 10 business days before the third-to-last business day of the previous month, rounded to 3 decimals, floor 0, cap 2.500%. Rate for a quarter = base rate in force on the quarter start date + permanence premium: year1 0, years 2-5 +0.25, 6-9 +0.50, 10-11 +1.00, 12-13 +1.50, 14-15 +1.75 (pp).
- Interest paid quarterly on the subscription day-of-month (if missing that day, 1st of next month), capitalized NET of 28% IRS: value += value * rate/4 * 0.72.
- Série E: base E3 + 1.00 pp, cap 3.50%, premium years 2-5 +0.50, 6-10 +1.00.
- Euribor daily source (Bundesbank, CSV `;` separator, comma decimals, header rows to skip; data rows look like `2026-11-16;2,779;`):
  `https://api.statistiken.bundesbank.de/rest/data/BBIG1/D.D0.EUR.MMKT.EURIBOR.M03.BID._Z?startPeriod=YYYY-MM-DD&format=csv`
  Cache fetched series in `data/euribor3m.csv`.
- Output: Balance(kind="savings", currency="EUR", account="Série F 2024-03-12") per subscription; Transaction per capitalization date >= since (description "Juros líquidos CA").

## FX (`financas.fx`)
- ECB daily: `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` (`<Cube currency="USD" rate="1.0989"/>`, EUR = amount / rate).
- BTC->EUR: `https://mempool.space/api/v1/prices` -> `{"EUR": 85000, ...}`.
