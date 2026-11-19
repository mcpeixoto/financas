#!/bin/zsh
# Corrido pelo launchd à segunda-feira, às 09:00. Logs em data/logs/.
set -u
cd "$(dirname "$0")/.."
mkdir -p data/logs
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
/opt/homebrew/bin/uv run --quiet financas run --only santander,revolut --no-notify >> "data/logs/launchd-semanal-$(date +%Y-W%V).log" 2>&1
/opt/homebrew/bin/uv run --quiet financas relatorio --semanal >> "data/logs/launchd-semanal-$(date +%Y-W%V).log" 2>&1
