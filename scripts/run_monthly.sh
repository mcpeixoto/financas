#!/bin/zsh
# Corrido pelo launchd no dia 2 de cada mês. Logs em data/logs/.
set -u
cd "$(dirname "$0")/.."
mkdir -p data/logs
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
/opt/homebrew/bin/uv run --quiet financas run >> "data/logs/launchd-$(date +%Y-%m).log" 2>&1
