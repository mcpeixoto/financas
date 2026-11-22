"use strict";

const SEPARADORES = ["resumo", "investimentos", "posicoes", "gastos", "movimentos", "estado"];
const CHAVE_ESCONDER = "financas.esconder";
const CFG = { displayModeBar: false, responsive: true };
const COR_CAPITAL = "#2a78d6";
const COR_VALOR = "#eb6834";
const COR_REALIZADO = "#1baf7a";

const memoria = new Map();
const ordemTabelas = { posicoes: { chave: "peso", dir: -1 } };
const graficos = [];
const gastosEstado = { periodo: "este mês", inicio: "", fim: "", freq: "mes" };
const movEstado = { texto: "", fonte: "", categoria: "", tipo: "", inicio: "", fim: "" };
let geracao = 0;
let plataformaEscolhida = "Total";
let filtroPosicao = "";
let timerPesquisa = 0;
let timerAtualizar = 0;

function h(tag, props) {
  const no = document.createElement(tag);
  if (props) {
    Object.keys(props).forEach((chave) => {
      const valor = props[chave];
      if (valor == null || valor === false) return;
      if (chave === "class") no.className = valor;
      else if (chave === "text") no.textContent = String(valor);
      else if (chave === "title") no.title = String(valor);
      else if (chave.slice(0, 2) === "on" && typeof valor === "function") no.addEventListener(chave.slice(2), valor);
      else no.setAttribute(chave, String(valor));
    });
  }
  for (let i = 2; i < arguments.length; i += 1) acrescentar(no, arguments[i]);
  return no;
}

function acrescentar(no, kid) {
  if (kid == null || kid === false) return;
  if (Array.isArray(kid)) {
    kid.forEach((item) => acrescentar(no, item));
    return;
  }
  no.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
}

function esconderLigado() {
  const caixa = document.getElementById("esconder");
  return !caixa || caixa.checked;
}

function lerEsconder() {
  try {
    const valor = localStorage.getItem(CHAVE_ESCONDER);
    if (valor === null) return true;
    return valor !== "0";
  } catch (exc) {
    return true;
  }
}

function guardarEsconder(ligado) {
  try {
    localStorage.setItem(CHAVE_ESCONDER, ligado ? "1" : "0");
  } catch (exc) {
    /* o modo continua a valer nesta página */
  }
}

function fmtNum(valor, casas) {
  const numero = Number(valor);
  if (!Number.isFinite(numero)) return "—";
  const negativo = numero < 0;
  const [inteiro, fraccao] = Math.abs(numero).toFixed(casas).split(".");
  const grupos = inteiro.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  const corpo = casas ? grupos + "," + fraccao : grupos;
  return (negativo ? "-" : "") + corpo;
}

function fmtEur(valor, moeda) {
  if (valor == null || !Number.isFinite(Number(valor))) return "—";
  const numero = fmtNum(valor, 2);
  if (!moeda || moeda === "EUR") return numero + " €";
  return numero + " " + moeda;
}

function montante(valor, moeda) {
  if (esconderLigado()) return "—";
  return fmtEur(valor, moeda);
}

function fmtPct(razao) {
  if (razao == null || !Number.isFinite(Number(razao))) return "—";
  return fmtNum(Number(razao) * 100, 1) + "%";
}

function fmtIndice(valor) {
  if (valor == null || !Number.isFinite(Number(valor))) return "—";
  return fmtNum(valor, 1);
}

function isoLocal(data) {
  const mes = String(data.getMonth() + 1).padStart(2, "0");
  const dia = String(data.getDate()).padStart(2, "0");
  return data.getFullYear() + "-" + mes + "-" + dia;
}

function fmtData(iso) {
  const texto = String(iso || "");
  const partes = texto.slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!partes) return "—";
  return partes[3] + "/" + partes[2] + "/" + partes[1];
}

function fmtPeriodo(chave) {
  const mes = String(chave || "").match(/^(\d{4})-(\d{2})$/);
  if (mes) return mes[2] + "/" + mes[1];
  const semana = String(chave || "").match(/^(\d{4})-W(\d{2})$/);
  if (semana) return "sem. " + Number(semana[2]) + "/" + semana[1];
  return String(chave || "");
}

function vazio(texto) {
  return h("p", { class: "vazio", text: texto || "Sem dados" });
}

function seccao(titulo) {
  const no = h("section", { class: "seccao" });
  no.append(h("h2", { text: titulo }));
  for (let i = 1; i < arguments.length; i += 1) acrescentar(no, arguments[i]);
  return no;
}

function cartao(rotulo, valor, nota) {
  return h(
    "article",
    { class: "cartao" },
    h("p", { class: "rotulo", text: rotulo }),
    h("p", { class: "valor", text: valor }),
    nota ? h("p", { class: "nota", text: nota }) : null,
  );
}

function cartoes(lista) {
  return h("div", { class: "cartoes" }, lista);
}

const ETIQUETAS = {
  bateu: ["▲", "Bateu o índice"],
  ficou_atras: ["▼", "Ficou atrás"],
  empatou: ["=", "Empatou"],
  sem_dados: ["–", "Sem dados"],
};

function etiqueta(id) {
  const par = ETIQUETAS[id] || ETIQUETAS.sem_dados;
  return h(
    "span",
    { class: "etiqueta " + (ETIQUETAS[id] ? id : "sem_dados") },
    h("span", { "aria-hidden": "true", text: par[0] }),
    " " + par[1],
  );
}

function amostra(cor) {
  const no = h("span", { class: "amostra", "aria-hidden": "true" });
  if (cor) no.style.background = cor;
  return no;
}

function separadorAtual() {
  const nome = (location.hash || "#resumo").slice(1);
  return SEPARADORES.indexOf(nome) === -1 ? "resumo" : nome;
}

function marcarNav() {
  const atual = separadorAtual();
  document.querySelectorAll("nav a").forEach((ligacao) => {
    const ativo = ligacao.getAttribute("href") === "#" + atual;
    ligacao.classList.toggle("ativo", ativo);
    if (ativo) ligacao.setAttribute("aria-current", "page");
    else ligacao.removeAttribute("aria-current");
  });
}

function limparGraficos() {
  graficos.splice(0).forEach((no) => {
    try {
      Plotly.purge(no);
    } catch (exc) {
      /* o nó já saiu */
    }
  });
}

function corTexto() {
  return getComputedStyle(document.body).getPropertyValue("--texto").trim() || "#222";
}

function layoutBase(eixo, sufixo) {
  const cor = corTexto();
  const eixoY = {
    gridcolor: "rgba(128,128,128,0.18)",
    zeroline: false,
    tickfont: { color: cor },
    ticksuffix: sufixo || "",
  };
  if (eixo) eixoY.title = { text: eixo, font: { color: cor } };
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "inherit", color: cor, size: 12 },
    margin: { l: 56, r: 12, t: 16, b: 56 },
    legend: { orientation: "h", y: -0.22 },
    hovermode: "x unified",
    separators: ", ",
    height: 320,
    xaxis: {
      gridcolor: "rgba(128,128,128,0.18)",
      zeroline: false,
      tickfont: { color: cor },
    },
    yaxis: eixoY,
  };
}

function grafico(pai, traces, layout) {
  const no = h("div", { class: "grafico" });
  pai.append(no);
  Plotly.newPlot(no, traces, layout, CFG);
  graficos.push(no);
  return no;
}

function temNumeros(series) {
  return (series || []).some((serie) => (serie.valores || []).some((valor) => Number(valor) !== 0));
}

function baseIndice(series) {
  const n = (series || []).reduce((maximo, serie) => Math.max(maximo, (serie.valores || []).length), 0);
  for (let i = 0; i < n; i += 1) {
    let soma = 0;
    series.forEach((serie) => {
      soma += Number(serie.valores[i] || 0);
    });
    if (soma !== 0) return soma;
  }
  return null;
}

function dica(quando, nome, rotulo) {
  return (quando ? fmtData(quando) + " · " : "") + nome + ": " + rotulo;
}

function desenharArea(pai, datas, series) {
  if (!datas || !datas.length || !temNumeros(series)) {
    pai.append(vazio());
    return;
  }
  const esconder = esconderLigado();
  const base = esconder ? baseIndice(series) : null;
  const traces = series.map((serie) => {
    const y = [];
    const texto = [];
    (serie.valores || []).forEach((valor, indice) => {
      const numero = Number(valor || 0);
      if (esconder) {
        const indice100 = base ? (numero / base) * 100 : 0;
        y.push(indice100);
        texto.push(dica(datas[indice], serie.nome, fmtIndice(indice100)));
      } else {
        y.push(numero);
        texto.push(dica(datas[indice], serie.nome, fmtEur(numero)));
      }
    });
    return {
      type: "scatter",
      mode: "lines",
      name: serie.nome,
      x: datas,
      y: y,
      stackgroup: "um",
      line: { color: serie.cor, width: 1.5 },
      customdata: texto,
      hovertemplate: "%{customdata}<extra></extra>",
    };
  });
  const layout = layoutBase(esconder ? "Índice (base 100)" : "", esconder ? "" : " €");
  layout.xaxis.tickformat = "%d/%m/%Y";
  grafico(pai, traces, layout);
}

function desenharLinhas(pai, datas, series) {
  if (!datas || !datas.length || !series.some((serie) => serie.valores.some((valor) => Number(valor) !== 0))) {
    pai.append(vazio());
    return;
  }
  const esconder = esconderLigado();
  let base = null;
  if (esconder) {
    series.some((serie) => {
      base = serie.valores.map(Number).find((valor) => valor !== 0) || null;
      return base;
    });
  }
  const traces = series.map((serie) => {
    const y = [];
    const texto = [];
    serie.valores.forEach((valor, indice) => {
      const numero = Number(valor || 0);
      if (esconder) {
        const indice100 = base ? (numero / Math.abs(base)) * 100 : 0;
        y.push(indice100);
        texto.push(dica(datas[indice], serie.nome, fmtIndice(indice100)));
      } else {
        y.push(numero);
        texto.push(dica(datas[indice], serie.nome, fmtEur(numero)));
      }
    });
    return {
      type: "scatter",
      mode: "lines+markers",
      name: serie.nome,
      x: datas,
      y: y,
      line: { color: serie.cor, width: 2 },
      customdata: texto,
      hovertemplate: "%{customdata}<extra></extra>",
    };
  });
  const layout = layoutBase(esconder ? "Índice (base 100)" : "", esconder ? "" : " €");
  layout.xaxis.tickformat = "%d/%m/%Y";
  grafico(pai, traces, layout);
}

function desenharDonut(pai, partes) {
  const uteis = (partes || []).filter((parte) => Number(parte.valor) !== 0);
  if (!uteis.length) {
    pai.append(vazio());
    return;
  }
  const total = uteis.reduce((soma, parte) => soma + Number(parte.valor), 0);
  const esconder = esconderLigado();
  const texto = uteis.map((parte) => {
    const peso = total ? Number(parte.valor) / total : null;
    if (esconder) return parte.nome + ": " + fmtPct(peso);
    return parte.nome + ": " + fmtEur(parte.valor) + " (" + fmtPct(peso) + ")";
  });
  const cor = corTexto();
  grafico(
    pai,
    [{
      type: "pie",
      labels: uteis.map((parte) => parte.nome),
      values: uteis.map((parte) => (esconder && total ? (Number(parte.valor) / total) * 100 : Number(parte.valor))),
      hole: 0.55,
      sort: false,
      marker: { colors: uteis.map((parte) => parte.cor) },
      textinfo: "percent",
      textposition: "outside",
      textfont: { color: cor },
      customdata: texto,
      hovertemplate: "%{customdata}<extra></extra>",
    }],
    {
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: "inherit", color: cor, size: 12 },
      margin: { l: 16, r: 16, t: 16, b: 16 },
      height: 340,
      showlegend: true,
      separators: ", ",
    },
  );
}

function desenharBarras(pai, chaves, series, modo) {
  if (!chaves || !chaves.length || !temNumeros(series)) {
    pai.append(vazio());
    return;
  }
  const esconder = esconderLigado();
  const rotulos = chaves.map(fmtPeriodo);
  const totais = chaves.map((_, indice) => series.reduce((soma, serie) => soma + Number(serie.valores[indice] || 0), 0));
  const traces = series.map((serie) => {
    const y = [];
    const texto = [];
    (serie.valores || []).forEach((valor, indice) => {
      const numero = Number(valor || 0);
      if (esconder && modo !== "indice") {
        const peso = totais[indice] ? numero / totais[indice] : 0;
        y.push(peso * 100);
        texto.push(serie.nome + " · " + rotulos[indice] + ": " + fmtPct(peso));
      } else if (esconder) {
        y.push(numero);
        texto.push(serie.nome + " · " + rotulos[indice] + ": " + fmtIndice(numero));
      } else {
        y.push(numero);
        texto.push(serie.nome + " · " + rotulos[indice] + ": " + fmtEur(numero));
      }
    });
    return {
      type: "bar",
      name: serie.nome,
      x: rotulos,
      y: y,
      marker: { color: serie.cor },
      customdata: texto,
      hovertemplate: "%{customdata}<extra></extra>",
    };
  });
  const layout = layoutBase(
    esconder ? (modo === "indice" ? "Índice (base 100)" : "Percentagem") : "",
    esconder ? "" : " €",
  );
  layout.barmode = modo === "grupo" || modo === "indice" ? "group" : "stack";
  layout.xaxis.type = "category";
  grafico(pai, traces, layout);
}

function desenharColunas(pai, rotulos, valores, nome, cor) {
  if (!valores || !valores.some((valor) => Number(valor) !== 0)) {
    pai.append(vazio());
    return;
  }
  const esconder = esconderLigado();
  const total = valores.reduce((soma, valor) => soma + Number(valor || 0), 0);
  const y = [];
  const texto = [];
  valores.forEach((valor, indice) => {
    const numero = Number(valor || 0);
    if (esconder) {
      const peso = total ? numero / total : 0;
      y.push(peso * 100);
      texto.push(nome + " · " + rotulos[indice] + ": " + fmtPct(peso));
    } else {
      y.push(numero);
      texto.push(nome + " · " + rotulos[indice] + ": " + fmtEur(numero));
    }
  });
  const layout = layoutBase(esconder ? "Percentagem" : "", esconder ? "" : " €");
  layout.xaxis.type = "category";
  grafico(pai, [{
    type: "bar",
    name: nome,
    x: rotulos,
    y: y,
    marker: { color: cor || COR_CAPITAL },
    customdata: texto,
    hovertemplate: "%{customdata}<extra></extra>",
  }], layout);
}

function indiceConjunto(listas) {
  let base = null;
  listas.forEach((valores) => {
    if (base != null) return;
    const achado = valores.map(Number).find((valor) => valor !== 0);
    if (achado) base = Math.abs(achado);
  });
  return base;
}

function queryDe(nome) {
  if (nome === "gastos") {
    const params = { periodo: gastosEstado.periodo, freq: gastosEstado.freq };
    if (gastosEstado.periodo === "personalizado") {
      params.inicio = gastosEstado.inicio;
      params.fim = gastosEstado.fim;
    }
    return params;
  }
  if (nome === "movimentos") {
    return {
      fonte: movEstado.fonte,
      categoria: movEstado.categoria,
      tipo: movEstado.tipo,
      texto: movEstado.texto,
      inicio: movEstado.inicio,
      fim: movEstado.fim,
      limite: "500",
    };
  }
  return {};
}

function urlDe(nome, params) {
  const pesquisa = new URLSearchParams();
  Object.keys(params || {}).forEach((chave) => {
    const valor = params[chave];
    if (valor != null && String(valor) !== "") pesquisa.set(chave, String(valor));
  });
  const texto = pesquisa.toString();
  return "/api/" + nome + (texto ? "?" + texto : "");
}

async function obter(nome) {
  const url = urlDe(nome, queryDe(nome));
  if (memoria.has(url)) return memoria.get(url);
  const resposta = await fetch(url);
  let dados = {};
  try {
    dados = await resposta.json();
  } catch (exc) {
    dados = {};
  }
  if (!resposta.ok) throw new Error(dados.erro || "erro " + resposta.status);
  memoria.set(url, dados);
  return dados;
}

function esquecer(prefixo) {
  Array.from(memoria.keys()).forEach((chave) => {
    if (chave.indexOf("/api/" + prefixo) === 0) memoria.delete(chave);
  });
}

function painel() {
  return document.getElementById("painel");
}

async function renderAtual() {
  const token = geracao + 1;
  geracao = token;
  const nome = separadorAtual();
  marcarNav();
  limparGraficos();
  const raiz = painel();
  raiz.setAttribute("aria-busy", "true");
  raiz.replaceChildren(h("p", { class: "carregar", text: "A carregar…" }));
  try {
    const dados = await obter(nome);
    if (token !== geracao) return;
    limparGraficos();
    raiz.removeAttribute("aria-busy");
    raiz.replaceChildren(VISTAS[nome](dados));
  } catch (exc) {
    if (token !== geracao) return;
    raiz.removeAttribute("aria-busy");
    raiz.replaceChildren(
      h("p", { class: "erro", text: "Não foi possível carregar." }),
      h("p", { class: "detalhe", text: exc && exc.message ? exc.message : "" }),
    );
  }
}

function vistaResumo(dados) {
  const raiz = h("div");
  raiz.append(h("h1", { text: "Quanto tenho e onde?" }));
  if (dados.dia) raiz.append(h("p", { class: "nota", text: "Snapshot de " + fmtData(dados.dia) + "." }));
  const indice = indicePatrimonio(dados.serie);
  const variacao = dados.variacao || {};
  raiz.append(cartoes([
    cartao(
      "Património",
      esconderLigado() ? (indice == null ? "—" : fmtIndice(indice)) : fmtEur(dados.patrimonio),
      esconderLigado() ? "Índice (base 100)" : null,
    ),
    cartao(
      "Desde o snapshot anterior",
      esconderLigado() ? fmtPct(variacao.delta_anterior_pct) : montante(variacao.delta_anterior),
      esconderLigado() ? null : fmtPct(variacao.delta_anterior_pct),
    ),
    cartao(
      "Há 30 dias",
      esconderLigado() ? fmtPct(variacao.delta_30_pct) : montante(variacao.delta_30),
      esconderLigado() ? null : fmtPct(variacao.delta_30_pct),
    ),
    cartao("Fontes ok", String(dados.n_fontes_ok == null ? 0 : dados.n_fontes_ok)),
  ]));
  if (dados.serie && dados.serie.preenchida) {
    raiz.append(h("p", { class: "nota", text: "Algumas fontes foram preenchidas com o último valor conhecido, para o total não saltar quando uma fonte falta num dia." }));
  }
  raiz.append(blocoVeredicto(dados.veredicto));
  const area = h("div");
  desenharArea(area, (dados.serie && dados.serie.datas) || [], (dados.serie && dados.serie.series) || []);
  raiz.append(seccao("Património por fonte", area));
  if (dados.fontes && dados.fontes.length) raiz.append(listaPesos(dados.fontes, dados.patrimonio));
  const donut = h("div");
  desenharDonut(donut, dados.alocacao || []);
  raiz.append(seccao("Alocação", donut));
  raiz.append(blocoAvisos(dados.avisos || []));
  return raiz;
}

function indicePatrimonio(serie) {
  if (!serie || !serie.series || !serie.series.length) return null;
  const base = baseIndice(serie.series);
  if (!base) return null;
  const ultimo = serie.series.reduce((soma, item) => soma + Number((item.valores || [])[item.valores.length - 1] || 0), 0);
  return (ultimo / base) * 100;
}

function listaPesos(fontes, total) {
  const lista = h("ul", { class: "lista" });
  fontes.forEach((fonte) => {
    const peso = total ? Number(fonte.valor) / Number(total) : null;
    const texto = esconderLigado()
      ? fonte.nome + " · " + fmtPct(peso)
      : fonte.nome + " · " + fmtEur(fonte.valor) + " (" + fmtPct(peso) + ")";
    lista.append(h("li", {}, amostra(fonte.cor), h("span", { text: texto })));
  });
  return lista;
}

function blocoVeredicto(veredicto) {
  if (!veredicto || veredicto.indisponivel || !veredicto.frases || !veredicto.frases.length) {
    return seccao("Veredicto", vazio());
  }
  const lista = h("ul", { class: "veredicto" });
  veredicto.frases.forEach((frase) => lista.append(h("li", { text: frase })));
  return seccao("Veredicto", lista);
}

function blocoAvisos(avisos) {
  const relevantes = avisos.filter((aviso) => aviso.estado !== "ok" || aviso.texto);
  if (!relevantes.length) return seccao("Avisos", h("p", { class: "vazio", text: "Sem avisos." }));
  const bloco = seccao("Avisos");
  relevantes.forEach((aviso) => {
    const texto = aviso.texto ? aviso.nome + ": " + aviso.estado + " — " + aviso.texto : aviso.nome + ": " + aviso.estado;
    bloco.append(h("p", { class: aviso.estado === "ok" ? "info" : "aviso", text: texto }));
  });
  return bloco;
}

function vistaInvestimentos(dados) {
  const raiz = h("div");
  raiz.append(h("h1", { text: "Sou bom investidor?" }));
  const linhas = dados.linhas || [];
  if (!linhas.length) {
    raiz.append(vazio());
    return raiz;
  }
  if (!linhas.some((linha) => linha.nome === plataformaEscolhida)) plataformaEscolhida = "Total";
  raiz.append(tabelaInvestimentos(linhas));
  const escolha = linhas.find((linha) => linha.nome === plataformaEscolhida) || linhas[linhas.length - 1];
  const controlos = h("div", { class: "controlos" });
  controlos.append(h("label", { class: "campo", text: "Plataforma" }, selecionar(
    plataformaEscolhida,
    linhas.map((linha) => [linha.nome, linha.nome]),
    (evento) => {
      plataformaEscolhida = evento.target.value;
      renderAtual();
    },
  )));
  raiz.append(controlos);
  if (escolha.nota) raiz.append(h("p", { class: "nota", text: escolha.nota }));
  raiz.append(cartoes([
    cartao("A tua XIRR", fmtPct(escolha.xirr)),
    cartao("MSCI World", fmtPct(escolha.xirr_indice)),
    cartao("Ganho", fmtPct(escolha.ganho_pct)),
  ]));
  const comparacao = h("p");
  comparacao.append(etiqueta(escolha.etiqueta), " ", h("span", { text: escolha.veredicto || "" }));
  raiz.append(comparacao);
  const curvas = h("div");
  const pontos = escolha.curva || [];
  desenharLinhas(curvas, pontos.map((ponto) => ponto.dia), [
    { nome: "Capital investido", cor: COR_CAPITAL, valores: pontos.map((ponto) => ponto.capital) },
    { nome: "Valor", cor: COR_VALOR, valores: pontos.map((ponto) => ponto.valor) },
  ]);
  raiz.append(seccao("Capital investido e valor", curvas));
  raiz.append(seccao("Rendimentos e lucro realizado", graficoAnos(escolha)));
  return raiz;
}

function tabelaInvestimentos(linhas) {
  const cabeca = h("tr");
  const colunas = ["Plataforma", "Ganho", "XIRR", "MSCI World", "Tu vs índice"];
  if (!esconderLigado()) colunas.splice(1, 0, "Depositado", "Valor");
  colunas.forEach((titulo) => cabeca.append(h("th", { text: titulo })));
  const corpo = h("tbody");
  linhas.forEach((linha) => {
    const tr = h("tr");
    tr.append(h("td", { text: linha.nome }));
    if (!esconderLigado()) {
      tr.append(h("td", { class: "num", text: fmtEur(linha.depositado) }));
      tr.append(h("td", { class: "num", text: fmtEur(linha.valor) }));
    }
    tr.append(h("td", { class: "num", text: fmtPct(linha.ganho_pct) }));
    tr.append(h("td", { class: "num", text: fmtPct(linha.xirr) }));
    tr.append(h("td", { class: "num", text: fmtPct(linha.xirr_indice) }));
    const vs = h("td");
    vs.append(etiqueta(linha.etiqueta));
    tr.append(vs);
    corpo.append(tr);
  });
  return h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo));
}

function graficoAnos(linha) {
  const caixa = h("div");
  const mapa = new Map();
  (linha.rendimentos || []).forEach((item) => mapa.set(item.ano, { ano: item.ano, rendimento: item.valor, realizado: 0 }));
  (linha.realizado || []).forEach((item) => {
    const atual = mapa.get(item.ano) || { ano: item.ano, rendimento: 0, realizado: 0 };
    atual.realizado = item.valor;
    mapa.set(item.ano, atual);
  });
  const anos = Array.from(mapa.values()).sort((a, b) => a.ano - b.ano);
  if (!anos.length) {
    caixa.append(vazio());
    return caixa;
  }
  const esconder = esconderLigado();
  const rendimentos = anos.map((item) => item.rendimento);
  const realizado = anos.map((item) => item.realizado);
  let series;
  if (esconder) {
    const base = indiceConjunto([rendimentos, realizado]);
    series = [
      { nome: "Dividendos e juros", cor: COR_CAPITAL, valores: rendimentos.map((valor) => (base ? (Number(valor) / base) * 100 : 0)) },
      { nome: "Lucro realizado", cor: COR_REALIZADO, valores: realizado.map((valor) => (base ? (Number(valor) / base) * 100 : 0)) },
    ];
  } else {
    series = [
      { nome: "Dividendos e juros", cor: COR_CAPITAL, valores: rendimentos },
      { nome: "Lucro realizado", cor: COR_REALIZADO, valores: realizado },
    ];
  }
  desenharBarras(caixa, anos.map((item) => String(item.ano)), series, esconder ? "indice" : "grupo");
  return caixa;
}

function vistaPosicoes(dados) {
  const raiz = h("div");
  raiz.append(h("h1", { text: "Posições" }));
  if (!dados || dados.indisponivel) {
    raiz.append(vazio());
    return raiz;
  }
  const fontes = [];
  (dados.atuais || []).forEach((linha) => {
    if (!fontes.some((item) => item.fonte === linha.fonte)) fontes.push({ fonte: linha.fonte, nome: linha.nome_fonte || linha.fonte });
  });
  const filtro = filtroPosicao;
  const controlos = h("div", { class: "controlos" });
  controlos.append(h("label", { class: "campo", text: "Fonte" }, selecionar(
    filtro,
    [["", "Todas"]].concat(fontes.map((item) => [item.fonte, item.nome])),
    (evento) => {
      filtroPosicao = evento.target.value;
      renderAtual();
    },
  )));
  raiz.append(controlos);
  const visiveis = (dados.atuais || []).filter((linha) => !filtro || linha.fonte === filtro);
  raiz.append(tabelaPosicoes(visiveis));
  const custo = h("div");
  desenharArea(custo, (dados.historico_custo || {}).datas || [], (dados.historico_custo || {}).series || []);
  raiz.append(seccao("Capital investido por posição", custo));
  const valor = h("div");
  desenharArea(valor, (dados.historico_valor || {}).datas || [], (dados.historico_valor || {}).series || []);
  const blocoValor = seccao("Valor de mercado por posição", valor);
  const semPreco = (dados.historico_valor && dados.historico_valor.sem_preco) || [];
  if (semPreco.length) blocoValor.append(h("p", { class: "nota", text: "Sem preço: " + semPreco.join(", ") }));
  raiz.append(blocoValor);
  raiz.append(seccao("Negócios fechados", cartoesNegocios(dados.estatisticas)));
  raiz.append(seccao("Realizado por símbolo", tabelaRealizado(dados.realizado_por_simbolo || [])));
  return raiz;
}

function barraLatente(pct, maximo) {
  const caixa = h("span", { class: "divergente" });
  const esq = h("span", { class: "metade esq" });
  const dir = h("span", { class: "metade dir" });
  const numero = Number(pct);
  if (Number.isFinite(numero) && maximo) {
    const largura = Math.min(100, (Math.abs(numero) / maximo) * 100);
    const barra = h("span", { class: numero < 0 ? "neg" : "pos" });
    barra.style.width = largura + "%";
    if (numero < 0) esq.append(barra);
    else if (numero > 0) dir.append(barra);
  }
  caixa.append(esq, dir);
  const linha = h("span", { title: fmtPct(pct) });
  linha.append(caixa, h("span", { text: fmtPct(pct) }));
  return linha;
}

function tabelaPosicoes(linhas) {
  if (!linhas.length) return vazio();
  const maximo = linhas.reduce((maior, linha) => Math.max(maior, Math.abs(Number(linha.latente_pct) || 0)), 0);
  const estado = ordemTabelas.posicoes;
  const colunas = [
    { chave: "nome", titulo: "Posição", ordem: (linha) => linha.nome || linha.simbolo },
    { chave: "classe", titulo: "Classe" },
    { chave: "peso", titulo: "Peso", num: true, ordem: (linha) => Number(linha.peso) },
    { chave: "latente_pct", titulo: "Ganho latente", num: true, ordem: (linha) => Number(linha.latente_pct) },
    { chave: "realizado_eur", titulo: esconderLigado() ? "Realizado %" : "Realizado", num: true, ordem: (linha) => Number(linha.realizado_eur) },
    { chave: "dividendos_eur", titulo: esconderLigado() ? "Dividendos %" : "Dividendos", num: true, ordem: (linha) => Number(linha.dividendos_eur) },
  ];
  const ordenadas = linhas.slice().sort((a, b) => comparar(a, b, colunas, estado));
  const cabeca = h("tr");
  colunas.forEach((coluna) => {
    const seta = estado.chave === coluna.chave ? (estado.dir > 0 ? " ↑" : " ↓") : "";
    const botao = h("button", {
      type: "button",
      class: "ordenar",
      text: coluna.titulo + seta,
      onclick: () => {
        if (estado.chave === coluna.chave) estado.dir *= -1;
        else {
          estado.chave = coluna.chave;
          estado.dir = coluna.num ? -1 : 1;
        }
        renderAtual();
      },
    });
    cabeca.append(h("th", { class: coluna.num ? "num" : "" }, botao));
  });
  const corpo = h("tbody");
  ordenadas.forEach((linha) => {
    const nome = (linha.nome || linha.simbolo || "") + (linha.simbolo ? " (" + linha.simbolo + ")" : "");
    const tr = h("tr");
    tr.append(h("td", { text: nome }));
    tr.append(h("td", { text: linha.classe || "—" }));
    tr.append(h("td", { class: "num", text: fmtPct(linha.peso) }));
    const ganho = h("td", { class: "num" });
    ganho.append(barraLatente(linha.latente_pct, maximo));
    tr.append(ganho);
    tr.append(h("td", { class: "num", text: percentagemSobreValor(linha.realizado_eur, linha.valor_eur, linha.realizado_eur) }));
    tr.append(h("td", { class: "num", text: percentagemSobreValor(linha.dividendos_eur, linha.valor_eur, linha.dividendos_eur) }));
    corpo.append(tr);
  });
  return h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo));
}

function percentagemSobreValor(parte, total, euros) {
  if (!esconderLigado()) return fmtEur(euros);
  if (!total) return "—";
  return fmtPct(Number(parte) / Number(total));
}

function comparar(a, b, colunas, estado) {
  const coluna = colunas.find((item) => item.chave === estado.chave) || colunas[0];
  const va = coluna.ordem ? coluna.ordem(a) : a[coluna.chave];
  const vb = coluna.ordem ? coluna.ordem(b) : b[coluna.chave];
  if (!Number.isFinite(va) && !Number.isFinite(vb) && (va == null || vb == null)) {
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
  }
  if (typeof va === "number" && typeof vb === "number") {
    if (!Number.isFinite(va) && !Number.isFinite(vb)) return 0;
    if (!Number.isFinite(va)) return 1;
    if (!Number.isFinite(vb)) return -1;
    return (va - vb) * estado.dir;
  }
  return String(va).localeCompare(String(vb), "pt") * estado.dir;
}

function cartoesNegocios(estatisticas) {
  if (!estatisticas) return vazio();
  const lista = [
    cartao("Negócios fechados", String(estatisticas.fechados)),
    cartao("Taxa de acerto", fmtPct(estatisticas.taxa_acerto)),
    cartao("Fator de lucro", estatisticas.fator_lucro == null ? "—" : fmtNum(estatisticas.fator_lucro, 2)),
  ];
  if (!esconderLigado()) {
    lista.push(cartao("Melhor", estatisticas.melhor ? fmtEur(estatisticas.melhor.valor) : "—", estatisticas.melhor ? estatisticas.melhor.nome : null));
    lista.push(cartao("Pior", estatisticas.pior ? fmtEur(estatisticas.pior.valor) : "—", estatisticas.pior ? estatisticas.pior.nome : null));
  } else {
    lista.push(cartao("Melhor", estatisticas.melhor ? estatisticas.melhor.nome : "—"));
    lista.push(cartao("Pior", estatisticas.pior ? estatisticas.pior.nome : "—"));
  }
  return cartoes(lista);
}

function tabelaRealizado(linhas) {
  if (!linhas.length) return vazio();
  const cabeca = h("tr");
  ["Símbolo", "Fonte", "Vendas", "Ganhos", "Acerto", esconderLigado() ? "" : "Realizado", "Estado"].filter(Boolean).forEach((titulo) => {
    cabeca.append(h("th", { text: titulo }));
  });
  const corpo = h("tbody");
  linhas.forEach((linha) => {
    const acerto = linha.vendas ? linha.ganhos / linha.vendas : null;
    const tr = h("tr");
    tr.append(h("td", { text: linha.nome ? linha.nome + " (" + linha.simbolo + ")" : linha.simbolo }));
    tr.append(h("td", { text: linha.nome_fonte || linha.fonte || "" }));
    tr.append(h("td", { class: "num", text: String(linha.vendas) }));
    tr.append(h("td", { class: "num", text: String(linha.ganhos) }));
    tr.append(h("td", { class: "num", text: fmtPct(acerto) }));
    if (!esconderLigado()) tr.append(h("td", { class: "num", text: fmtEur(linha.realizado_eur) }));
    tr.append(h("td", { text: linha.ainda_aberta ? "Ainda aberta" : "Fechada" }));
    corpo.append(tr);
  });
  return h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo));
}

function vistaGastos(dados) {
  const raiz = h("div");
  raiz.append(h("h1", { text: "Para onde vai o meu dinheiro?" }));
  raiz.append(controlosGastos());
  raiz.append(h("p", { class: "nota", text: "De " + fmtData(dados.inicio) + " a " + fmtData(dados.fim) + "." }));
  (dados.avisos || []).forEach((aviso) => raiz.append(h("p", { class: "aviso", text: aviso })));
  if (dados.frases && dados.frases.length) {
    const lista = h("ul", { class: "frases" });
    dados.frases.forEach((frase) => lista.append(h("li", { text: frase })));
    raiz.append(lista);
  }
  raiz.append(cartao("Taxa de poupança", fmtPct(dados.taxa_poupanca)));
  const donut = h("div");
  desenharDonut(donut, dados.categorias || []);
  raiz.append(seccao("Por categoria", donut));
  const barras = h("div");
  desenharBarras(barras, dados.periodos || [], dados.empilhado || [], "percentagem");
  raiz.append(seccao("Por período", barras));
  const fluxo = h("div");
  const meses = dados.receitas_gastos || [];
  desenharBarras(fluxo, meses.map((item) => item.periodo), [
    { nome: "Receitas", cor: COR_REALIZADO, valores: meses.map((item) => item.receitas) },
    { nome: "Gastos", cor: COR_VALOR, valores: meses.map((item) => item.gastos) },
  ], "grupo");
  raiz.append(seccao("Receitas e gastos", fluxo));
  raiz.append(seccao("Top comerciantes", tabelaPares(dados.comerciantes || [], dados.categorias || [], "Comerciante", "valor")));
  raiz.append(seccao("Subscrições", tabelaSubscricoes(dados.subscricoes || [])));
  const dias = h("div");
  const listaDias = dados.dias || [];
  desenharColunas(dias, listaDias.map((item) => item.nome), listaDias.map((item) => item.valor), "Gastos", COR_CAPITAL);
  raiz.append(seccao("Dia da semana", dias));
  raiz.append(seccao("Comparação com o mês anterior", tabelaComparacao(dados.comparacao || [])));
  return raiz;
}

function controlosGastos() {
  const linha = h("div", { class: "controlos" });
  linha.append(h("label", { class: "campo", text: "Período" }, selecionar(
    gastosEstado.periodo,
    [
      ["este mês", "Este mês"],
      ["mês passado", "Mês passado"],
      ["últimos 3 meses", "Últimos 3 meses"],
      ["últimos 12 meses", "Últimos 12 meses"],
      ["personalizado", "Personalizado"],
    ],
    (evento) => {
      gastosEstado.periodo = evento.target.value;
      if (gastosEstado.periodo === "personalizado") {
        const hoje = new Date();
        if (!gastosEstado.inicio) gastosEstado.inicio = isoLocal(new Date(hoje.getFullYear(), hoje.getMonth(), 1));
        if (!gastosEstado.fim) gastosEstado.fim = isoLocal(hoje);
      }
      renderAtual();
    },
  )));
  linha.append(h("label", { class: "campo", text: "Frequência" }, selecionar(
    gastosEstado.freq,
    [["mes", "Mês"], ["semana", "Semana"]],
    (evento) => {
      gastosEstado.freq = evento.target.value;
      renderAtual();
    },
  )));
  if (gastosEstado.periodo === "personalizado") {
    linha.append(campoData("De", gastosEstado.inicio, (evento) => {
      gastosEstado.inicio = evento.target.value;
      if (gastosEstado.inicio && gastosEstado.fim) renderAtual();
    }));
    linha.append(campoData("Até", gastosEstado.fim, (evento) => {
      gastosEstado.fim = evento.target.value;
      if (gastosEstado.inicio && gastosEstado.fim) renderAtual();
    }));
  }
  return linha;
}

function campoData(rotulo, valor, aoMudar) {
  const input = h("input", { type: "date", value: valor || "", onchange: aoMudar });
  return h("label", { class: "campo", text: rotulo }, input);
}

function selecionar(valor, pares, aoMudar) {
  const sel = h("select", { onchange: aoMudar });
  pares.forEach((par) => {
    const opt = h("option", { value: par[0], text: par[1] });
    if (par[0] === valor) opt.selected = true;
    sel.append(opt);
  });
  return sel;
}

function tabelaPares(linhas, categorias, titulo, chave) {
  if (!linhas.length) return vazio();
  const total = (categorias || []).reduce((soma, item) => soma + Number(item.valor || 0), 0);
  const cabeca = h("tr", {}, h("th", { text: titulo }), h("th", { class: "num", text: esconderLigado() ? "Peso" : "Valor" }));
  const corpo = h("tbody");
  linhas.forEach((linha) => {
    const peso = total ? Number(linha[chave]) / total : null;
    corpo.append(h("tr", {}, h("td", { class: "largo", text: linha.nome }), h("td", {
      class: "num",
      text: esconderLigado() ? fmtPct(peso) : fmtEur(linha[chave]),
    })));
  });
  return h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo));
}

function tabelaSubscricoes(linhas) {
  if (!linhas.length) return vazio();
  const total = linhas.reduce((soma, item) => soma + Number(item.custo_anual || 0), 0);
  const cabeca = h("tr", {}, h("th", { text: "Nome" }), h("th", { text: "Periodicidade" }), h("th", { class: "num", text: esconderLigado() ? "Peso" : "Custo anual" }));
  const corpo = h("tbody");
  linhas.forEach((linha) => {
    const peso = total ? Number(linha.custo_anual) / total : null;
    corpo.append(h("tr", {},
      h("td", { class: "largo", text: linha.nome }),
      h("td", { text: linha.periodicidade || "" }),
      h("td", { class: "num", text: esconderLigado() ? fmtPct(peso) : fmtEur(linha.custo_anual) }),
    ));
  });
  return h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo));
}

function tabelaComparacao(linhas) {
  if (!linhas.length) return vazio();
  const cabeca = h("tr");
  const titulos = esconderLigado()
    ? ["Categoria", "Vs mês anterior", "Vs média 3 meses"]
    : ["Categoria", "Este mês", "Mês anterior", "Média 3 meses", "Vs mês anterior", "Vs média 3 meses"];
  titulos.forEach((titulo, indice) => cabeca.append(h("th", { class: indice ? "num" : "", text: titulo })));
  const corpo = h("tbody");
  linhas.forEach((linha) => {
    const tr = h("tr");
    tr.append(h("td", {}, amostra(linha.cor), " ", h("span", { text: linha.categoria })));
    if (!esconderLigado()) {
      tr.append(h("td", { class: "num", text: fmtEur(linha.este_mes) }));
      tr.append(h("td", { class: "num", text: fmtEur(linha.mes_anterior) }));
      tr.append(h("td", { class: "num", text: fmtEur(linha.media_3_meses) }));
    }
    tr.append(h("td", { class: "num", text: fmtPct(linha.vs_anterior) }));
    tr.append(h("td", { class: "num", text: fmtPct(linha.vs_media) }));
    corpo.append(tr);
  });
  return h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo));
}

function vistaMovimentos(dados) {
  const raiz = h("div");
  raiz.append(h("h1", { text: "Movimentos" }));
  raiz.append(filtrosMovimentos(dados));
  const lista = h("div", { id: "lista-mov" });
  lista.append(tabelaMovimentos(dados));
  raiz.append(lista);
  return raiz;
}

function filtrosMovimentos(dados) {
  const linha = h("div", { class: "filtros" });
  const pesquisa = h("input", {
    id: "pesquisa",
    type: "search",
    value: movEstado.texto,
    placeholder: "Pesquisar",
    oninput: (evento) => {
      movEstado.texto = evento.target.value;
      clearTimeout(timerPesquisa);
      timerPesquisa = setTimeout(atualizarTabelaMovimentos, 180);
    },
  });
  linha.append(h("label", { class: "campo", text: "Pesquisa" }, pesquisa));
  linha.append(h("label", { class: "campo", text: "Fonte" }, selecionar(
    movEstado.fonte,
    [["", "Todas"]].concat((dados.fontes || []).map((item) => [item.fonte, item.nome])),
    (evento) => {
      movEstado.fonte = evento.target.value;
      renderAtual();
    },
  )));
  linha.append(h("label", { class: "campo", text: "Categoria" }, selecionar(
    movEstado.categoria,
    [["", "Todas"]].concat((dados.categorias || []).map((nome) => [nome, nome])),
    (evento) => {
      movEstado.categoria = evento.target.value;
      renderAtual();
    },
  )));
  linha.append(h("label", { class: "campo", text: "Tipo" }, selecionar(
    movEstado.tipo,
    [["", "Todos"]].concat((dados.tipos || []).map((nome) => [nome, nome])),
    (evento) => {
      movEstado.tipo = evento.target.value;
      renderAtual();
    },
  )));
  linha.append(campoData("De", movEstado.inicio, (evento) => {
    movEstado.inicio = evento.target.value;
    renderAtual();
  }));
  linha.append(campoData("Até", movEstado.fim, (evento) => {
    movEstado.fim = evento.target.value;
    renderAtual();
  }));
  return linha;
}

async function atualizarTabelaMovimentos() {
  const texto = movEstado.texto;
  const token = geracao;
  try {
    const dados = await obter("movimentos");
    if (texto !== movEstado.texto || token !== geracao) return;
    const lista = document.getElementById("lista-mov");
    if (!lista) return;
    lista.replaceChildren(tabelaMovimentos(dados));
  } catch (exc) {
    const lista = document.getElementById("lista-mov");
    if (!lista || token !== geracao) return;
    lista.replaceChildren(h("p", { class: "erro", text: exc.message || "Não foi possível carregar." }));
  }
}

function tabelaMovimentos(dados) {
  const linhas = dados.linhas || [];
  if (!linhas.length) return vazio();
  const bloco = h("div");
  if (dados.total > linhas.length) {
    bloco.append(h("p", { class: "nota", text: "A mostrar " + linhas.length + " de " + dados.total + "." }));
  }
  const categorias = dados.categorias || [];
  const cabeca = h("tr");
  ["Data", "Fonte", "Descrição", "Categoria", "Tipo", esconderLigado() ? "Sentido" : "Montante"].forEach((titulo) => {
    cabeca.append(h("th", { text: titulo }));
  });
  const corpo = h("tbody");
  linhas.forEach((linha) => {
    const tr = h("tr");
    tr.append(h("td", { text: fmtData(linha.data) }));
    tr.append(h("td", { text: linha.nome_fonte || linha.fonte || "" }));
    tr.append(h("td", { class: "largo", text: linha.descricao || "" }));
    tr.append(h("td", {}, editorCategoria(linha, categorias)));
    tr.append(h("td", { text: linha.tipo || "" }));
    tr.append(h("td", {
      class: "num",
      text: esconderLigado() ? sentido(linha.montante) : fmtEur(linha.montante, linha.moeda),
    }));
    corpo.append(tr);
  });
  bloco.append(h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo)));
  return bloco;
}

function sentido(valor) {
  const numero = Number(valor);
  if (!Number.isFinite(numero)) return "—";
  if (numero < 0) return "saída";
  if (numero > 0) return "entrada";
  return "zero";
}

function editorCategoria(linha, categorias) {
  const nomes = categorias.slice();
  if (linha.categoria && nomes.indexOf(linha.categoria) === -1) nomes.unshift(linha.categoria);
  const sel = selecionar(linha.categoria, nomes.map((nome) => [nome, nome]), () => {});
  const estado = h("span", { class: "estado-guardar" });
  const botao = h("button", {
    type: "button",
    text: "Guardar",
    onclick: () => guardarCategoria(linha, sel.value, estado),
  });
  return h("div", { class: "celula-acao" }, sel, botao, estado);
}

async function guardarCategoria(linha, categoria, estado) {
  estado.textContent = "A guardar…";
  try {
    const resposta = await fetch("/api/categoria", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Financas": "1" },
      body: JSON.stringify({ descricao: linha.descricao, categoria: categoria, tipo: linha.tipo }),
    });
    let dados = {};
    try {
      dados = await resposta.json();
    } catch (exc) {
      dados = {};
    }
    if (!resposta.ok) throw new Error(dados.erro || "Não foi possível guardar.");
    esquecer("movimentos");
    esquecer("gastos");
    estado.textContent = "Guardado.";
    renderAtual();
  } catch (exc) {
    estado.textContent = exc.message || "Não foi possível guardar.";
  }
}

function vistaEstado(dados) {
  const raiz = h("div");
  raiz.append(h("h1", { text: "Estado" }));
  raiz.append(h("p", { class: "nota", text: "Última corrida: " + fmtData(dados.ultima_corrida) + "." }));
  const fontes = dados.fontes || [];
  if (!fontes.length) {
    raiz.append(vazio());
    return raiz;
  }
  const cabeca = h("tr");
  ["Fonte", "Estado", "Avisos", "Consentimento", "Movimentos"].forEach((titulo) => cabeca.append(h("th", { text: titulo })));
  const corpo = h("tbody");
  fontes.forEach((fonte) => {
    const tr = h("tr");
    const nome = h("td");
    nome.append(amostra(fonte.cor), " ", h("span", { text: fonte.nome }));
    tr.append(nome);
    tr.append(h("td", { text: fonte.estado || "—" }));
    tr.append(h("td", { class: "largo", text: fonte.avisos || "—" }));
    tr.append(h("td", { text: textoConsentimento(fonte) }));
    tr.append(h("td", { class: "num", text: String(fonte.movimentos == null ? 0 : fonte.movimentos) }));
    corpo.append(tr);
  });
  raiz.append(h("div", { class: "tabela-wrap" }, h("table", {}, h("thead", {}, cabeca), corpo)));
  return raiz;
}

function textoConsentimento(fonte) {
  if (fonte.dias == null) return "—";
  if (fonte.dias >= 0) return fonte.dias + " dias";
  return "expirado há " + (-fonte.dias) + " dias";
}

const VISTAS = {
  resumo: vistaResumo,
  investimentos: vistaInvestimentos,
  posicoes: vistaPosicoes,
  gastos: vistaGastos,
  movimentos: vistaMovimentos,
  estado: vistaEstado,
};

async function atualizarAgora() {
  const botao = document.getElementById("atualizar");
  const progresso = document.getElementById("progresso");
  botao.disabled = true;
  progresso.hidden = false;
  progresso.textContent = "A atualizar…";
  try {
    const resposta = await fetch("/api/atualizar", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Financas": "1" },
      body: "{}",
    });
    let dados = {};
    try {
      dados = await resposta.json();
    } catch (exc) {
      dados = {};
    }
    if (!resposta.ok) throw new Error(dados.erro || "A atualização falhou.");
    acompanhar();
  } catch (exc) {
    progresso.textContent = exc.message || "A atualização falhou.";
    botao.disabled = false;
  }
}

async function acompanhar() {
  const botao = document.getElementById("atualizar");
  const progresso = document.getElementById("progresso");
  let dados = {};
  try {
    const resposta = await fetch("/api/atualizar");
    dados = await resposta.json();
  } catch (exc) {
    progresso.textContent = "Não foi possível ler o progresso.";
    botao.disabled = false;
    return;
  }
  const linhas = dados.ultimas_linhas || [];
  const ultima = linhas.length ? linhas[linhas.length - 1] : "";
  if (dados.a_correr) {
    progresso.hidden = false;
    progresso.textContent = ultima ? "A atualizar… " + ultima : "A atualizar…";
    botao.disabled = true;
    clearTimeout(timerAtualizar);
    timerAtualizar = setTimeout(acompanhar, 800);
    return;
  }
  botao.disabled = false;
  if (dados.ultimo_codigo == null && !dados.fim) {
    progresso.hidden = true;
    progresso.textContent = "";
    return;
  }
  progresso.hidden = false;
  progresso.textContent = dados.ultimo_codigo ? "A atualização falhou." : "Atualização concluída.";
  memoria.clear();
  renderAtual();
}

function iniciar() {
  const caixa = document.getElementById("esconder");
  caixa.checked = lerEsconder();
  caixa.addEventListener("change", () => {
    guardarEsconder(caixa.checked);
    renderAtual();
  });
  document.getElementById("atualizar").addEventListener("click", atualizarAgora);
  window.addEventListener("hashchange", renderAtual);
  window.addEventListener("resize", () => {
    graficos.forEach((no) => {
      try {
        Plotly.Plots.resize(no);
      } catch (exc) {
        /* gráfico já purgado */
      }
    });
  });
  if (!location.hash) history.replaceState(null, "", "#resumo");
  renderAtual();
  acompanhar();
}

iniciar();
