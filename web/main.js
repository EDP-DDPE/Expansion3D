import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { buildNetwork, disposeObject, fmt, groundHeight, LAYERS, setTerrain, toWorld } from './network.js';
import { BASEMAPS, buildTerrainSurface } from './basemap.js';
import {
  buildBuildings, buildClearance, buildRoads, buildTrees, clearancePick,
  CLEARANCE_COLORS, CLEARANCE_LABELS, CONTEXT_ATTRIBUTION,
} from './context.js';
import { buildImportada } from './importado.js';

const $ = (id) => document.getElementById(id);
const viewport = $('viewport');

// --- cena -------------------------------------------------------------------------------------
const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
viewport.prepend(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.domElement.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:1';
viewport.appendChild(labelRenderer.domElement);

// segundo renderizador de rótulos, usado só na comparação lado a lado
const labelRendererDir = new CSS2DRenderer();
labelRendererDir.domElement.style.cssText = 'position:absolute;top:0;bottom:0;left:50%;right:0;pointer-events:none;z-index:1';
labelRendererDir.domElement.hidden = true;
viewport.appendChild(labelRendererDir.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color('#aac3d8');
scene.add(new THREE.HemisphereLight(0xe8f2ff, 0x5a5a48, 1.8));
const sun = new THREE.DirectionalLight(0xffffff, 2.2);
sun.position.set(0.6, 1, 0.35);
scene.add(sun);

const camera = new THREE.PerspectiveCamera(50, 1, 0.5, 200000);
camera.position.set(0, 3000, 3000);
const cameraDir = camera.clone(); // lado direito da comparação: segue a câmera principal
const controls = new OrbitControls(camera, renderer.domElement);
Object.assign(controls, {
  enableDamping: true, dampingFactor: 0.12, screenSpacePanning: false, zoomToCursor: true,
  minDistance: 3, maxDistance: 60000, maxPolarAngle: Math.PI / 2 - 0.03,
});

const selection = new THREE.Mesh(
  new THREE.SphereGeometry(1, 16, 12),
  new THREE.MeshBasicMaterial({ color: 0xffe14d, wireframe: true, depthTest: false, transparent: true, opacity: 0.6 }),
);
selection.renderOrder = 999;
selection.visible = false;
scene.add(selection);

// A camada 0 do Three.js guarda o que é comum a todas as etapas (relevo, árvores, casas, ruas);
// cada etapa fica na sua própria camada, e a câmera escolhe qual enxergar.
const CAMADA_CONTEXTO = 0;
const camadaDaEtapa = (i) => i + 1;

const state = {
  data: null, net: null, ground: null, basemap: null, token: 0, redeId: null, acervo: [],
  layerOn: Object.fromEntries(LAYERS.map((l) => [l.key, l.on])),
  basemapKey: 'satelite', mapDetail: 1, lineWidth: 2,
  gerenciadores: [], alcance: Number(localStorage.getItem('alcance')) || 1800,
  usuario: null, sso: false, atlas: '',
  etapas: [], ativa: 0, direita: 1, dividida: false, projeto: null, projetos: [],
  camadas: [], importadas: [], contexto: null, cfg: null,
};

const etapaAtual = () => state.etapas[state.ativa] ?? null;

// --- prédios e árvores conforme a câmera ------------------------------------------------------
let proximaAtualizacao = 0;
function atualizarContexto(forcar = false) {
  const agora = performance.now();
  if (!forcar && agora < proximaAtualizacao) return;
  proximaAtualizacao = agora + 250;
  const altura = Math.max(camera.position.y - groundHeight(camera.position.x, -camera.position.z), 0);
  // muito alto: prédios e árvores não são legíveis e só custam desempenho
  const raio = altura > state.alcance * 2 ? 0 : state.alcance;
  for (const g of state.gerenciadores) g.atualizar(camera, state.layerOn[g.camada] ? raio : 0);
  const predios = state.gerenciadores.find((g) => g.camada === 'predios');
  if (predios) {
    $('ctx-predios-blocos').textContent = raio && state.layerOn.predios
      ? `Carregando ${fmt(predios.montadas)} de ${fmt(predios.total)} blocos de prédios conforme a câmera (alcance ${fmt(state.alcance)} m)`
      : 'Prédios fora de alcance: aproxime a câmera do solo para carregá-los';
  }
}

function todosLineMaterials() {
  return [...state.etapas.flatMap((e) => e.net?.lineMaterials ?? []),
    ...(state.contexto?.lineMaterials ?? [])];
}

function resize() {
  const w = viewport.clientWidth, h = viewport.clientHeight;
  renderer.setSize(w, h);
  const larguraCena = state.dividida ? Math.floor(w / 2) : w;
  labelRenderer.setSize(larguraCena, h);
  labelRenderer.domElement.style.right = state.dividida ? '50%' : '0';
  labelRendererDir.setSize(w - larguraCena, h);
  camera.aspect = larguraCena / h;
  camera.updateProjectionMatrix();
  cameraDir.aspect = (w - larguraCena) / h;
  cameraDir.updateProjectionMatrix();
  todosLineMaterials().forEach((m) => m.resolution.set(larguraCena, h));
}
new ResizeObserver(resize).observe(viewport);

// --- animação de câmera -----------------------------------------------------------------------
let tween = null;
function flyTo(target, distance) {
  const dir = camera.position.clone().sub(controls.target).setY(0);
  if (dir.lengthSq() < 1e-6) dir.set(0, 0, 1);
  dir.normalize();
  // aproximações ficam oblíquas para mostrar a altura dos equipamentos; visões gerais, mais de cima
  const elevation = distance < 300 ? 0.55 : 1.0;
  dir.multiplyScalar(Math.cos(elevation)).setY(Math.sin(elevation));
  tween = {
    t0: performance.now(), dur: 800,
    fromPos: camera.position.clone(), fromTarget: controls.target.clone(),
    toTarget: target.clone(), toPos: target.clone().add(dir.multiplyScalar(distance)),
  };
}

function apontarCamera(cam, indiceEtapa) {
  cam.layers.set(CAMADA_CONTEXTO);
  if (state.etapas.length) cam.layers.enable(camadaDaEtapa(indiceEtapa));
}

renderer.setAnimationLoop(() => {
  if (tween) {
    const k = Math.min((performance.now() - tween.t0) / tween.dur, 1);
    const e = k < 0.5 ? 4 * k ** 3 : 1 - (-2 * k + 2) ** 3 / 2;
    camera.position.lerpVectors(tween.fromPos, tween.toPos, e);
    controls.target.lerpVectors(tween.fromTarget, tween.toTarget, e);
    if (k === 1) tween = null;
  }
  controls.update();
  atualizarContexto();
  for (const g of state.gerenciadores) g.montarLote?.();
  if (selection.visible) {
    const s = THREE.MathUtils.clamp(camera.position.distanceTo(selection.position) * 0.012, 0.8, 60);
    selection.scale.setScalar(s * (1 + 0.12 * Math.sin(performance.now() / 180)));
  }
  desenhar();
});

function desenhar() {
  const w = viewport.clientWidth, h = viewport.clientHeight;
  if (!state.dividida) {
    renderer.setScissorTest(false);
    renderer.setViewport(0, 0, w, h);
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
    return;
  }
  // os dois lados compartilham a mesma câmera: o movimento é igual nos dois, para comparar ponto a ponto
  const meio = Math.floor(w / 2);
  cameraDir.position.copy(camera.position);
  cameraDir.quaternion.copy(camera.quaternion);
  apontarCamera(cameraDir, state.direita);

  renderer.setScissorTest(true);
  renderer.setViewport(0, 0, meio, h);
  renderer.setScissor(0, 0, meio, h);
  renderer.render(scene, camera);
  renderer.setViewport(meio, 0, w - meio, h);
  renderer.setScissor(meio, 0, w - meio, h);
  renderer.render(scene, cameraDir);
  renderer.setScissorTest(false);

  const esq = etapaAtual();
  const dir = state.etapas[state.direita];
  if (esq?.net) labelRenderer.render(esq.net.root, camera);
  // mesma etapa dos dois lados: um renderizador só, senão os rótulos pulam entre os lados
  if (dir?.net && dir !== esq) labelRendererDir.render(dir.net.root, cameraDir);
}

// --- utilidades de tela -------------------------------------------------------------------------
function setStatus(text, error = false) {
  $('status').textContent = text;
  $('status').classList.toggle('error', error);
}

function setContextStatus(id, text, error = false) {
  $(id).textContent = text;
  $(id).classList.toggle('error', error);
}

// O FastAPI devolve `detail` como texto nos nossos erros e como lista nos de validação;
// sem tratar os dois a mensagem na tela viraria "[object Object]".
function mensagemDeErro(body, res) {
  const d = body?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc ?? []).slice(1).join('.')}: ${e.msg}`).join(' · ');
  return res.statusText || 'erro desconhecido';
}

async function fetchJson(url, opcoes) {
  const res = await fetch(url, opcoes);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(mensagemDeErro(body, res));
  return body;
}

// Lê a resposta em pedaços para mostrar o % baixado (o servidor informa o tamanho total).
async function fetchJsonProgresso(url, onProgress) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? res.statusText);
  }
  if (!res.body) return res.json();
  const total = Number(res.headers.get('content-length')) || 0;
  const reader = res.body.getReader();
  const partes = [];
  let recebido = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    partes.push(value);
    recebido += value.length;
    onProgress?.(total ? recebido / total : null, recebido);
  }
  return JSON.parse(await new Blob(partes).text());
}

function setLoading(visivel, titulo, sub) {
  $('loading').hidden = !visivel;
  if (titulo) $('loading-titulo').textContent = titulo;
  if (sub !== undefined) $('loading-sub').textContent = sub ?? '';
}

// Cada conjunto de dados vira um "chip" na barra superior, com % e barrinha.
function setProgresso(chave, { rotulo, pct, texto, concluido, erro }) {
  const painel = $('progresso');
  let chip = painel.querySelector(`[data-k="${chave}"]`);
  if (!chip) {
    chip = document.createElement('div');
    chip.className = 'chip';
    chip.dataset.k = chave;
    chip.innerHTML = '<span class="rot"></span><span class="val"></span><div class="barra"><i></i></div>';
    painel.append(chip);
  }
  chip.querySelector('.rot').textContent = rotulo;
  chip.querySelector('.val').textContent = erro ? 'erro' : pct == null ? (texto ?? '…') : `${Math.round(pct * 100)}%`;
  chip.querySelector('.barra i').style.width = pct == null ? '100%' : `${Math.round(pct * 100)}%`;
  chip.classList.toggle('indeterminado', pct == null && !erro);
  chip.classList.toggle('erro', !!erro);
  if (concluido) setTimeout(() => chip.remove(), 5000);
}

// --- limpeza ------------------------------------------------------------------------------------
function clearNetwork() {
  for (const etapa of state.etapas) {
    if (etapa.net) { scene.remove(etapa.net.root); disposeObject(etapa.net.root); }
  }
  state.etapas = [];
  state.ativa = 0;
  state.direita = 1;
  dividirTela(false);
  if (state.contexto) { scene.remove(state.contexto.root); disposeObject(state.contexto.root); }
  state.contexto = null;
  for (const key of ['ground', 'basemap']) {
    if (state[key]) { scene.remove(state[key]); disposeObject(state[key]); }
    state[key] = null;
  }
  state.gerenciadores = [];
  state.importadas = [];
  state.net = null;
  state.data = null;
  selection.visible = false;
  $('info').hidden = true;
  $('veg-summary').replaceChildren();
  $('veg-list').replaceChildren();
  $('barra-etapas').hidden = true;
}

function bboxWorld(points) {
  const box = new THREE.Box3();
  points.forEach((p) => box.expandByPoint(toWorld(p.x, p.y)));
  return box;
}

function focusBox(box, minDistance = 150) {
  const size = box.getSize(new THREE.Vector3());
  flyTo(box.getCenter(new THREE.Vector3()), Math.max(size.x, size.z, minDistance) * 1.1);
}

// --- abertura de rede e de projeto --------------------------------------------------------------
async function baixarRede(id, rotulo, token) {
  return fetchJsonProgresso(`/api/rede?id=${encodeURIComponent(id)}`, (pct, bytes) => {
    if (token !== state.token) return;
    setProgresso('rede', { rotulo, pct, texto: `${fmt(bytes / 1048576, 1)} MB` });
    setLoading(true, 'Abrindo a rede…', pct == null
      ? `${fmt(bytes / 1048576, 1)} MB recebidos`
      : `${Math.round(pct * 100)}% do modelo recebido`);
  });
}

/** Abre uma rede solta do acervo: internamente é um projeto de uma etapa só. */
function loadRede(id) {
  if (!id) return Promise.resolve();
  return abrirProjeto({ nome: null, etapas: [{ id: `rede:${id}`, nome: 'Rede', rede_id: id }] });
}

/**
 * Abre um projeto: a primeira etapa vira a referência (dono do relevo, do mapa e do contexto)
 * e as demais são recolocadas no referencial dela e carregadas em segundo plano.
 */
async function abrirProjeto(projeto) {
  const etapas = projeto.etapas ?? [];
  if (!etapas.length) { setStatus('Este projeto ainda não tem etapas.', true); return; }
  const token = ++state.token;
  $('progresso').replaceChildren();
  setLoading(true, 'Abrindo a rede…', 'lendo a base do Interplan e preparando o relevo');
  setStatus('Lendo a base e preparando o relevo (a 1ª carga de uma região pode levar alguns minutos)…');
  setProgresso('rede', { rotulo: 'Rede', texto: 'no servidor…' });
  const t0 = performance.now();

  let data;
  try {
    data = await baixarRede(etapas[0].rede_id, 'Rede', token);
  } catch (err) {
    if (token === state.token) {
      setStatus(`Erro: ${err.message}`, true);
      setProgresso('rede', { rotulo: 'Rede', texto: 'erro', erro: true });
      setLoading(false);
    }
    return;
  }
  if (token !== state.token) return;

  setLoading(true, 'Montando a cena 3D…', `${fmt(data.meta.resumo.postes)} postes e ${fmt(data.meta.resumo.vaos)} vãos`);
  // deixa a tela de carregamento aparecer antes de montar a cena (setTimeout, e não
  // requestAnimationFrame, que não dispara com a janela oculta e travaria a abertura)
  await new Promise((resolve) => setTimeout(resolve, 16));

  clearNetwork();
  state.token = token;
  state.projeto = projeto.nome ? projeto : null;
  state.data = data;
  state.redeId = etapas[0].rede_id;

  state.etapas = etapas.map((e, i) => ({
    ...e, indice: i, data: null, net: null, circOn: new Map(), estado: i === 0 ? 'montando' : 'na fila',
  }));
  montarEtapa(state.etapas[0], data);
  state.ativa = 0;
  state.direita = Math.min(1, state.etapas.length - 1);

  const [x0, y0, x1, y1] = data.meta.bbox;
  const size = Math.max(x1 - x0, y1 - y0) * 4;
  state.ground = new THREE.Mesh(
    new THREE.PlaneGeometry(size, size).rotateX(-Math.PI / 2),
    new THREE.MeshStandardMaterial({ color: 0x46513f, roughness: 1 }),
  );
  state.ground.position.set((x0 + x1) / 2, -2, -(y0 + y1) / 2);
  state.ground.layers.set(CAMADA_CONTEXTO);
  scene.add(state.ground);

  state.contexto = { root: new THREE.Group(), buckets: new Map(), pickables: [], lineMaterials: [], amostras: {} };
  state.contexto.root.layers.set(CAMADA_CONTEXTO);
  scene.add(state.contexto.root);
  applyBasemap();

  apontarCamera(camera, 0);
  resize();
  renderSidebar();
  renderLayers();
  renderBarraEtapas();
  applyVisibility();
  applyLineWidth();
  $('empty').hidden = true;
  focusBox(bboxWorld(data.postes));
  history.replaceState(null, '', projeto.id ? `?projeto=${encodeURIComponent(projeto.id)}`
    : `?rede=${encodeURIComponent(state.redeId)}`);
  const reg = data.meta.registro ?? {};
  $('rede-atual').textContent = [projeto.nome, data.meta.nome, reg.descricao,
    reg.usuario && `enviado por ${reg.usuario}`].filter(Boolean).join(' · ');
  renderAcervo();
  // as duas listas trazem botões que só valem com uma rede aberta ("Mostrar na cena",
  // "Nova etapa"): sem redesenhá-las aqui eles continuariam desabilitados
  renderCamadas();
  renderProjetos();
  const r = data.meta.resumo;
  const t = data.terreno;
  setStatus(`${fmt(r.postes)} postes · relevo ${fmt(t?.min, 0)}–${fmt(t?.max, 0)} m · ${((performance.now() - t0) / 1000).toFixed(1)} s`);
  setProgresso('rede', { rotulo: 'Rede', pct: 1, texto: 'pronto', concluido: true });
  setLoading(false);
  loadContext(state.redeId, token);
  if (state.etapas.length > 1) carregarDemaisEtapas(token);
}

/** Constrói a geometria de uma etapa e a põe na camada dela. */
function montarEtapa(etapa, data) {
  const ref = state.etapas[0]?.data ?? data;
  const deslocamento = etapa.indice === 0 ? null : {
    x: data.meta.origem.x - ref.meta.origem.x,
    y: (data.terreno?.base ?? 0) - (ref.terreno?.base ?? 0),
    z: -(data.meta.origem.y - ref.meta.origem.y),
  };
  etapa.data = data;
  etapa.net = buildNetwork(data, { camadaRender: camadaDaEtapa(etapa.indice), deslocamento });
  etapa.circOn = new Map(data.circuitos.map((c) => [c.id, true]));
  etapa.estado = 'pronta';
  scene.add(etapa.net.root);
  if (etapa.indice === 0) state.net = etapa.net;
  // o relevo corrente volta a ser o da etapa de referência, que é o da cena
  if (state.etapas[0]?.data) setTerrain(state.etapas[0].data.terreno);
}

/** Carrega as demais etapas em segundo plano, uma de cada vez, sem travar a navegação. */
async function carregarDemaisEtapas(token) {
  for (const etapa of state.etapas.slice(1)) {
    if (token !== state.token) return;
    etapa.estado = 'carregando';
    renderBarraEtapas();
    setProgresso(`etapa${etapa.indice}`, { rotulo: etapa.nome, texto: 'no servidor…' });
    try {
      const data = await fetchJsonProgresso(`/api/rede?id=${encodeURIComponent(etapa.rede_id)}`,
        (pct, bytes) => setProgresso(`etapa${etapa.indice}`,
          { rotulo: etapa.nome, pct, texto: `${fmt(bytes / 1048576, 1)} MB` }));
      if (token !== state.token) return;
      montarEtapa(etapa, data);
      applyVisibility();
      applyLineWidth();
      resize();
      setProgresso(`etapa${etapa.indice}`, { rotulo: etapa.nome, pct: 1, texto: 'pronta', concluido: true });
    } catch (err) {
      if (token !== state.token) return;
      etapa.estado = 'erro';
      etapa.erro = err.message;
      setProgresso(`etapa${etapa.indice}`, { rotulo: etapa.nome, texto: 'erro', erro: true });
    }
    renderBarraEtapas();
  }
}

function mostrarEtapa(indice) {
  const etapa = state.etapas[indice];
  if (!etapa?.net) return;
  state.ativa = indice;
  state.net = etapa.net;
  state.data = etapa.data;
  apontarCamera(camera, indice);
  renderSidebar();
  renderLayers();
  renderBarraEtapas();
  applyVisibility();
  setStatus(`${etapa.nome} · ${fmt(etapa.data.meta.resumo.postes)} postes · ${fmt(etapa.data.meta.resumo.km, 1, 'km')}`);
}

function dividirTela(ligada) {
  state.dividida = ligada && state.etapas.length > 1;
  $('dividir-tela').checked = state.dividida;
  $('etapa-direita').hidden = !state.dividida;
  viewport.classList.toggle('dividida', state.dividida);
  labelRendererDir.domElement.hidden = !state.dividida;
  $('rotulo-esq').hidden = !state.dividida;
  $('rotulo-dir').hidden = !state.dividida;
  if (state.dividida) {
    $('rotulo-esq').textContent = etapaAtual()?.nome ?? '';
    $('rotulo-dir').textContent = state.etapas[state.direita]?.nome ?? '';
  } else {
    // ao sair da comparação os rótulos do lado direito precisam sumir da tela
    labelRendererDir.domElement.replaceChildren();
  }
  resize();
}

function renderBarraEtapas() {
  const barra = $('barra-etapas');
  const varias = state.etapas.length > 1;
  barra.hidden = !varias;
  if (!varias) return;
  $('barra-etapas-nome').textContent = state.projeto?.nome ?? '';
  $('etapas-botoes').replaceChildren(...state.etapas.map((etapa, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = etapa.nome;
    b.classList.toggle('ativa', i === state.ativa);
    b.classList.toggle('carregando', !etapa.net);
    b.disabled = !etapa.net;
    b.title = etapa.estado === 'erro' ? `Erro: ${etapa.erro}`
      : etapa.net ? (etapa.descricao || 'Mostrar esta etapa') : 'Carregando…';
    b.addEventListener('click', () => mostrarEtapa(i));
    return b;
  }));
  const seletor = $('etapa-direita');
  seletor.replaceChildren(...state.etapas.map((etapa, i) => new Option(etapa.nome, String(i))));
  seletor.value = String(state.direita);
  if (state.dividida) {
    $('rotulo-esq').textContent = etapaAtual()?.nome ?? '';
    $('rotulo-dir').textContent = state.etapas[state.direita]?.nome ?? '';
  }
}

// --- acervo compartilhado ---------------------------------------------------------------------
function setAcervoStatus(texto, erro = false) {
  $('acervo-status').textContent = texto;
  $('acervo-status').classList.toggle('error', erro);
}

function selo(visibilidade) {
  if (visibilidade !== 'privada') return null;
  const s = document.createElement('span');
  s.className = 'selo privada';
  s.textContent = 'privada';
  s.title = 'Só você vê esta entrada';
  return s;
}

function podeExcluir(registro) {
  if (!state.usuario) return false;
  return registro.visibilidade !== 'privada' || registro.dono === state.usuario.matricula;
}

function cartaoRede(r) {
  const li = document.createElement('li');
  li.className = 'acervo-item';
  if (r.id === state.redeId) li.classList.add('ativo');
  const numeros = r.resumo.numeros ?? {};
  const ses = r.resumo.subestacoes.map((s) => s.nome || s.cod);
  const titulo = document.createElement('b');
  titulo.textContent = ses.length > 2 ? `${ses.length} SEs: ${ses.slice(0, 2).join(', ')}…`
    : ses.map((nome) => `SE ${nome}`).join(', ') || r.arquivo;
  const marca = selo(r.visibilidade);
  if (marca) titulo.append(marca);
  const circuitos = r.resumo.circuitos;
  const linha = document.createElement('div');
  linha.className = 'meta';
  linha.textContent = [
    r.regiao,
    circuitos.length > 4 ? `${fmt(circuitos.length)} circuitos` : circuitos.map((c) => c.codigo).join(' · '),
    `${fmt(numeros.postes)} postes`,
    fmt(numeros.km, 1, 'km'),
  ].filter(Boolean).join(' · ');
  const quem = document.createElement('div');
  quem.className = 'meta';
  quem.textContent = `${r.usuario} · ${new Date(r.criado_em).toLocaleString('pt-BR')} · ${r.arquivo}`;
  const acoes = document.createElement('div');
  acoes.className = 'acoes';
  const abrir = document.createElement('button');
  abrir.textContent = r.id === state.redeId ? 'Recarregar' : 'Abrir';
  abrir.addEventListener('click', () => loadRede(r.id));
  acoes.append(abrir);
  if (podeExcluir(r)) {
    const excluir = document.createElement('button');
    excluir.className = 'secundario';
    excluir.textContent = 'Excluir';
    excluir.addEventListener('click', () => excluirRede(r));
    acoes.append(excluir);
  }
  li.append(titulo, linha);
  if (r.descricao) {
    const desc = document.createElement('div');
    desc.className = 'desc';
    desc.textContent = r.descricao;
    li.append(desc);
  }
  li.append(quem, acoes);
  return li;
}

function acervoFiltrado() {
  const busca = ($('acervo-busca').value || '').trim().toLowerCase();
  const regiao = $('acervo-regiao').value;
  const porRegiao = regiao ? state.acervo.filter((r) => r.regiao === regiao) : state.acervo;
  if (!busca) return porRegiao;
  return porRegiao.filter((r) => [
    r.arquivo, r.usuario, r.descricao,
    ...r.resumo.subestacoes.flatMap((s) => [s.nome, s.cod]),
    ...r.resumo.circuitos.map((c) => c.codigo),
  ].filter(Boolean).join(' ').toLowerCase().includes(busca));
}

function renderAcervo() {
  const lista = acervoFiltrado();
  $('acervo').replaceChildren(...lista.map(cartaoRede));
  if (state.acervo.length && !lista.length) {
    const vazio = document.createElement('li');
    vazio.className = 'acervo-item meta';
    vazio.textContent = 'Nenhuma rede corresponde à busca.';
    $('acervo').append(vazio);
  }
}

async function carregarAcervo(abrir) {
  try {
    const { redes } = await fetchJson('/api/acervo');
    state.acervo = redes;
    renderAcervo();
    const privadas = redes.filter((r) => r.visibilidade === 'privada').length;
    setAcervoStatus(redes.length
      ? `${fmt(redes.length)} rede(s) disponíveis${privadas ? ` · ${fmt(privadas)} privada(s) sua(s)` : ''}`
      : 'Acervo vazio. Envie um rede.mdb do Interplan.');
    if (abrir && redes.some((r) => r.id === abrir)) loadRede(abrir);
  } catch (err) {
    setAcervoStatus(`Erro ao ler o acervo: ${err.message}`, true);
  }
}

async function excluirRede(r) {
  const alcance = r.visibilidade === 'privada' ? 'Ela é sua e some só para você.'
    : 'A exclusão vale para todos os usuários.';
  if (!window.confirm(`Excluir "${r.arquivo}" do acervo? ${alcance}`)) return;
  try {
    await fetchJson(`/api/acervo/${r.id}`, { method: 'DELETE' });
    if (state.redeId === r.id) {
      state.token += 1; // descarta respostas de contexto ainda a caminho
      clearNetwork();
      state.redeId = null;
      $('empty').hidden = false;
      $('rede-atual').textContent = '';
      setStatus('');
    }
    carregarAcervo();
  } catch (err) {
    setAcervoStatus(`Erro ao excluir: ${err.message}`, true);
  }
}

// --- vegetação, prédios e camadas importadas ----------------------------------------------------
function addContext(built) {
  const alvo = state.contexto;
  if (!alvo) return;
  for (const g of built.groups) {
    g.layers.set(CAMADA_CONTEXTO);
    g.traverse((o) => o.layers.set(CAMADA_CONTEXTO));
    alvo.root.add(g);
    alvo.buckets.set(`${g.userData.layer}|${g.uuid}`, g);
  }
  alvo.pickables.push(...built.pickables);
  alvo.lineMaterials.push(...(built.lineMaterials ?? []));
  if (built.gerenciador) state.gerenciadores.push(built.gerenciador);
  for (const [camada, amostra] of Object.entries(built.amostras ?? {})) alvo.amostras[camada] = amostra;
  resize();
  applyLineWidth();
  applyVisibility();
  renderLayers();
}

// Clicar no nome da camada leva a um item sorteado dela.
function irParaAleatorio(camada) {
  const amostra = state.net?.amostras?.[camada.key] ?? state.contexto?.amostras?.[camada.key];
  if (!amostra?.picks.length) {
    setStatus(`Sem itens na camada ${camada.label}`, true);
    return;
  }
  if (!state.layerOn[camada.key]) {
    state.layerOn[camada.key] = true;
    applyVisibility();
    renderLayers();
  }
  const pick = amostra.picks[Math.floor(Math.random() * amostra.picks.length)]();
  showInfo(pick);
  flyTo(pick.pos, 70);
  setStatus(`${camada.label}: item sorteado de ${fmt(amostra.total)}`);
}

// Cada conjunto é baixado em paralelo, com % na barra superior e resumo no painel lateral.
async function executarContexto({ chave, rotulo, url, alvo, token, montar, resumo }) {
  setProgresso(chave, { rotulo, texto: 'no servidor…' });
  setContextStatus(alvo, `${rotulo}: consultando o servidor (a 1ª vez de uma região leva alguns minutos)…`);
  try {
    const dados = await fetchJsonProgresso(url, (pct, bytes) =>
      setProgresso(chave, { rotulo, pct, texto: `${fmt(bytes / 1048576, 1)} MB` }));
    if (token !== state.token) return;
    setProgresso(chave, { rotulo, pct: 1, texto: 'montando' });
    setContextStatus(alvo, `${rotulo}: montando na cena…`);
    await montar(dados, (fracao) => setProgresso(chave, { rotulo, pct: fracao, texto: 'montando' }));
    if (token !== state.token) return;
    const novos = dados.ladrilhos?.novos ?? 0;
    setContextStatus(alvo, `${resumo(dados)} · ${novos ? `${fmt(novos)} ladrilho(s) baixado(s) agora` : 'reaproveitado do cache'}`);
    setProgresso(chave, { rotulo, pct: 1, texto: 'pronto', concluido: true });
  } catch (err) {
    if (token !== state.token) return;
    setContextStatus(alvo, `Erro: ${err.message}`, true);
    setProgresso(chave, { rotulo, texto: 'erro', erro: true });
  }
}

function loadContext(id, token) {
  const q = `id=${encodeURIComponent(id)}`;

  executarContexto({
    chave: 'ruas', rotulo: 'Ruas', alvo: 'ctx-ruas', url: `/api/contexto/ruas?${q}`, token,
    montar: async (ruas, prog) => {
      const built = await buildRoads(ruas, (feito, total) => prog(feito / total));
      if (token !== state.token) { built.groups.forEach(disposeObject); return; }
      addContext(built);
    },
    resumo: (ruas) => `${fmt(ruas.n)} trechos de via`,
  });

  executarContexto({
    chave: 'veg', rotulo: 'Vegetação', alvo: 'ctx-veg', url: `/api/contexto/vegetacao?${q}`, token,
    montar: async (veg) => {
      addContext(buildTrees(veg));
      addContext(buildClearance(veg, state.etapas[0].data, state.etapas[0].net));
      renderVegetation(veg);
    },
    resumo: (veg) => `${fmt(veg.arvores.n)} árvores no corredor da rede`,
  });

  executarContexto({
    chave: 'predios', rotulo: 'Prédios', alvo: 'ctx-predios', url: `/api/contexto/predios?${q}`, token,
    montar: async (predios, prog) => {
      prog(1);
      const built = buildBuildings(predios);
      if (token !== state.token) { built.groups.forEach(disposeObject); return; }
      addContext(built);
      atualizarContexto(true);
    },
    resumo: (predios) => `${fmt(predios.n)} edificações · ${fmt(predios.estimadas)} com altura estimada`,
  });
}

function renderVegetation(veg) {
  $('veg-summary').replaceChildren(...veg.limiares.flatMap(({ classe, folga_menor_que: limit }) => {
    const a = document.createElement('span');
    const dot = document.createElement('i');
    dot.style.background = CLEARANCE_COLORS[classe];
    a.append(dot, `${CLEARANCE_LABELS[classe]} (< ${fmt(limit, 1)} m)`);
    const b = document.createElement('b');
    b.textContent = `${fmt(veg.resumo[classe])} · ${fmt(veg.resumo[`km_${classe}`], 1, 'km')}`;
    return [a, b];
  }));

  $('veg-list').replaceChildren(...veg.criticos.slice(0, 150).map((r) => {
    const li = document.createElement('li');
    const dot = document.createElement('i');
    dot.style.background = CLEARANCE_COLORS[r.classe];
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = `Trecho ${r.id} · ${state.etapas[0].net.circName(r.c)}`;
    const value = document.createElement('span');
    value.className = 'km';
    value.textContent = fmt(r.folga, 1, 'm');
    li.append(dot, name, value);
    li.title = 'Clique para ir até o ponto de menor folga';
    li.addEventListener('click', () => {
      const pick = clearancePick(r, veg, state.etapas[0].data, state.etapas[0].net);
      showInfo(pick);
      flyTo(pick.pos, 70);
    });
    return li;
  }));
}

function applyBasemap() {
  if (state.basemap) { scene.remove(state.basemap); disposeObject(state.basemap); }
  state.basemap = buildTerrainSurface(state.etapas[0]?.data?.mapa, state.basemapKey, renderer, state.mapDetail);
  state.basemap.layers.set(CAMADA_CONTEXTO);
  state.basemap.traverse((o) => o.layers.set(CAMADA_CONTEXTO));
  scene.add(state.basemap);
  const parts = [BASEMAPS[state.basemapKey].attribution, state.data && CONTEXT_ATTRIBUTION].filter(Boolean);
  $('attribution').hidden = !parts.length;
  $('attribution').textContent = parts.join(' · ');
}

function applyVisibility() {
  for (const etapa of state.etapas) {
    if (!etapa.net) continue;
    for (const g of etapa.net.buckets.values()) {
      const { layer, circ } = g.userData;
      const visible = state.layerOn[layer] && (circ == null || etapa.circOn.get(circ) !== false);
      g.visible = visible;
      g.traverse((o) => { if (o.isCSS2DObject) o.visible = visible; });
    }
  }
  for (const g of state.contexto?.buckets.values() ?? []) {
    const visible = state.layerOn[g.userData.layer];
    g.visible = visible;
    g.traverse((o) => { if (o.isCSS2DObject) o.visible = visible; });
  }
}

function applyLineWidth() {
  todosLineMaterials().forEach((m) => { m.linewidth = state.lineWidth + (m.userData.extraWidth ?? 0); });
}

// --- painel lateral ---------------------------------------------------------------------------
function renderSidebar() {
  const { data } = state;
  if (!data) return;
  const r = data.meta.resumo;
  const ses = data.subestacoes;
  $('se-title').textContent = ses.length === 1
    ? `SE ${ses[0].nome || ses[0].cod} (${ses[0].cod})`
    : ses.length ? `${ses.length} subestações: ${ses.map((s) => s.cod).join(', ')}`
      : data.meta.nome;
  const rows = [
    ['Circuitos', fmt(r.circuitos)],
    ['Rede MT', fmt(r.km, 2, 'km')],
    ['Postes', fmt(r.postes)],
    ['Transformadores', `${fmt(r.trafos)} · ${fmt(r.kva, 0, 'kVA')}`],
    ['Cabines de cliente MT', fmt(r.cabines)],
    ['Chaves (abertas)', `${fmt(r.chaves)} (${fmt(r.chaves_abertas)})`],
    ['Reguladores / capacitores', `${fmt(r.reguladores)} / ${fmt(r.capacitores)}`],
    ['Geração distribuída', `${fmt(r.geradores)} · ${fmt(r.gd_kva, 0, 'kVA')}`],
  ];
  $('summary').replaceChildren(...rows.flatMap(([k, v]) => {
    const a = document.createElement('span'); a.textContent = k;
    const b = document.createElement('b'); b.textContent = v;
    return [a, b];
  }));

  const etapa = etapaAtual();
  $('circuits').replaceChildren(...data.circuitos.map((c) => {
    const li = document.createElement('li');
    li.innerHTML = '<input type="checkbox" checked><span class="swatch"></span><span class="name"></span><span class="km"></span>';
    li.querySelector('.swatch').style.background = c.cor;
    li.querySelector('.name').textContent = c.codigo;
    li.querySelector('.km').textContent = fmt(c.km, 1, 'km');
    li.title = 'Clique para focar o circuito';
    const box = li.querySelector('input');
    box.checked = etapa?.circOn.get(c.id) !== false;
    box.addEventListener('click', (ev) => ev.stopPropagation());
    box.addEventListener('change', () => { etapa?.circOn.set(c.id, box.checked); applyVisibility(); });
    li.addEventListener('click', () => {
      const pts = data.postes.filter((p) => p.c === c.id);
      if (pts.length) focusBox(bboxWorld(pts));
    });
    return li;
  }));

  $('search-list').replaceChildren(...[...(state.net?.index.keys() ?? [])].sort().map((code) => {
    const o = document.createElement('option');
    o.value = code;
    return o;
  }));
}

function renderLayers() {
  const basemap = document.createElement('select');
  for (const [key, b] of Object.entries(BASEMAPS)) basemap.add(new Option(b.label, key, false, key === state.basemapKey));
  basemap.addEventListener('change', () => { state.basemapKey = basemap.value; if (state.data) applyBasemap(); });

  const detail = document.createElement('select');
  detail.add(new Option('Imagem: resolução normal', '1'));
  detail.add(new Option('Imagem: alta resolução (4× ladrilhos)', '2'));
  detail.value = String(state.mapDetail);
  detail.addEventListener('change', () => { state.mapDetail = Number(detail.value); if (state.data) applyBasemap(); });

  const toggles = LAYERS.map((l) => {
    const linha = document.createElement('div');
    linha.className = 'layer-row';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = state.layerOn[l.key];
    box.addEventListener('change', () => { state.layerOn[l.key] = box.checked; applyVisibility(); });
    const cor = document.createElement('i');
    cor.className = 'swatch';
    cor.style.background = l.cor;
    const nome = document.createElement('button');
    nome.type = 'button';
    nome.className = 'layer-nome';
    nome.textContent = l.label;
    nome.title = 'Clique para ir a um item aleatório desta camada';
    nome.addEventListener('click', () => irParaAleatorio(l));
    const qtd = document.createElement('span');
    qtd.className = 'layer-qtd';
    const amostra = state.net?.amostras?.[l.key] ?? state.contexto?.amostras?.[l.key];
    qtd.textContent = amostra ? fmt(amostra.total) : '';
    linha.append(box, cor, nome, qtd);
    return linha;
  });

  const width = document.createElement('label');
  width.style.display = 'grid';
  width.innerHTML = '<span>Espessura dos cabos</span><input type="range" min="1" max="6" step="0.5">';
  const range = width.querySelector('input');
  range.value = state.lineWidth;
  range.addEventListener('input', () => { state.lineWidth = Number(range.value); applyLineWidth(); });

  const alcance = document.createElement('label');
  alcance.style.display = 'grid';
  alcance.title = 'Prédios e árvores são carregados só dentro deste raio e à frente da câmera';
  alcance.innerHTML = '<span></span><input type="range" min="300" max="5000" step="100">';
  const alcanceRange = alcance.querySelector('input');
  const rotuloAlcance = () => { alcance.querySelector('span').textContent = `Alcance de prédios e árvores: ${fmt(state.alcance)} m`; };
  alcanceRange.value = state.alcance;
  rotuloAlcance();
  alcanceRange.addEventListener('input', () => {
    state.alcance = Number(alcanceRange.value);
    localStorage.setItem('alcance', state.alcance);
    rotuloAlcance();
    atualizarContexto(true);
  });

  $('layers').replaceChildren(basemap, detail, ...toggles, width, alcance);
}

// --- seleção e detalhes ---------------------------------------------------------------------
function showInfo(pick) {
  $('info-title').textContent = pick.title ?? '—';
  $('info-kind').textContent = pick.kind;
  $('info-table').replaceChildren(...pick.props.map(([k, v]) => {
    const tr = document.createElement('tr');
    const a = document.createElement('td'); a.textContent = k;
    const b = document.createElement('td'); b.textContent = v;
    tr.append(a, b);
    return tr;
  }));
  $('info').hidden = false;
  selection.position.copy(pick.pos);
  selection.visible = true;
  updateStreetView(pick.pos);
}

// Link para o Street View do ponto selecionado (sem chave de API: abre o Google numa aba nova).
let streetViewToken = 0;
async function updateStreetView(pos) {
  const link = $('info-streetview');
  link.hidden = true;
  if (!state.data) return;
  const token = ++streetViewToken;
  const { epsg, origem } = state.data.meta;
  try {
    const { lat, lon } = await fetchJson(`/api/geo?epsg=${epsg}&e=${origem.x + pos.x}&n=${origem.y - pos.z}`);
    if (token !== streetViewToken) return;
    link.href = `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lon}`;
    link.hidden = false;
  } catch {
    // sem conversão de coordenada, o link simplesmente não aparece
  }
}

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

const isVisible = (o) => { for (; o; o = o.parent) if (!o.visible) return false; return true; };

function hitAt(ev) {
  if (!state.net) return null;
  const rect = renderer.domElement.getBoundingClientRect();
  // na comparação, cada metade tem a sua câmera e o seu conjunto de objetos
  const meio = rect.width / 2;
  const noDireito = state.dividida && ev.clientX - rect.left > meio;
  const cam = noDireito ? cameraDir : camera;
  const largura = state.dividida ? meio : rect.width;
  const x0 = noDireito ? rect.left + meio : rect.left;
  pointer.set(((ev.clientX - x0) / largura) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(pointer, cam);
  raycaster.layers.mask = cam.layers.mask;
  raycaster.params.Points.threshold = camera.position.distanceTo(controls.target) * 0.008;
  raycaster.params.Line2 = { threshold: 6 };
  const etapa = noDireito ? state.etapas[state.direita] : etapaAtual();
  const alvos = [...(etapa?.net?.pickables ?? []), ...(state.contexto?.pickables ?? [])];
  // recursivo: os prédios entram e saem da cena conforme a câmera
  return raycaster.intersectObjects(alvos, true).find((h) => isVisible(h.object)) ?? null;
}

let downAt = null;
renderer.domElement.addEventListener('pointerdown', (ev) => { downAt = [ev.clientX, ev.clientY]; tween = null; });
renderer.domElement.addEventListener('pointerup', (ev) => {
  if (!downAt || ev.button !== 0 || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;
  const hit = hitAt(ev);
  if (hit) showInfo(hit.object.userData.pick(hit));
});
renderer.domElement.addEventListener('dblclick', (ev) => {
  const hit = hitAt(ev);
  if (hit) {
    const pick = hit.object.userData.pick(hit);
    showInfo(pick);
    flyTo(pick.pos, 60);
  } else if (state.basemap) {
    const g = raycaster.intersectObject(state.basemap, true)[0];
    if (g) flyTo(g.point, camera.position.distanceTo(controls.target) * 0.5);
  }
});

$('info-close').addEventListener('click', () => { $('info').hidden = true; selection.visible = false; });
window.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') $('info-close').click(); });

$('search-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  if (!state.net) return;
  const q = $('search').value.trim().toUpperCase();
  const code = state.net.index.has(q) ? q : [...state.net.index.keys()].find((k) => k.toUpperCase().includes(q));
  if (!q || !code) { setStatus(`Nada encontrado para "${$('search').value}"`, true); return; }
  const pick = state.net.index.get(code)();
  showInfo(pick);
  flyTo(pick.pos, 70);
});

// Escolher uma sugestão da lista já dispara a busca (o Enter é consumido pelo datalist).
$('search').addEventListener('input', (ev) => {
  if (ev.inputType === 'insertReplacementText' || ev.inputType === undefined) $('search-form').requestSubmit();
});

// --- envio de uma base para o acervo ----------------------------------------------------------
$('upload-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const arquivo = $('up-arquivo').files[0];
  if (!arquivo) return;
  const dados = new FormData();
  dados.append('arquivo', arquivo);
  dados.append('descricao', $('up-descricao').value.trim());
  dados.append('regiao', $('up-regiao').value);
  dados.append('visibilidade', $('up-visibilidade').value);
  setAcervoStatus(`Enviando ${arquivo.name} (${fmt(arquivo.size / 1048576, 1, 'MB')})…`);
  try {
    const body = await fetchJson('/api/acervo', { method: 'POST', body: dados });
    $('up-arquivo').value = '';
    $('up-descricao').value = '';
    // mostra a rede recém-enviada mesmo se o filtro estiver noutra região
    if ($('acervo-regiao').value && $('acervo-regiao').value !== body.regiao) $('acervo-regiao').value = body.regiao;
    await carregarAcervo(body.id);
    if (body.aviso) setAcervoStatus(body.aviso, true);
  } catch (err) {
    setAcervoStatus(`Erro no envio: ${err.message}`, true);
  }
});

// --- camadas KML/KMZ ----------------------------------------------------------------------------
function cartaoCamada(c) {
  const li = document.createElement('li');
  li.className = 'acervo-item';
  const titulo = document.createElement('b');
  titulo.textContent = c.arquivo;
  const marca = selo(c.visibilidade);
  if (marca) titulo.append(marca);
  const tipos = c.resumo.tipos ?? {};
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = [
    `${fmt(c.resumo.n)} feições`,
    tipos.ponto ? `${fmt(tipos.ponto)} pontos` : null,
    tipos.linha ? `${fmt(tipos.linha)} linhas` : null,
    tipos.area ? `${fmt(tipos.area)} áreas` : null,
  ].filter(Boolean).join(' · ');
  const quem = document.createElement('div');
  quem.className = 'meta';
  quem.textContent = `${c.usuario} · ${new Date(c.criado_em).toLocaleString('pt-BR')}`;
  const acoes = document.createElement('div');
  acoes.className = 'acoes';
  const naCena = state.importadas.some((i) => i.id === c.id);
  const mostrar = document.createElement('button');
  mostrar.textContent = naCena ? 'Tirar da cena' : 'Mostrar na cena';
  mostrar.disabled = !state.redeId;
  mostrar.title = state.redeId ? '' : 'Abra uma rede antes de posicionar a camada';
  mostrar.addEventListener('click', () => (naCena ? tirarCamada(c) : mostrarCamada(c)));
  acoes.append(mostrar);
  if (podeExcluir(c)) {
    const excluir = document.createElement('button');
    excluir.className = 'secundario';
    excluir.textContent = 'Excluir';
    excluir.addEventListener('click', () => excluirCamada(c));
    acoes.append(excluir);
  }
  li.append(titulo, meta);
  if (c.descricao) {
    const desc = document.createElement('div');
    desc.className = 'desc';
    desc.textContent = c.descricao;
    li.append(desc);
  }
  li.append(quem, acoes);
  return li;
}

function renderCamadas() {
  $('camadas').replaceChildren(...state.camadas.map(cartaoCamada));
}

async function carregarCamadas() {
  try {
    const { camadas } = await fetchJson('/api/camadas');
    state.camadas = camadas;
    renderCamadas();
    setContextStatus('kml-status', camadas.length
      ? `${fmt(camadas.length)} camada(s) importada(s)`
      : 'Nenhuma camada importada ainda.');
  } catch (err) {
    setContextStatus('kml-status', `Erro ao ler as camadas: ${err.message}`, true);
  }
}

async function mostrarCamada(c) {
  if (!state.redeId) return;
  setContextStatus('kml-status', `Posicionando ${c.arquivo} na rede…`);
  try {
    const dados = await fetchJson(`/api/camadas/${c.id}?rede=${encodeURIComponent(state.redeId)}`);
    const built = buildImportada({ ...dados, id: c.id });
    addContext(built);
    state.importadas.push({
      id: c.id, grupos: built.groups,
      pickables: built.pickables, lineMaterials: built.lineMaterials ?? [],
    });
    setContextStatus('kml-status', built.resumo);
    renderCamadas();
  } catch (err) {
    setContextStatus('kml-status', `Erro ao posicionar: ${err.message}`, true);
  }
}

function tirarCamada(c) {
  const posicao = state.importadas.findIndex((i) => i.id === c.id);
  if (posicao < 0) return;
  const camada = state.importadas[posicao];
  const contexto = state.contexto;
  if (contexto) {
    // sair das listas antes de descartar a geometria: o raycast não olha a cena, e um objeto
    // já descartado que continuasse na lista seria testado a cada clique
    const fora = new Set(camada.pickables);
    contexto.pickables = contexto.pickables.filter((p) => !fora.has(p));
    const materiais = new Set(camada.lineMaterials);
    contexto.lineMaterials = contexto.lineMaterials.filter((m) => !materiais.has(m));
    delete contexto.amostras.importado;
    for (const g of camada.grupos) {
      contexto.root.remove(g);
      for (const [chave, bucket] of contexto.buckets) if (bucket === g) contexto.buckets.delete(chave);
    }
  }
  camada.grupos.forEach(disposeObject);
  state.importadas.splice(posicao, 1);
  renderCamadas();
  renderLayers();
  setContextStatus('kml-status', `${c.arquivo} saiu da cena.`);
}

async function excluirCamada(c) {
  if (!window.confirm(`Excluir a camada "${c.arquivo}"?`)) return;
  try {
    tirarCamada(c);
    await fetchJson(`/api/camadas/${c.id}`, { method: 'DELETE' });
    carregarCamadas();
  } catch (err) {
    setContextStatus('kml-status', `Erro ao excluir: ${err.message}`, true);
  }
}

$('kml-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const arquivo = $('kml-arquivo').files[0];
  if (!arquivo) return;
  const dados = new FormData();
  dados.append('arquivo', arquivo);
  dados.append('descricao', $('kml-descricao').value.trim());
  dados.append('regiao', $('up-regiao').value);
  dados.append('visibilidade', $('kml-visibilidade').value);
  setContextStatus('kml-status', `Importando ${arquivo.name}…`);
  try {
    const body = await fetchJson('/api/camadas', { method: 'POST', body: dados });
    $('kml-arquivo').value = '';
    $('kml-descricao').value = '';
    await carregarCamadas();
    setContextStatus('kml-status', body.aviso ?? `${body.arquivo}: ${fmt(body.resumo.n)} feições importadas.`,
      !!body.aviso);
  } catch (err) {
    setContextStatus('kml-status', `Erro na importação: ${err.message}`, true);
  }
});

// --- projetos em etapas ----------------------------------------------------------------------
function cartaoProjeto(p) {
  const li = document.createElement('li');
  li.className = 'acervo-item';
  if (state.projeto?.id === p.id) li.classList.add('ativo');
  const titulo = document.createElement('b');
  titulo.textContent = p.nome;
  const marca = selo(p.visibilidade);
  if (marca) titulo.append(marca);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${p.regiao} · ${p.etapas.length} etapa(s) · ${p.usuario}`;
  li.append(titulo, meta);
  if (p.descricao) {
    const desc = document.createElement('div');
    desc.className = 'desc';
    desc.textContent = p.descricao;
    li.append(desc);
  }

  const lista = document.createElement('div');
  lista.className = 'meta';
  lista.textContent = p.etapas.map((e) => e.nome).join(' → ') || 'sem etapas ainda';
  li.append(lista);

  const acoes = document.createElement('div');
  acoes.className = 'acoes';
  const abrir = document.createElement('button');
  abrir.textContent = 'Abrir';
  abrir.disabled = !p.etapas.length;
  abrir.title = p.etapas.length ? '' : 'Acrescente ao menos uma etapa';
  abrir.addEventListener('click', () => abrirProjeto(p));
  acoes.append(abrir);
  if (state.usuario && (p.visibilidade !== 'privada' || p.dono === state.usuario.matricula)) {
    const add = document.createElement('button');
    add.className = 'secundario';
    add.textContent = 'Nova etapa';
    add.disabled = !state.redeId;
    add.title = state.redeId ? 'Usa a rede aberta agora como etapa'
      : 'Abra a rede que será a etapa antes de acrescentá-la';
    add.addEventListener('click', () => novaEtapa(p));
    const excluir = document.createElement('button');
    excluir.className = 'secundario';
    excluir.textContent = 'Excluir';
    excluir.addEventListener('click', () => excluirProjeto(p));
    acoes.append(add, excluir);
  }
  li.append(acoes);
  return li;
}

function renderProjetos() {
  $('projetos').replaceChildren(...state.projetos.map(cartaoProjeto));
}

async function carregarProjetos(abrir) {
  try {
    const { projetos } = await fetchJson('/api/projetos');
    state.projetos = projetos;
    renderProjetos();
    setContextStatus('projetos-status', projetos.length
      ? `${fmt(projetos.length)} projeto(s)`
      : 'Nenhum projeto ainda. Crie um e acrescente as etapas.');
    const alvo = projetos.find((p) => p.id === abrir);
    if (alvo) abrirProjeto(alvo);
  } catch (err) {
    setContextStatus('projetos-status', `Erro ao ler os projetos: ${err.message}`, true);
  }
}

async function novaEtapa(projeto) {
  const rede = state.acervo.find((r) => r.id === state.redeId);
  const sugestao = `Etapa ${projeto.etapas.length}`;
  const nome = window.prompt(
    `Nova etapa de "${projeto.nome}" com a rede aberta agora (${rede?.arquivo ?? state.redeId}).\n`
    + 'Nome da etapa:', sugestao);
  if (nome === null) return;
  try {
    await fetchJson(`/api/projetos/${projeto.id}/etapas`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nome: nome.trim() || sugestao, rede_id: state.redeId }),
    });
    carregarProjetos();
    setContextStatus('projetos-status', `Etapa acrescentada a "${projeto.nome}".`);
  } catch (err) {
    setContextStatus('projetos-status', `Erro ao criar a etapa: ${err.message}`, true);
  }
}

async function excluirProjeto(p) {
  if (!window.confirm(`Excluir o projeto "${p.nome}" e suas etapas? As redes continuam no acervo.`)) return;
  try {
    await fetchJson(`/api/projetos/${p.id}`, { method: 'DELETE' });
    if (state.projeto?.id === p.id) state.projeto = null;
    carregarProjetos();
  } catch (err) {
    setContextStatus('projetos-status', `Erro ao excluir: ${err.message}`, true);
  }
}

$('projeto-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const nome = $('proj-nome').value.trim();
  if (!nome) return;
  try {
    await fetchJson('/api/projetos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        nome,
        descricao: $('proj-descricao').value.trim(),
        regiao: $('up-regiao').value,
        visibilidade: $('proj-visibilidade').value,
      }),
    });
    $('proj-nome').value = '';
    $('proj-descricao').value = '';
    carregarProjetos();
  } catch (err) {
    setContextStatus('projetos-status', `Erro ao criar o projeto: ${err.message}`, true);
  }
});

$('dividir-tela').addEventListener('change', (ev) => dividirTela(ev.target.checked));
$('etapa-direita').addEventListener('change', (ev) => {
  state.direita = Number(ev.target.value);
  $('rotulo-dir').textContent = state.etapas[state.direita]?.nome ?? '';
});

// --- exportação para KMZ --------------------------------------------------------------------
$('exportar-kmz').addEventListener('click', async () => {
  const etapa = etapaAtual();
  if (!etapa) { setContextStatus('exportar-status', 'Abra uma rede antes de exportar.', true); return; }
  const camadas = Object.entries(state.layerOn).filter(([, ligada]) => ligada).map(([k]) => k);
  const circuitos = [...etapa.circOn.entries()].filter(([, ligado]) => ligado).map(([id]) => id);
  const botao = $('exportar-kmz');
  botao.disabled = true;
  setContextStatus('exportar-status', 'Montando o KMZ no servidor…');
  try {
    const res = await fetch('/api/exportar/kmz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: etapa.rede_id,
        camadas,
        circuitos,
        importadas: state.importadas.map((i) => i.id),
      }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
    const blob = await res.blob();
    const nome = (res.headers.get('content-disposition') ?? '').match(/filename="(.+?)"/)?.[1]
      ?? `${etapa.nome}.kmz`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = nome;
    link.click();
    URL.revokeObjectURL(url);
    setContextStatus('exportar-status',
      `${nome} · ${fmt(blob.size / 1048576, 2, 'MB')} · ${camadas.length} camada(s) e ${circuitos.length} circuito(s)`);
  } catch (err) {
    setContextStatus('exportar-status', `Erro ao exportar: ${err.message}`, true);
  } finally {
    botao.disabled = false;
  }
});

// --- sessão e permissões -----------------------------------------------------------------------
function aplicarPermissoes() {
  const dentro = !!state.usuario;
  const soLeitura = ['upload-form', 'kml-form', 'projeto-form'];
  for (const id of soLeitura) {
    for (const campo of $(id).querySelectorAll('input, select, button')) campo.disabled = !dentro;
  }
  $('conta-nome').innerHTML = '';
  if (dentro) {
    const b = document.createElement('b');
    b.textContent = state.usuario.nome;
    $('conta-nome').append(b);
  } else if (state.sso) {
    $('conta-nome').textContent = 'Modo Visualização';
  }
  $('entrar').hidden = dentro || !state.sso;
  $('sair').hidden = !dentro || !state.sso;

  const aviso = $('aviso-visualizacao');
  aviso.hidden = dentro || !state.sso;
  if (!aviso.hidden) {
    aviso.textContent = 'Modo Visualização: você vê e exporta as redes públicas, mas não pode enviar, '
      + 'importar nem excluir. Entre com sua conta do Atlas para liberar essas ações.';
  }
}

async function carregarSessao() {
  try {
    const dados = await fetchJson('/api/sessao');
    state.usuario = dados.usuario;
    state.sso = dados.sso;
    state.atlas = dados.atlas;
    aplicarPermissoes();
    return dados;
  } catch {
    state.usuario = null;
    aplicarPermissoes();
    return { usuario: null, sso: false, login_automatico: false };
  }
}

$('entrar').addEventListener('click', () => { location.href = '/auth/entrar'; });
$('sair').addEventListener('click', async () => {
  await fetch('/auth/sair', { method: 'POST' });
  location.href = '/';
});

/**
 * Login automático: quem já está logado no Atlas entra sem clicar em nada.
 *
 * A tentativa acontece num quadro escondido, e não trocando a página: se o Atlas estiver fora do
 * ar ou demorar, o usuário continua no modo Visualização em vez de cair numa página de erro.
 * É tentada uma vez por aba (marca em sessionStorage), para não repetir a cada recarga.
 */
function tentarLoginAutomatico(dados) {
  const params = new URLSearchParams(location.search);
  const resultado = params.get('login');
  if (resultado) {
    // limpa o ?login=… da barra de endereços, mantendo o resto
    const motivo = params.get('motivo');
    params.delete('login');
    params.delete('motivo');
    history.replaceState(null, '', params.toString() ? `?${params}` : location.pathname);
    if (resultado === 'erro') setStatus(`Login recusado: ${motivo ?? 'ticket inválido'}`, true);
    if (resultado !== 'ok') sessionStorage.setItem('sso-tentado', '1');
    return;
  }
  if (!dados.login_automatico || dados.usuario || sessionStorage.getItem('sso-tentado')) return;
  sessionStorage.setItem('sso-tentado', '1');

  const quadro = document.createElement('iframe');
  quadro.style.cssText = 'position:absolute;width:0;height:0;border:0;visibility:hidden';
  quadro.src = '/auth/entrar?auto=1&quadro=1';
  let encerrado = false;
  const encerrar = async () => {
    if (encerrado) return;
    encerrado = true;
    clearTimeout(limite);
    quadro.remove();
    const novo = await carregarSessao();
    if (novo.usuario) {
      // entrou: as listas precisam vir de novo, agora com o que é privado dele
      carregarAcervo();
      carregarCamadas();
      carregarProjetos();
    }
  };
  quadro.addEventListener('load', () => {
    try {
      // enquanto está no Atlas a origem é outra e a leitura lança; ao voltar, encerramos
      if (quadro.contentWindow.location.origin === location.origin) encerrar();
    } catch {
      // ainda no Atlas: espera o próximo load
    }
  });
  const limite = setTimeout(encerrar, 8000); // Atlas fora do ar: desiste sem incomodar
  document.body.append(quadro);
}

// --- painel lateral: largura ajustável e grupos recolhíveis ------------------------------------
function configurarResizer() {
  const alca = $('resizer');
  const aplicar = (px) => {
    const largura = THREE.MathUtils.clamp(px, 220, 700);
    document.documentElement.style.setProperty('--sidebar-w', `${largura}px`);
    return largura;
  };
  const salvo = Number(localStorage.getItem('sidebar-w'));
  if (salvo) aplicar(salvo);
  // listeners no documento: funcionam mesmo se o ponteiro sair da alça durante o arraste
  const mover = (ev) => localStorage.setItem('sidebar-w', aplicar(ev.clientX));
  const soltar = () => {
    alca.classList.remove('ativo');
    document.removeEventListener('pointermove', mover);
    document.removeEventListener('pointerup', soltar);
  };
  alca.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    alca.classList.add('ativo');
    document.addEventListener('pointermove', mover);
    document.addEventListener('pointerup', soltar);
  });
}

function prepararSecoes() {
  document.querySelectorAll('.sidebar section').forEach((secao) => {
    const titulo = secao.querySelector('h2');
    if (!titulo) return;
    const corpo = document.createElement('div');
    corpo.className = 'secao-corpo';
    while (titulo.nextSibling) corpo.append(titulo.nextSibling);
    secao.append(corpo);
    titulo.classList.add('clicavel');
    const chave = `secao:${titulo.id || titulo.textContent.trim()}`;
    if (localStorage.getItem(chave) === '1') secao.classList.add('recolhida');
    titulo.addEventListener('click', () => {
      localStorage.setItem(chave, secao.classList.toggle('recolhida') ? '1' : '0');
    });
  });
}

// Regiões vêm do conf.ini: cada uma tem seu EPSG (SP = UTM 23S, ES = UTM 24S).
function preencherRegioes(cfg) {
  const regioes = cfg.regioes ?? [];
  const envio = $('up-regiao');
  envio.replaceChildren(...regioes.map((r) => new Option(`Região: ${r.sigla}`, r.sigla)));
  envio.value = localStorage.getItem('regiao') || cfg.regiao_padrao || regioes[0]?.sigla || '';
  envio.addEventListener('change', () => localStorage.setItem('regiao', envio.value));
  $('acervo-regiao').replaceChildren(new Option('Todas', ''), ...regioes.map((r) => new Option(r.sigla, r.sigla)));
}

async function init() {
  prepararSecoes();
  configurarResizer();
  $('acervo-busca').addEventListener('input', renderAcervo);
  $('acervo-regiao').addEventListener('change', renderAcervo);
  renderLayers();
  resize();
  try {
    const cfg = await fetchJson('/api/config');
    state.cfg = cfg;
    if (BASEMAPS[cfg.provedor_mapa]) state.basemapKey = cfg.provedor_mapa;
    document.title = `${cfg.titulo} · Interplan`;
    preencherRegioes(cfg);
    renderLayers();
  } catch (err) {
    setStatus(`Servidor indisponível: ${err.message}`, true);
    return;
  }
  const sessao = await carregarSessao();
  tentarLoginAutomatico(sessao);
  const params = new URLSearchParams(location.search);
  await Promise.all([carregarAcervo(params.get('rede') ?? undefined), carregarCamadas(),
    carregarProjetos(params.get('projeto') ?? undefined)]);
}

// Referência para diagnóstico pelo console do navegador (F12), útil para conferir o que está
// carregado quando alguém relata problema: expansion3d.etapas, .layerOn, .usuario…
window.expansion3d = state;

init();
