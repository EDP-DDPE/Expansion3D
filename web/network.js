import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

export const LAYERS = [
  { key: 'postes', label: 'Postes', on: true, cor: '#8d8d8d' },
  { key: 'condutores', label: 'Condutores', on: true, cor: '#3aa0ff' },
  { key: 'trafos', label: 'Transformadores (ET)', on: true, cor: '#5b6b7a' },
  { key: 'cabines', label: 'Cabines de cliente MT (EP)', on: true, cor: '#c7a24a' },
  { key: 'chaves_rl', label: 'Religadoras (RL)', on: true, cor: '#2fb36b' },
  { key: 'chaves_cf', label: 'Chaves faca (CF)', on: true, cor: '#2fb36b' },
  { key: 'chaves_outras', label: 'Outras chaves', on: true, cor: '#2fb36b' },
  { key: 'equip', label: 'Reguladores e capacitores', on: true, cor: '#7a4fd6' },
  { key: 'gd', label: 'Geração distribuída', on: true, cor: '#f2c230' },
  { key: 'se', label: 'Subestação', on: true, cor: '#9b9a92' },
  { key: 'interlig', label: 'Interligações (socorro)', on: true, cor: '#ff4fd8' },
  { key: 'arvores', label: 'Árvores (corredor da rede)', on: true, cor: '#3f7d3a' },
  { key: 'folga', label: 'Vãos com vegetação próxima', on: true, cor: '#e0483e' },
  { key: 'predios', label: 'Prédios e casas', on: true, cor: '#d9d3c5' },
  { key: 'ruas', label: 'Ruas e vias', on: true, cor: '#5a5a62' },
  { key: 'marcadores', label: 'Marcadores de equipamentos', on: true, cor: '#9fb3c8' },
  { key: 'rotulos', label: 'Rótulos', on: true, cor: '#ffffff' },
  { key: 'importado', label: 'Camadas importadas (KML/KMZ)', on: true, cor: '#ff8a3d' },
];

// As chaves aparecem em três camadas, pela sigla do tipo no Interplan.
const switchLayer = (s) => (s.sigla === 'RL' ? 'chaves_rl' : s.sigla === 'CF' ? 'chaves_cf' : 'chaves_outras');
const SWITCH_LAYERS = ['chaves_rl', 'chaves_cf', 'chaves_outras'];
const MAX_AMOSTRAS = 800; // itens sorteáveis por camada ao clicar no nome

const COLORS = {
  poste: 0x8d8d8d, trafo: 0x5b6b7a, cabine: 0xc7a24a, fechada: 0x2fb36b, aberta: 0xe0483e,
  equip: 0x7a4fd6, gd: 0xf2c230, painel: 0x1f3b73, interlig: 0xff4fd8,
};

const SAG_SEGMENTS = 8;
const PARALLEL_DROP_M = 1.4;
// Spacer Cable: mensageiro no topo e fases em losango preso ao espaçador (~0,45 m de diagonal).
const SPACER_R = 0.22;
const SPACER_SPACING_M = 9; // espaçadores em losango ao longo do vão

export function decodeFloat32(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Float32Array(bytes.buffer);
}

// Relevo: grade regular (linha 0 = norte) com alturas relativas a terreno.base, amostrada de forma bilinear.
let groundAt = () => 0;
export function setTerrain(terreno) {
  if (!terreno) { groundAt = () => 0; return; }
  const h = decodeFloat32(terreno.h);
  const { x0, y1, dx, nx, ny } = terreno;
  groundAt = (x, y) => {
    const c = THREE.MathUtils.clamp((x - x0) / dx, 0, nx - 1);
    const r = THREE.MathUtils.clamp((y1 - y) / dx, 0, ny - 1);
    const c0 = Math.min(Math.floor(c), nx - 2), r0 = Math.min(Math.floor(r), ny - 2);
    const fc = c - c0, fr = r - r0, i = r0 * nx + c0;
    return (h[i] * (1 - fc) + h[i + 1] * fc) * (1 - fr) + (h[i + nx] * (1 - fc) + h[i + nx + 1] * fc) * fr;
  };
}
export const groundHeight = (x, y) => groundAt(x, y);

// Modelo: x = leste, y = norte (m, relativos à origem). Three.js: x = leste, y = altura sobre a base do relevo, z = -norte.
export const toWorld = (x, y, h = 0) => new THREE.Vector3(x, groundAt(x, y) + h, -y);

export const fmt = (v, digits = 0, unit = '') =>
  v == null ? '—' : `${Number(v).toLocaleString('pt-BR', { maximumFractionDigits: digits })}${unit ? ` ${unit}` : ''}`;

const stateLabel = (s) => (s === 0 ? 'Aberta' : s === 1 ? 'Fechada' : fmt(s));

function phaseSlots(v) {
  if (v.nf >= 3) return [0, 1, 2];
  if (v.nf === 1) return [{ A: 0, B: 1, C: 2 }[v.f] ?? 1];
  return [0, 2];
}

export function dotTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d');
  g.beginPath();
  g.arc(32, 32, 26, 0, Math.PI * 2);
  g.fillStyle = '#fff';
  g.fill();
  g.lineWidth = 8;
  g.strokeStyle = '#111';
  g.stroke();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function label(text, color) {
  const el = document.createElement('div');
  el.className = 'label3d';
  el.textContent = text;
  if (color) el.style.borderLeft = `3px solid ${color}`;
  return new CSS2DObject(el);
}

export function disposeObject(obj) {
  const materials = new Set();
  obj.traverse((o) => {
    o.geometry?.dispose();
    if (o.material) [].concat(o.material).forEach((m) => materials.add(m));
    if (o.isCSS2DObject) o.element.remove();
  });
  materials.forEach((m) => { m.map?.dispose(); m.dispose(); });
}

/**
 * Monta a rede 3D.
 *
 * Com etapas, várias redes convivem na mesma cena:
 *  - `camadaRender` põe tudo desta etapa numa camada do Three.js, para cada câmera escolher
 *    qual etapa enxerga (a camada 0 fica com relevo, árvores, casas e ruas, vistos por todas);
 *  - `deslocamento` recoloca a etapa no referencial da etapa de referência, já que cada base
 *    do Interplan tem a sua própria origem e a sua própria cota de base do relevo.
 */
export function buildNetwork(data, { camadaRender = 0, deslocamento = null } = {}) {
  setTerrain(data.terreno);
  const root = new THREE.Group();
  if (deslocamento) root.position.set(deslocamento.x, deslocamento.y, deslocamento.z);
  const buckets = new Map();
  const pickables = [];
  const lineMaterials = [];
  const index = new Map(); // código -> () => pick
  const circById = new Map(data.circuitos.map((c) => [c.id, c]));
  const circName = (id) => circById.get(id)?.codigo ?? '—';
  const circColor = (id) => circById.get(id)?.cor ?? '#cccccc';
  const poles = data.postes;
  const arr = data.arranjo;
  const armH = Math.max(...arr.fases.map((f) => f[1]));
  const poleH = armH + 1;
  const origin = data.meta.origem;

  const bucket = (layer, circ) => {
    const key = `${layer}|${circ ?? ''}`;
    let g = buckets.get(key);
    if (!g) {
      g = new THREE.Group();
      g.userData = { layer, circ: circ ?? null };
      buckets.set(key, g);
      root.add(g);
    }
    return g;
  };

  const byCirc = (items) => {
    const m = new Map();
    for (const it of items) {
      const k = it.c ?? null;
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(it);
    }
    return m;
  };

  // Ponto no entorno de um poste: "along" segue a linha, "across" segue a cruzeta.
  const nearPole = (p, along, across, h) => {
    const { x, y, rot } = poles[p];
    const cx = Math.cos(rot), sy = Math.sin(rot);
    return toWorld(x + cx * along - sy * across, y + sy * along + cx * across, h);
  };

  const dummy = new THREE.Object3D();
  function instanced(layer, items, geometry, material, place, pick, colorOf) {
    for (const [circ, list] of byCirc(items)) {
      const mesh = new THREE.InstancedMesh(geometry, material, list.length);
      list.forEach((it, i) => {
        dummy.position.set(0, 0, 0);
        dummy.rotation.set(0, 0, 0);
        dummy.scale.set(1, 1, 1);
        place(it, dummy);
        dummy.updateMatrix();
        mesh.setMatrixAt(i, dummy.matrix);
        if (colorOf) mesh.setColorAt(i, colorOf(it));
      });
      mesh.computeBoundingSphere();
      mesh.userData.pick = (hit) => pick(list[hit.instanceId]);
      bucket(layer, circ).add(mesh);
      pickables.push(mesh);
    }
  }

  function fatLines(layer, circ, positions, color, pickSegment, extraWidth = 0) {
    if (!positions.length) return;
    const geo = new LineSegmentsGeometry();
    geo.setPositions(positions);
    const mat = new LineMaterial({ color: new THREE.Color(color), linewidth: 2, worldUnits: false });
    mat.userData.extraWidth = extraWidth;
    lineMaterials.push(mat);
    const mesh = new LineSegments2(geo, mat);
    if (pickSegment) {
      mesh.userData.pick = (hit) => pickSegment(hit.faceIndex);
      pickables.push(mesh);
    }
    bucket(layer, circ).add(mesh);
  }

  const markers = [];
  const addMarker = (c, pos, color, pick) => markers.push({ c, pos, color: new THREE.Color(color), pick });
  const register = (code, pick) => { if (code) index.set(code, pick); };

  // Amostras por camada: alimentam o clique no nome da camada (vai a um item aleatório).
  const amostras = {};
  const amostrar = (layer, pick) => {
    const a = (amostras[layer] ??= { total: 0, picks: [] });
    a.total += 1;
    if (a.picks.length < MAX_AMOSTRAS) a.picks.push(pick);
  };

  // --- postes -----------------------------------------------------------------------------
  const poleGeo = mergeGeometries([
    new THREE.CylinderGeometry(0.1, 0.17, poleH, 8).translate(0, poleH / 2, 0),
    new THREE.BoxGeometry(2.4, 0.12, 0.12).translate(0, armH - 0.06, 0),
  ]);
  const polePick = (p) => ({
    title: p.cod, kind: 'Poste', pos: toWorld(p.x, p.y, poleH),
    props: [
      ['Circuito', circName(p.c)],
      ['Vãos conectados', p.grau],
      ['Barras no Interplan', p.barras],
      ['UTM (E ; N)', `${fmt(p.x + origin.x, 1)} ; ${fmt(p.y + origin.y, 1)}`],
    ],
  });
  instanced('postes', poles, poleGeo, new THREE.MeshStandardMaterial({ color: COLORS.poste, roughness: 0.9 }),
    (p, o) => { o.position.copy(toWorld(p.x, p.y)); o.rotation.y = p.rot + Math.PI / 2; }, polePick);
  poles.forEach((p) => { register(p.cod, () => polePick(p)); amostrar('postes', () => polePick(p)); });

  // --- condutores ---------------------------------------------------------------------------
  const spanPick = (v) => ({
    title: `Trecho ${v.id}`, kind: 'Vão de média tensão',
    pos: toWorld((poles[v.a].x + poles[v.b].x) / 2, (poles[v.a].y + poles[v.b].y) / 2, armH),
    props: [
      ['Circuito', circName(v.c)],
      ['Fases', v.f],
      ['Cabo', v.cabo ?? '—'],
      ['Formação', v.spacer ? 'Spacer Cable (losango, com mensageiro)' : 'Cabo nu em cruzeta'],
      ['Corrente admissível', fmt(v.iadm, 0, 'A')],
      ['Comprimento', fmt(v.l, 1, 'm')],
      ['Neutro', v.neutro ? 'Sim' : 'Não'],
      ['De / para poste', `${poles[v.a].cod} → ${poles[v.b].cod}`],
    ],
  });

  // Extremidade do fio na cruzeta, com a cruzeta orientada para o mesmo lado do vão.
  const armEnd = (p, nx, ny, off) => {
    const { x, y, rot } = poles[p];
    const ax = -Math.sin(rot), ay = Math.cos(rot);
    const side = Math.sign(ax * nx + ay * ny) || 1;
    return [x + ax * off * side, y + ay * off * side];
  };

  const pushCatenary = (out, [x0, y0], [x1, y1], h0, h1, sag) => {
    for (let k = 0; k < SAG_SEGMENTS; k++) {
      const t0 = k / SAG_SEGMENTS, t1 = (k + 1) / SAG_SEGMENTS;
      for (const t of [t0, t1]) {
        out.push(x0 + (x1 - x0) * t, h0 + (h1 - h0) * t - 4 * sag * t * (1 - t), -(y0 + (y1 - y0) * t));
      }
    }
  };

  // Fases do Spacer Cable em losango sob o mensageiro (que vai no topo, na posição da cruzeta).
  const spacerWires = (v) => {
    const center = armH - SPACER_R;
    const phases = [[-SPACER_R, center], [SPACER_R, center], [0, center - SPACER_R]];
    const used = v.nf >= 3 ? phases : v.nf === 1 ? [phases[2]] : [phases[0], phases[1]];
    return [[0, armH], ...used];
  };

  // Losangos do espaçador ao longo do vão, ligando mensageiro e fases na ordem topo-esq-baixo-dir.
  const spacerFrames = (v, out) => {
    const A = poles[v.a], B = poles[v.b];
    const L = Math.hypot(B.x - A.x, B.y - A.y);
    const count = Math.floor(L / SPACER_SPACING_M);
    if (count < 1) return;
    const nx = -(B.y - A.y) / L, ny = (B.x - A.x) / L;
    const sag = THREE.MathUtils.clamp(L * 0.015, 0.05, 1.2);
    const wires = spacerWires(v);
    const ring = wires.length === 4 ? [wires[0], wires[1], wires[3], wires[2]] : wires;
    const ends = ring.map(([off, h]) => {
      const hh = h - v.lvl * PARALLEL_DROP_M;
      const ea = armEnd(v.a, nx, ny, off), eb = armEnd(v.b, nx, ny, off);
      return { ea, eb, ha: groundAt(...ea) + hh, hb: groundAt(...eb) + hh };
    });
    for (let k = 1; k <= count; k++) {
      const t = k / (count + 1);
      const pts = ends.map(({ ea, eb, ha, hb }) => [
        ea[0] + (eb[0] - ea[0]) * t,
        ha + (hb - ha) * t - 4 * sag * t * (1 - t),
        -(ea[1] + (eb[1] - ea[1]) * t),
      ]);
      for (let i = 0; i < pts.length; i++) out.push(...pts[i], ...pts[(i + 1) % pts.length]);
    }
  };

  // Fios de um vão como pares de pontos; devolve quantos segmentos entraram em `out`.
  const spanPositions = (v, out, messengerOut) => {
    const A = poles[v.a], B = poles[v.b];
    const L = Math.hypot(B.x - A.x, B.y - A.y) || 1;
    const nx = -(B.y - A.y) / L, ny = (B.x - A.x) / L;
    const sag = THREE.MathUtils.clamp(L * 0.015, 0.05, 1.2);
    const wires = v.spacer ? spacerWires(v) : phaseSlots(v).map((k) => arr.fases[k]);
    if (v.neutro && !v.spacer) wires.push(arr.neutro);
    let segments = 0;
    wires.forEach(([off, h], i) => {
      const hh = h - v.lvl * PARALLEL_DROP_M;
      const ea = armEnd(v.a, nx, ny, off), eb = armEnd(v.b, nx, ny, off);
      const messenger = v.spacer && i === 0 && messengerOut;
      pushCatenary(messenger ? messengerOut : out, ea, eb, groundAt(...ea) + hh, groundAt(...eb) + hh, sag);
      if (!messenger) segments += SAG_SEGMENTS;
    });
    return segments;
  };

  for (const [circ, list] of byCirc(data.vaos)) {
    const positions = [];
    const messengers = [];
    const frames = [];
    const segSpan = [];
    list.forEach((v, si) => {
      const n = spanPositions(v, positions, messengers);
      for (let k = 0; k < n; k++) segSpan.push(si);
      if (v.spacer) spacerFrames(v, frames);
    });
    list.forEach((v) => amostrar('condutores', () => spanPick(v)));
    fatLines('condutores', circ, positions, circColor(circ), (seg) => spanPick(list[segSpan[seg]]));
    fatLines('condutores', circ, messengers, 0x4a4a4a, null, -0.5); // mensageiro do spacer, mais fino e escuro
    if (frames.length) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(frames, 3));
      bucket('condutores', circ).add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: 0x2a2a2a })));
    }
  }

  // --- transformadores de distribuição -----------------------------------------------------
  const trafoPick = (t) => ({
    title: t.cod, kind: t.tipo === 'EP' ? 'Cabine de cliente MT' : 'Transformador de distribuição',
    pos: nearPole(t.p, 0.6, 0, armH - 3),
    props: [
      ['Circuito', circName(t.c)],
      ['Potência', fmt(t.snom, 1, 'kVA')],
      ['Fases', t.fases],
      ['Tensão secundária', fmt(t.vsec, 0, 'V')],
      ...(t.lampadas ? [['Lâmpadas IP', t.lampadas]] : []),
      ...t.consumidores.map((m) => [`Cliente ${m.cod}`, `${m.nome ?? ''} · ${fmt(m.kwh, 0, 'kWh')}`]),
      ['Poste', poles[t.p].cod],
    ],
  });
  const etGeo = mergeGeometries([
    new THREE.CylinderGeometry(0.33, 0.33, 1.1, 14),
    new THREE.CylinderGeometry(0.36, 0.36, 0.08, 14).translate(0, 0.58, 0),
    new THREE.BoxGeometry(0.12, 0.8, 0.5).translate(-0.38, 0, 0),
  ]);
  const ets = data.trafos.filter((t) => t.tipo === 'ET');
  instanced('trafos', ets, etGeo, new THREE.MeshStandardMaterial({ color: COLORS.trafo, metalness: 0.4, roughness: 0.5 }),
    (t, o) => {
      o.position.copy(nearPole(t.p, 0.6 + t.k * 1.0, 0, armH - 3));
      o.rotation.y = poles[t.p].rot;
      const s = THREE.MathUtils.clamp(Math.cbrt((t.snom || 45) / 45), 0.7, 1.8);
      o.scale.set(s, s, s);
    }, trafoPick);

  const eps = data.trafos.filter((t) => t.tipo === 'EP');
  const cabinOffset = (t) => 8 + t.k * 7;
  instanced('cabines', eps, new THREE.BoxGeometry(4, 3, 5).translate(0, 1.5, 0),
    new THREE.MeshStandardMaterial({ color: COLORS.cabine, roughness: 0.8 }),
    (t, o) => { o.position.copy(nearPole(t.p, 0, cabinOffset(t), 0)); o.rotation.y = poles[t.p].rot; }, trafoPick);
  for (const [circ, list] of byCirc(eps)) {
    const pts = list.flatMap((t) => [...nearPole(t.p, 0, 0, armH - 0.5).toArray(), ...nearPole(t.p, 0, cabinOffset(t), 3).toArray()]);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    bucket('cabines', circ).add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: 0x222222 })));
  }
  data.trafos.forEach((t) => {
    const pick = () => trafoPick(t);
    register(t.cod, pick);
    amostrar(t.tipo === 'EP' ? 'cabines' : 'trafos', pick);
    const pos = t.tipo === 'EP' ? nearPole(t.p, 0, cabinOffset(t), 6) : nearPole(t.p, 0, 0, poleH + 2.5);
    addMarker(t.c, pos, t.tipo === 'EP' ? COLORS.cabine : 0x9fb3c8, pick);
  });

  // --- chaves -------------------------------------------------------------------------------
  const switchPick = (s) => ({
    title: s.cod, kind: s.tipo, pos: nearPole(s.p, -0.5, 0, armH - 1),
    props: [
      ['Circuito', circName(s.c)],
      ['Estado atual', stateLabel(s.estado)],
      ['Estado normal', stateLabel(s.normal)],
      ['Corrente nominal', fmt(s.corrente, 0, 'A')],
      ...(s.elo ? [['Elo', s.elo]] : []),
      ['Poste', poles[s.p].cod],
    ],
  });
  const openColor = new THREE.Color(COLORS.aberta), closedColor = new THREE.Color(COLORS.fechada);
  const switchGeo = new THREE.BoxGeometry(0.45, 0.8, 0.45);
  const switchMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.6, emissive: 0x111111 });
  for (const layer of SWITCH_LAYERS) {
    instanced(layer, data.chaves.filter((s) => switchLayer(s) === layer), switchGeo, switchMat,
      (s, o) => { o.position.copy(nearPole(s.p, -0.5 - s.k * 0.6, 0, armH - 1)); o.rotation.y = poles[s.p].rot; },
      switchPick, (s) => (s.estado === 0 ? openColor : closedColor));
  }
  data.chaves.forEach((s) => {
    const pick = () => switchPick(s);
    register(s.cod, pick);
    amostrar(switchLayer(s), pick);
    addMarker(s.c, nearPole(s.p, -0.5, 0, poleH + 4 + s.k * 1.5), s.estado === 0 ? COLORS.aberta : COLORS.fechada, pick);
  });

  // --- reguladores e capacitores ---------------------------------------------------------
  const equipMat = new THREE.MeshStandardMaterial({ color: COLORS.equip, metalness: 0.3, roughness: 0.5 });
  const regPick = (r) => ({
    title: r.cod, kind: 'Regulador de tensão', pos: nearPole(r.p, 0, 0, 5),
    props: [
      ['Circuito', circName(r.c)],
      ['Potência', fmt(r.snom, 1, 'kVA')],
      ['Corrente nominal', fmt(r.inom, 0, 'A')],
      ['Tensão nominal', fmt(r.vnom, 2, 'kV')],
      ['Passos', fmt(r.passos)],
      ['Faixa', fmt((r.faixa ?? 0) * 100, 1, '%')],
      ['Poste', poles[r.p].cod],
    ],
  });
  const regGeo = mergeGeometries([
    new THREE.BoxGeometry(2.8, 0.15, 1).translate(0, -0.1, 0),
    ...[-0.9, 0, 0.9].map((dx) => new THREE.CylinderGeometry(0.3, 0.3, 1.5, 12).translate(dx, 0.75, 0)),
  ]);
  instanced('equip', data.reguladores, regGeo, equipMat,
    (r, o) => { o.position.copy(nearPole(r.p, 0.9, 0, 4.2)); o.rotation.y = poles[r.p].rot + Math.PI / 2; }, regPick);

  const capPick = (c) => ({
    title: c.cod, kind: 'Banco de capacitores', pos: nearPole(c.p, 0.6, 0, armH - 2),
    props: [
      ['Circuito', circName(c.c)],
      ['Potência reativa', fmt(c.qnom, 0, 'kVAr')],
      ['Tensão nominal', fmt(c.vnom, 2, 'kV')],
      ['Poste', poles[c.p].cod],
    ],
  });
  const capGeo = mergeGeometries([
    new THREE.BoxGeometry(1.8, 0.7, 0.5),
    ...[-0.6, 0, 0.6].map((dx) => new THREE.CylinderGeometry(0.06, 0.09, 0.4, 8).translate(dx, 0.55, 0)),
  ]);
  instanced('equip', data.capacitores, capGeo, equipMat,
    (c, o) => { o.position.copy(nearPole(c.p, -0.7, 0, armH - 2.2)); o.rotation.y = poles[c.p].rot + Math.PI / 2; }, capPick);
  data.reguladores.forEach((r) => {
    const pick = () => regPick(r);
    register(r.cod, pick);
    amostrar('equip', pick);
    addMarker(r.c, nearPole(r.p, 0, 0, poleH + 4), COLORS.equip, pick);
  });
  data.capacitores.forEach((c) => {
    const pick = () => capPick(c);
    register(c.cod, pick);
    amostrar('equip', pick);
    addMarker(c.c, nearPole(c.p, 0, 0, poleH + 4), COLORS.equip, pick);
  });

  // --- geração distribuída --------------------------------------------------------------
  const gdPick = (g) => ({
    title: g.cod, kind: 'Geração distribuída (MT)', pos: nearPole(g.p, 0, -(8 + g.k * 6), 2),
    props: [
      ['Circuito', circName(g.c)],
      ['Potência', fmt(g.snom, 1, 'kVA')],
      ['Tensão', fmt(g.vnom, 1, 'kV')],
      ...(g.cliente ? [['Cliente', g.cliente]] : []),
      ...(g.carga ? [['Cabine', g.carga]] : []),
      ['Poste', poles[g.p].cod],
    ],
  });
  const panel = new THREE.BoxGeometry(1.7, 0.05, 1.1).rotateX(-0.45);
  const gdGeo = mergeGeometries([-1.8, 0, 1.8].flatMap((dx) => [
    panel.clone().translate(dx, 1.3, 0),
    new THREE.BoxGeometry(0.08, 1.2, 0.08).translate(dx, 0.6, 0),
  ]));
  instanced('gd', data.geradores, gdGeo, new THREE.MeshStandardMaterial({ color: COLORS.painel, metalness: 0.6, roughness: 0.3 }),
    (g, o) => { o.position.copy(nearPole(g.p, 0, -(8 + g.k * 6), 0)); o.rotation.y = poles[g.p].rot; }, gdPick);
  data.geradores.forEach((g) => {
    const pick = () => gdPick(g);
    register(g.cod, pick);
    amostrar('gd', pick);
    addMarker(g.c, nearPole(g.p, 0, -(8 + g.k * 6), 4), COLORS.gd, pick);
  });

  // --- interligações com outros alimentadores --------------------------------------------
  const tieGeo = new THREE.ConeGeometry(1.2, 3, 12).rotateX(Math.PI).translate(0, poleH + 5, 0);
  const tiePick = (t) => ({
    title: t.cod, kind: 'Interligação (socorro)', pos: toWorld(t.x, t.y, poleH + 5),
    props: [['Alimentador', t.de], ['Socorro por', t.para], ['Tipo', t.tipo ?? '—'], ['Corrente nominal', fmt(t.corrente, 0, 'A')]],
  });
  instanced('interlig', data.interligacoes, tieGeo, new THREE.MeshStandardMaterial({ color: COLORS.interlig, emissive: 0x440033 }),
    (t, o) => o.position.copy(toWorld(t.x, t.y)), tiePick);
  data.interligacoes.forEach((t) => { register(t.cod, () => tiePick(t)); amostrar('interlig', () => tiePick(t)); });

  // --- subestações e saídas de alimentadores ----------------------------------------------
  for (const se of data.subestacoes) buildSubstation(se);

  function buildSubstation(se) {
    const W = 56, D = 44;
    const g = new THREE.Group();
    g.position.copy(toWorld(se.x, se.y));
    g.rotation.y = se.rot;
    g.updateMatrixWorld(true);
    const local = (x, y, z) => g.localToWorld(new THREE.Vector3(x, y, z));
    const mat = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.8, ...extra });
    const add = (geo, material, x, y, z, pick) => {
      const m = new THREE.Mesh(geo, material);
      m.position.set(x, y, z);
      g.add(m);
      if (pick) { m.userData.pick = pick; pickables.push(m); }
      return m;
    };
    const wires = [];
    const wire = (a, b) => wires.push(...a.toArray(), ...b.toArray());

    const sePick = () => ({
      title: `SE ${se.nome || se.cod}`, kind: 'Subestação', pos: local(0, 8, 0),
      props: [
        ['Código', se.cod],
        ['Tensões', `${fmt(se.valta, 1)} / ${fmt(se.vmedia, 1)} kV`],
        ['Transformadores', se.trafos.map((t) => `${t.cod} (${fmt(t.snom, 1)} MVA)`).join(', ') || '—'],
        ['Alimentadores', se.alimentadores.map((a) => circName(a.circ)).join(', ')],
      ],
    });
    register(se.cod, sePick);
    amostrar('se', sePick);
    // base terraplenada: estende-se abaixo do pátio para não deixar vão sobre o relevo em declive
    add(new THREE.BoxGeometry(W, 4.3, D), mat(0x9b9a92), 0, -2, 0, sePick);

    const fence = mat(0xd0d6da, { transparent: true, opacity: 0.35, side: THREE.DoubleSide });
    add(new THREE.BoxGeometry(W, 2.5, 0.05), fence, 0, 1.55, D / 2);
    add(new THREE.BoxGeometry(W, 2.5, 0.05), fence, 0, 1.55, -D / 2);
    add(new THREE.BoxGeometry(0.05, 2.5, D), fence, W / 2, 1.55, 0);
    add(new THREE.BoxGeometry(0.05, 2.5, D), fence, -W / 2, 1.55, 0);
    add(new THREE.BoxGeometry(10, 3.6, 6), mat(0xd9d2c3), 14, 2.1, D / 2 - 6, sePick);

    const steel = mat(0xb8bec4, { metalness: 0.6, roughness: 0.4 });
    const porcelain = mat(0x8b5a3c);
    const trafoCount = Math.max(se.trafos.length, 1);
    se.trafos.forEach((t, i) => {
      const z = (i - (trafoCount - 1) / 2) * 12;
      const pick = () => ({
        title: t.cod, kind: 'Transformador de força', pos: local(8, 6, z),
        props: [['Potência', fmt(t.snom, 1, 'MVA')], ['Tensões', `${fmt(t.valta, 1)} / ${fmt(t.vmedia, 1)} kV`], ['Subestação', se.nome]],
      });
      register(t.cod, pick);
      add(new THREE.BoxGeometry(5, 4.2, 6.5), mat(0x6f7d88, { metalness: 0.3 }), 8, 2.4, z, pick);
      add(new THREE.BoxGeometry(0.6, 3.2, 5.5), mat(0x5d6a74), 11, 2.2, z, pick);
      add(new THREE.BoxGeometry(0.6, 3.2, 5.5), mat(0x5d6a74), 5, 2.2, z, pick);
      add(new THREE.CylinderGeometry(0.5, 0.5, 4, 12), mat(0x6f7d88), 7, 5.7, z).rotation.x = Math.PI / 2;
      for (const k of [-1, 0, 1]) {
        add(new THREE.CylinderGeometry(0.14, 0.2, 2.6, 10), porcelain, 9.4, 5.8, z + k * 1.6);
        add(new THREE.CylinderGeometry(0.1, 0.14, 1.2, 10), porcelain, 6.4, 5.1, z + k * 1.0);
        wire(local(9.4, 7.1, z + k * 1.6), local(W / 2 - 5, 13.5, k * 2.2));
        wire(local(6.4, 5.7, z + k * 1.0), local(-8, 7.6, z + k * 0.8));
      }
    });

    // pórtico de entrada da alta tensão e linha de transmissão
    for (const z of [-7, 7]) add(new THREE.BoxGeometry(0.5, 14, 0.5), steel, W / 2 - 5, 7, z);
    add(new THREE.BoxGeometry(0.4, 0.5, 14.5), steel, W / 2 - 5, 13.8, 0);
    add(new THREE.CylinderGeometry(0.4, 1.8, 24, 4), steel, W / 2 + 45, 12, 0);
    for (const k of [-1, 0, 1]) wire(local(W / 2 - 5, 13.5, k * 2.2), local(W / 2 + 45, 20, k * 2.8));

    // barramento de média tensão
    for (const z of [-(D / 2 - 4), D / 2 - 4]) add(new THREE.BoxGeometry(0.4, 8, 0.4), steel, -8, 4, z);
    add(new THREE.BoxGeometry(0.4, 0.4, D - 7.5), steel, -8, 8, 0);
    for (const k of [-1, 0, 1]) wire(local(-8 + k * 0.8, 7.6, -(D / 2 - 4)), local(-8 + k * 0.8, 7.6, D / 2 - 4));

    const n = se.alimentadores.length;
    se.alimentadores.forEach((a, i) => {
      const z = (i - (n - 1) / 2) * 6;
      const circ = circById.get(a.circ);
      const breakerPick = () => ({
        title: a.disjuntor ?? `Disjuntor ${circName(a.circ)}`, kind: 'Disjuntor de alimentador', pos: local(-14, 2.6, z),
        props: [['Alimentador', circName(a.circ)], ['Estado', stateLabel(a.estado)], ['Subestação', se.nome]],
      });
      register(a.disjuntor, breakerPick);
      add(new THREE.BoxGeometry(1.4, 2.3, 1.4), mat(a.estado === 0 ? COLORS.aberta : COLORS.fechada), -14, 1.45, z, breakerPick);
      for (const dz of [-1.4, 1.4]) add(new THREE.BoxGeometry(0.3, 9, 0.3), steel, -W / 2 + 3, 4.5, z + dz);
      add(new THREE.BoxGeometry(0.3, 0.3, 3.2), steel, -W / 2 + 3, 9, z);
      const lbl = label(circName(a.circ), circ?.cor);
      lbl.position.copy(local(-W / 2 + 3, 11, z));
      bucket('rotulos', a.circ).add(lbl);

      if (!circ || circ.poste0 == null) return;
      const P = poles[circ.poste0];
      const exits = [-1, 0, 1].map((k) => local(-W / 2 + 3, 8.8, z + k * 0.8));
      const dx = P.x - exits[1].x, dy = P.y + exits[1].z;
      const L = Math.hypot(dx, dy) || 1;
      const positions = [];
      exits.forEach((e, k) => {
        for (const [p0, p1] of [[local(-8 + (k - 1) * 0.8, 7.6, z), local(-14, 2.6, z + (k - 1) * 0.4)], [local(-14, 2.6, z + (k - 1) * 0.4), e]]) {
          wire(p0, p1);
        }
        const [ex, ey] = armEnd(circ.poste0, -dy / L, dx / L, arr.fases[k][0]);
        pushCatenary(positions, [e.x, -e.z], [ex, ey], e.y, groundAt(ex, ey) + arr.fases[k][1], THREE.MathUtils.clamp(L * 0.015, 0.05, 1.2));
      });
      fatLines('condutores', a.circ, positions, circ.cor, () => breakerPick());
    });

    const wireGeo = new THREE.BufferGeometry();
    wireGeo.setAttribute('position', new THREE.Float32BufferAttribute(wires, 3));
    g.updateMatrixWorld(true);
    const inverse = g.matrixWorld.clone().invert();
    wireGeo.applyMatrix4(inverse);
    g.add(new THREE.LineSegments(wireGeo, new THREE.LineBasicMaterial({ color: 0x333333 })));

    const seLabel = label(`SE ${se.nome || se.cod}`);
    seLabel.position.copy(local(0, 20, 0));
    bucket('rotulos', null).add(seLabel);
    bucket('se', null).add(g);
  }

  // --- rótulos dos circuitos no primeiro poste --------------------------------------------
  for (const c of data.circuitos) {
    if (c.poste0 == null || data.subestacoes.some((se) => se.alimentadores.some((a) => a.circ === c.id))) continue;
    const lbl = label(c.codigo, c.cor);
    lbl.position.copy(toWorld(poles[c.poste0].x, poles[c.poste0].y, poleH + 8));
    bucket('rotulos', c.id).add(lbl);
  }

  // --- marcadores (pontos com tamanho fixo em tela, visíveis de longe) ---------------------
  const markerMat = new THREE.PointsMaterial({ size: 10, sizeAttenuation: false, vertexColors: true, map: dotTexture(), alphaTest: 0.5 });
  for (const [circ, list] of byCirc(markers)) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(list.flatMap((m) => m.pos.toArray()), 3));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(list.flatMap((m) => m.color.toArray()), 3));
    const pts = new THREE.Points(geo, markerMat);
    pts.userData.pick = (hit) => list[hit.index].pick();
    bucket('marcadores', circ).add(pts);
    pickables.push(pts);
  }

  if (camadaRender) aplicarCamada(root, camadaRender);
  return { root, buckets, pickables, lineMaterials, index, amostras, poleH, spanPositions, spanPick,
    circName, camadaRender };
}

/** Marca o objeto e tudo dentro dele numa camada de render (usado para separar as etapas). */
export function aplicarCamada(objeto, camada) {
  objeto.traverse((o) => o.layers.set(camada));
}
