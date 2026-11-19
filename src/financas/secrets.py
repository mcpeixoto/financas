"""Secrets live in the macOS login Keychain (service "financas"), never in files."""
from __future__ import annotations

import getpass
import subprocess

SERVICE = "financas"


def get(name: str) -> str | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return _unhex(r.stdout.rstrip("\n"))


def _unhex(value: str) -> str:
    """`security -w` prints multi-line values as hex; turn them back into text."""
    if len(value) % 2 or not value or any(c not in "0123456789abcdef" for c in value):
        return value
    try:
        decoded = bytes.fromhex(value).decode("utf-8")
    except ValueError:
        return value
    return decoded if "\n" in decoded else value


def set_(name: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value],
        check=True, capture_output=True,
    )


def prompt_and_store(name: str, label: str) -> None:
    """Ask the user in the terminal (hidden input) and save to Keychain."""
    value = getpass.getpass(f"{label}: ").strip()
    if not value:
        raise SystemExit("vazio, nada guardado")
    set_(name, value)
    print(f"guardado no Keychain como {SERVICE}/{name}")
