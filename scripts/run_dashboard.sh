#!/bin/zsh
# Corrido pelo applet FinancasDashboard.app. Fica à escuta em 127.0.0.1:8501.
set -u
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
exec uv run financas dashboard
