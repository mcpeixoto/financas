"""Servidor HTTP local. Só escuta em 127.0.0.1."""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from financas.web import api

PASTA = Path(__file__).resolve().parent / "static"
_PLOTLY: bytes | None = None


class Servidor(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def caminho_plotly() -> Path:
    spec = importlib.util.find_spec("plotly")
    if spec is None or not spec.origin:
        raise FileNotFoundError("plotly")
    caminho = Path(spec.origin).resolve().parent / "package_data" / "plotly.min.js"
    if not caminho.is_file():
        raise FileNotFoundError(str(caminho))
    return caminho


def _bytes_plotly() -> bytes:
    global _PLOTLY
    if _PLOTLY is None:
        _PLOTLY = caminho_plotly().read_bytes()
    return _PLOTLY


def _normalizar(valor):
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, dict):
        return {str(chave): _normalizar(item) for chave, item in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_normalizar(item) for item in valor]
    return valor


class Tratador(BaseHTTPRequestHandler):
    server_version = "Financas/1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        try:
            self._get()
        except Exception as exc:
            self._json(500, {"erro": str(exc) or "erro interno"})

    def do_POST(self) -> None:
        try:
            self._post()
        except Exception as exc:
            self._json(500, {"erro": str(exc) or "erro interno"})

    def _get(self) -> None:
        if not self._host_ok():
            self._json(403, {"erro": "host não permitido"})
            return
        caminho = self._caminho()
        if self._proibido(caminho):
            self._json(404, {"erro": "não encontrado"})
            return
        if caminho == "/":
            self._ficheiro(PASTA / "index.html", "text/html; charset=utf-8")
            return
        if caminho.startswith("/static/"):
            self._estatico(caminho[len("/static/") :])
            return
        if caminho.startswith("/api/"):
            nome = caminho[len("/api/") :]
            if not nome or "/" in nome:
                self._json(404, {"erro": "não encontrado"})
                return
            self._api(nome, api.GET, self._params())
            return
        self._json(404, {"erro": "não encontrado"})

    def _post(self) -> None:
        if not self._host_ok():
            self._drenar()
            self._json(403, {"erro": "host não permitido"})
            return
        if not self._post_ok():
            self._drenar()
            self._json(403, {"erro": "pedido recusado"})
            return
        caminho = self._caminho()
        if self._proibido(caminho) or not caminho.startswith("/api/"):
            self._drenar()
            self._json(404, {"erro": "não encontrado"})
            return
        nome = caminho[len("/api/") :]
        funcao = api.POST.get(nome)
        if funcao is None or "/" in nome:
            self._drenar()
            self._json(404, {"erro": "não encontrado"})
            return
        try:
            corpo = self._corpo()
        except ValueError as exc:
            self._json(500, {"erro": str(exc)})
            return
        self._api(nome, {nome: funcao}, corpo)

    def _api(self, nome: str, rotas: dict, params: dict) -> None:
        funcao = rotas.get(nome)
        if funcao is None:
            self._json(404, {"erro": "não encontrado"})
            return
        from financas import config, store

        db = store.connect(config.DATA / "financas.db")
        try:
            dados = funcao(db, params)
        finally:
            db.close()
        self._json(200, dados)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        porta = self.server.server_address[1]
        return host in {f"127.0.0.1:{porta}", f"localhost:{porta}"}

    def _post_ok(self) -> bool:
        marca = (self.headers.get("X-Financas") or "").strip()
        if marca != "1":
            return False
        tipo = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return tipo == "application/json"

    def _caminho(self) -> str:
        return unquote(urlsplit(self.path).path)

    def _proibido(self, caminho: str) -> bool:
        return any(parte == ".." for parte in caminho.split("/"))

    def _params(self) -> dict:
        consulta = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
        return {chave: valores[0] for chave, valores in consulta.items() if valores}

    def _corpo(self) -> dict:
        try:
            tamanho = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("corpo inválido") from exc
        if tamanho < 0 or tamanho > 1_000_000:
            raise ValueError("corpo inválido")
        bruto = self.rfile.read(tamanho) if tamanho else b""
        if not bruto:
            return {}
        try:
            dados = json.loads(bruto.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON inválido") from exc
        if not isinstance(dados, dict):
            raise ValueError("JSON inválido")
        return dados

    def _drenar(self) -> None:
        try:
            tamanho = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < tamanho <= 1_000_000:
            self.rfile.read(tamanho)

    def _estatico(self, nome: str) -> None:
        if not nome or "/" in nome or "\\" in nome:
            self._json(404, {"erro": "não encontrado"})
            return
        if nome == "plotly.min.js":
            extra = {"Cache-Control": "max-age=86400"}
            self._enviar(200, _bytes_plotly(), "text/javascript; charset=utf-8", extra)
            return
        if nome not in {"app.js", "app.css"}:
            self._json(404, {"erro": "não encontrado"})
            return
        tipo = "text/javascript; charset=utf-8" if nome.endswith(".js") else "text/css; charset=utf-8"
        self._ficheiro(PASTA / nome, tipo)

    def _ficheiro(self, caminho: Path, tipo: str) -> None:
        base = PASTA.resolve()
        destino = caminho.resolve()
        if not destino.is_relative_to(base) or not destino.is_file():
            self._json(404, {"erro": "não encontrado"})
            return
        self._enviar(200, destino.read_bytes(), tipo)

    def _json(self, codigo: int, dados) -> None:
        corpo = json.dumps(_normalizar(dados), ensure_ascii=False).encode("utf-8")
        self._enviar(codigo, corpo, "application/json; charset=utf-8")

    def _enviar(self, codigo: int, corpo: bytes, tipo: str, extra: dict | None = None) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        cache = "no-store"
        if extra and "Cache-Control" in extra:
            cache = extra["Cache-Control"]
        self.send_header("Cache-Control", cache)
        if extra:
            for chave, valor in extra.items():
                if chave != "Cache-Control":
                    self.send_header(chave, valor)
        self.end_headers()
        self.wfile.write(corpo)


def criar_servidor(porta: int) -> ThreadingHTTPServer:
    return Servidor(("127.0.0.1", int(porta)), Tratador)


def serve(porta: int) -> None:
    servidor = criar_servidor(porta)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
