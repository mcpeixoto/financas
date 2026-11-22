"""Servidor local: localhost, cabeçalhos e o comando financas web."""
import http.client
import json
import re
import sys
import threading
import time
import tomllib

from financas import config
from financas.web.server import criar_servidor


def _pedir(porta, metodo, caminho, corpo=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", porta, timeout=5)
    cabecalhos = {}
    if host is not None:
        cabecalhos["Host"] = host
    if headers:
        cabecalhos.update(headers)
    conn.request(metodo, caminho, body=corpo, headers=cabecalhos)
    resposta = conn.getresponse()
    dados = resposta.read()
    estado = resposta.status
    cabecas = {chave.lower(): valor for chave, valor in resposta.getheaders()}
    conn.close()
    return estado, cabecas, dados


def _servidor(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    httpd = criar_servidor(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_pagina_estaticos_e_api(tmp_path, monkeypatch):
    httpd = _servidor(tmp_path, monkeypatch)
    porta = httpd.server_address[1]
    try:
        estado, _cabecas, corpo = _pedir(porta, "GET", "/")
        assert estado == 200
        texto = corpo.decode("utf-8")
        assert "<html" in texto.lower()
        assert "#resumo" in texto
        assert "/static/app.js" in texto
        assert "/static/plotly.min.js" in texto
        assert "cdn" not in texto.lower()
        assert "http://" not in texto and "https://" not in texto

        estado, _cabecas, js = _pedir(porta, "GET", "/static/app.js")
        assert estado == 200
        assert b"Esconder valores" in js or b"esconder" in js

        estado, cabecas, plotly = _pedir(porta, "GET", "/static/plotly.min.js")
        assert estado == 200
        assert plotly[:1] != b""
        assert "max-age=86400" in cabecas.get("cache-control", "")

        estado, cabecas, resumo = _pedir(porta, "GET", "/api/resumo")
        assert estado == 200
        assert "application/json" in cabecas.get("content-type", "")
        dados = json.loads(resumo)
        assert "patrimonio" in dados
        assert "Traceback" not in resumo.decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_host_errado_e_post_sem_cabecalho_sao_403(tmp_path, monkeypatch):
    httpd = _servidor(tmp_path, monkeypatch)
    porta = httpd.server_address[1]
    try:
        estado, _cabecas, _corpo = _pedir(porta, "GET", "/api/resumo", host="evil.example")
        assert estado == 403
        estado, _cabecas, _corpo = _pedir(
            porta,
            "GET",
            "/api/resumo",
            host=f"localhost:{porta}",
        )
        assert estado == 200
        estado, _cabecas, _corpo = _pedir(
            porta,
            "POST",
            "/api/categoria",
            corpo=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert estado == 403
        estado, _cabecas, _corpo = _pedir(
            porta,
            "POST",
            "/api/categoria",
            corpo=b"{}",
            headers={"Content-Type": "text/plain", "X-Financas": "1"},
        )
        assert estado == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_categoria_grava_regra_e_caminho_com_pontos_e_404(tmp_path, monkeypatch):
    httpd = _servidor(tmp_path, monkeypatch)
    porta = httpd.server_address[1]
    try:
        corpo = json.dumps(
            {"descricao": "LOJA FICTICIA", "categoria": "Compras", "tipo": "gasto"}
        ).encode()
        estado, _cabecas, resposta = _pedir(
            porta,
            "POST",
            "/api/categoria",
            corpo=corpo,
            headers={"Content-Type": "application/json", "X-Financas": "1"},
        )
        assert estado == 200
        assert json.loads(resposta)["ok"] is True
        texto = (tmp_path / "categorias.toml").read_text(encoding="utf-8")
        regra = tomllib.loads(texto)["regra"][0]
        assert regra["categoria"] == "Compras"
        assert regra["tipo"] == "gasto"
        assert regra["padrao"] == re.escape("LOJA FICTICIA")

        estado, _cabecas, _corpo = _pedir(porta, "GET", "/static/../pyproject.toml")
        assert estado == 404
        estado, _cabecas, _corpo = _pedir(porta, "GET", "/static/%2e%2e/pyproject.toml")
        assert estado == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_atualizar_usa_subprocess_falso(tmp_path, monkeypatch):
    from financas.web import api

    api._reiniciar_atualizacao()
    vistos = []
    bloquear = threading.Event()

    class _Proc:
        def __init__(self):
            self.stdout = iter(["pronto\n"])

        def wait(self, timeout=None):
            assert bloquear.wait(2)
            return 0

    def popen(cmd, **_kwargs):
        vistos.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(api.subprocess, "Popen", popen)
    httpd = _servidor(tmp_path, monkeypatch)
    porta = httpd.server_address[1]
    try:
        estado, _cabecas, _corpo = _pedir(
            porta,
            "POST",
            "/api/atualizar",
            corpo=b"{}",
            headers={"Content-Type": "application/json", "X-Financas": "1"},
        )
        assert estado == 200
        estado, _cabecas, segundo = _pedir(
            porta,
            "POST",
            "/api/atualizar",
            corpo=b"{}",
            headers={"Content-Type": "application/json", "X-Financas": "1"},
        )
        assert estado == 200
        assert json.loads(segundo)["a_correr"] is True
        assert vistos == [[sys.executable, "-m", "financas.cli", "run", "--no-notify"]]
        bloquear.set()
        dados = {}
        for _ in range(50):
            estado, _cabecas, corpo = _pedir(porta, "GET", "/api/atualizar")
            assert estado == 200
            dados = json.loads(corpo)
            if not dados["a_correr"]:
                break
            time.sleep(0.05)
        assert dados["a_correr"] is False
        assert dados["ultimo_codigo"] == 0
        assert "pronto" in dados["ultimas_linhas"]
        assert isinstance(dados["fim"], str)
    finally:
        bloquear.set()
        api._reiniciar_atualizacao()
        httpd.shutdown()
        httpd.server_close()


def test_erro_de_api_e_500_sem_stack(tmp_path, monkeypatch):
    httpd = _servidor(tmp_path, monkeypatch)
    porta = httpd.server_address[1]
    try:
        estado, _cabecas, corpo = _pedir(porta, "GET", "/api/gastos?periodo=desconhecido")
        assert estado == 500
        texto = corpo.decode()
        assert "Traceback" not in texto
        assert "erro" in json.loads(texto)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_web_chama_serve(monkeypatch):
    from financas.cli import main
    from financas.web import server

    chamadas = []
    monkeypatch.setattr(server, "serve", lambda porta: chamadas.append(porta))
    assert main(["web", "--porta", "9123"]) == 0
    assert main(["web"]) == 0
    assert chamadas == [9123, 8501]
