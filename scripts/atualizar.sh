#!/bin/zsh
# Corrida manual: vai buscar tudo e atualiza os relatórios em ../Investimentos/.
# Exemplos:
#   scripts/atualizar.sh                      # todas as fontes
#   scripts/atualizar.sh --only etoro,ibkr    # só algumas
#   scripts/atualizar.sh --since 2026-01-01   # movimentos desde uma data
set -eu
cd "$(dirname "$0")/.."
exec /opt/homebrew/bin/uv run --quiet financas run --no-notify "$@"
