# financas: património pessoal automático (Portugal)

Vai buscar, **só em leitura**, os saldos, as posições e os movimentos de contas portuguesas e de corretoras. Grava tudo numa base SQLite local e escreve relatórios em Markdown e em Excel. Pode correr sozinho todos os meses através do launchd, no macOS.

Fontes suportadas:

| Fonte | Como |
|---|---|
| Santander Totta, Revolut (e outros bancos PSD2) | [Enable Banking](https://enablebanking.com) (PSD2/AIS) |
| Interactive Brokers | Flex Web Service v3 |
| eToro | API pública (chave de utilizador só de leitura) |
| Trade Republic | [`pytr`](https://github.com/pytr-org/pytr) |
| Certificados de Aforro (Série E/F) | calculados a partir das subscrições e da Euribor 3M do Bundesbank |
| Bitcoin (cold wallet) | zpub, com derivação local ([embit](https://github.com/diybitcoinhardware/embit)) e saldos no mempool.space |

Não há servidores nem cloud: os dados ficam no teu Mac e os segredos no Keychain do macOS.

## Instalar

```bash
git clone https://github.com/mcpeixoto/financas && cd financas
uv sync
uv run financas status
```

Para cada fonte: `uv run financas setup <fonte>`. A especificação de cada API está em [docs/APIS.md](docs/APIS.md).

## Uso rápido

| O quê | Comando |
|---|---|
| Atualizar tudo agora | `scripts/atualizar.sh` |
| Só algumas fontes | `scripts/atualizar.sh --only etoro,ibkr` |
| Movimentos desde uma data | `scripts/atualizar.sh --since 2026-01-01` |
| Estado (segredos, consentimentos, última corrida) | `uv run financas status` |
| (Re)configurar uma fonte | `uv run financas setup <fonte>` |
| Relatório da semana anterior | `uv run financas relatorio --semanal` |
| Relatório do mês anterior | `uv run financas relatorio --mensal` |
| Os mesmos, com euros | `uv run financas relatorio --semanal --com-valores` |
| Testes | `uv run pytest -q` |

Por omissão, `--since` é o dia 1 do mês anterior. Os movimentos não se duplicam (a chave é `(source, ext_id)`).

## Dashboard

O dashboard local mostra o património, os gastos, os investimentos e os movimentos. Fica só em `http://127.0.0.1:8501`.

```bash
uv run financas dashboard
uv run financas dashboard --porta 8502
```

Na barra lateral, «Esconder valores» vem ligado: os gráficos passam a percentagens e a índices com base 100, e nenhum montante em euros aparece. Desliga-o para ver os valores.

Há quatro páginas:

- **Resumo** — património, variação desde o snapshot anterior e há 30 dias, alocação e avisos da última corrida;
- **Gastos** — categorias, comerciantes, subscrições, dia da semana e taxa de poupança;
- **Investimentos** — depositado, valor, ganho e XIRR por plataforma, contra o MSCI World (IWDA.AS);
- **Movimentos** — filtros e correção de categoria, que grava uma regra no início de `data/categorias.toml`.

Para o deixar sempre a correr no macOS, o launchd abre um applet. O TCC não deixa o agente ler `~/Documents` diretamente.

1. Criar o applet: `osacompile -o ~/Applications/FinancasDashboard.app -e 'do shell script "/bin/zsh <repo>/scripts/run_dashboard.sh"'`.
2. Copiar `scripts/local.financas.dashboard.plist` para `~/Library/LaunchAgents/` e trocar `__HOME__` pelo teu `$HOME`.
3. Carregar: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.financas.dashboard.plist`.

## Página web

A página em `http://127.0.0.1:8501` responde a três perguntas: quanto tenho e onde, se sou bom investidor, e para onde vai o dinheiro. Não usa o Streamlit, não precisa de build e funciona offline.

```bash
uv run financas web
uv run financas web --porta 8502
```

Há seis separadores: Resumo, Investimentos, Posições, Gastos, Movimentos e Estado. «Esconder valores» vem ligado: só aparecem percentagens, pesos e índices com base 100. «Atualizar agora» corre `financas run --no-notify` em segundo plano. Corrigir a categoria de um movimento grava uma regra no início de `data/categorias.toml`.

O dashboard Streamlit continua disponível com `uv run financas dashboard`.

## Dados privados

Tudo o que é pessoal fica em `data/`, que está no `.gitignore`:

- `data/financas.db`: a base SQLite;
- `data/config.json`: sessões, ids e subscrições;
- `data/logs/`: os logs das corridas;
- `data/NOTAS.md`: notas tuas, como as contas ligadas.

Os segredos vão para o Keychain (serviço `financas`) e nunca para ficheiros.

Os relatórios vão para `../Investimentos/` (`AAAA-MM-DD_Patrimonio_auto.md` e `Patrimonio_historico.xlsx`).

## Fontes: segredos e renovação

| Fonte | Segredos no Keychain | Config | Renovação |
|---|---|---|---|
| `santander`, `revolut` | `enablebanking_app_id`, `enablebanking_private_key` | `<banco>.session_id`, `valid_until`, `aspsp` | consentimento de 180 dias: `financas setup <banco>` |
| `ibkr` | `ibkr_flex_token` | `ibkr.query_id` | gerar um token novo no portal (Settings → Flex Web Service) |
| `etoro` | `etoro_api_key`, `etoro_user_key` | `etoro.depositos` = `[{data, valor_eur}]` | só se a chave for revogada |
| `traderepublic` | sessão em `~/.pytr/` | — | `pytr login --store_credentials --v2` (confirmar na app) |
| `aforro` | — | `aforro.subscricoes` = `[{serie, data, valor}]` | quando houver subscrições novas |
| `bitcoin` | `btc_zpub` | — | nunca |

A API pública da eToro não devolve depósitos. Em `etoro.depositos`, cada entrada é `{"data": "AAAA-MM-DD", "valor_eur": 1000}`; um valor negativo é um levantamento. Sem esta lista, a rentabilidade do eToro não pode ser calculada.

Enable Banking:

- A app fica em modo restrito e só lê as contas ligadas no Control Panel.
- O setup abre o banco no browser. No fim, a página de retorno (`https://localhost:8765/callback?code=…`) não carrega, e isso é normal: cola o URL no terminal.
- Versão não interativa: `financas setup santander --start` e depois `financas setup santander --finish '<URL>'`.
- O PSD2 do Revolut só mostra as contas correntes. Cofres e Poupanças ficam de fora.

## Agendamento (macOS)

O TCC não deixa o launchd ler `~/Documents` diretamente. Por isso o agente abre um applet que corre `scripts/run_monthly.sh`.

1. Criar o applet: `osacompile -o ~/Applications/Financas.app -e 'do shell script "/bin/zsh <repo>/scripts/run_monthly.sh"'`.
2. Copiar `scripts/local.financas.plist` para `~/Library/LaunchAgents/` e trocar `__HOME__` pelo teu `$HOME`.
3. Carregar: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.financas.plist`.
4. Testar: `launchctl kickstart -k gui/$(id -u)/local.financas`.

O relatório da semana anterior corre à segunda-feira, às 09:00. O launchd abre um applet, como no relatório mensal.

1. Criar o applet: `osacompile -o ~/Applications/FinancasSemanal.app -e 'do shell script "/bin/zsh <repo>/scripts/run_semanal.sh"'`.
2. Copiar `scripts/local.financas.semanal.plist` para `~/Library/LaunchAgents/` e trocar `__HOME__` pelo teu `$HOME`.
3. Carregar: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.financas.semanal.plist`.

`scripts/run_semanal.sh` atualiza o Santander e o Revolut (`financas run --only santander,revolut --no-notify`) e gera o HTML da semana (`financas relatorio --semanal`). No dia 2, `scripts/run_monthly.sh` gera também o relatório do mês anterior (`financas relatorio --mensal`). Os ficheiros ficam em `../Investimentos/Relatorios/`.

## Quando uma fonte falha

As outras fontes correm na mesma. A que falhou fica com estado `precisa de ti: …` ou `erro: …`, com o comando a correr.

## Código

```
src/financas/
  models.py      Balance, Position, Transaction, FetchResult, NeedsUserAction
  secrets.py     Keychain (security CLI)
  config.py      caminhos e data/config.json
  store.py       SQLite
  fx.py          BCE + BTC
  report.py      Markdown + Excel
  cli.py         financas run | setup | status | relatorio
  relatorios.py  HTML semanal e mensal
  connectors/    um módulo por fonte: NAME, SECRETS, fetch(since) -> FetchResult, setup() opcional
```

Para acrescentar uma fonte nova:

1. Criar `connectors/<nome>.py` com `NAME`, `SECRETS` e `fetch(since)`.
2. Registá-la em `cli.CONNECTORS`, em `report.CONNECTOR_ORDER` e em `DISPLAY`.
3. Escrever testes com `respx`. Os testes nunca chamam APIs reais.

Regras:

- Só leitura.
- Nunca imprimir segredos.
- Nunca pôr dados pessoais no repositório.

## Licença

MIT
