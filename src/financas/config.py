"""Non-secret settings in data/config.json (gitignored). Paths are fixed here."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # .../Automacao
DATA = ROOT / "data"
CONTABILIDADE = ROOT.parent                         # .../Contabilidade
CONFIG_FILE = DATA / "config.json"


def load() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save(cfg: dict) -> None:
    DATA.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False, default=str))


def section(name: str) -> dict:
    return load().get(name, {})


def update_section(name: str, values: dict) -> None:
    cfg = load()
    cfg.setdefault(name, {}).update(values)
    save(cfg)
