import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { decodeFloat32, dotTexture, fmt, groundHeight, toWorld } from './network.js';

export const CLEARANCE_COLORS = { contato: '#e0483e', critico: '#ff8a3d', atencao: '#f5d547' };
export const CLEARANCE_LABELS = { contato: 'Contato provável', critico: 'Crítico', atencao: 'Atenção' };
export const CONTEXT_ATTRIBUTION = 'Relevo: Copernicus DEM (ESA/UE) · Vegetação: Meta & WRI · Prédios: Overture Maps';

const TREE_CHUNK_M = 1000;
const LARGE_BUILDING_M2 = 3000;
const CELULA_PREDIOS_M = 800;  // lado da célula usada para carregar prédios conforme a câmera
const CELULAS_POR_QUADRO = 2;  // limite de células montadas por quadro, para não travar a navegação

const yieldToBrowser = () => new Promise((resolve) => setTimeout(resolve));

// --- árvores ---------------------------------------------------------------------------------
function painted(geometry, hex) {
  const g = geometry.index ? geometry.toNonIndexed() : geometry;
  const c = new THREE.Color(hex);
  const colors = new Float32Array(g.attributes.position.count * 3);
  for (let i = 0; i < colors.length; i += 3) colors.set([c.r, c.g, c.b], i);
  g.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  g.deleteAttribute('uv');
  return g;
}

const MAX_AMOSTRAS = 800; // itens sorteáveis por camada ao clicar no nome

// Geometrias são fundidas por bloco: acha o item dono do triângulo clicado.
function itemDoTriangulo(finais, itens, faceIndex) {
  let lo = 0, hi = finais.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (finais[mid] > faceIndex) hi = mid; else lo = mid + 1;
  }
  return itens[lo];
}

// Uma amostra a cada N itens, para o clique na camada poder sortear sem guardar tudo.
function amostrarLista(itens, montarPick) {
  const passo = Math.max(1, Math.ceil(itens.length / MAX_AMOSTRAS));
  const picks = [];
  for (let i = 0; i < itens.length; i += passo) picks.push(montarPick(itens[i]));
  return { total: itens.length, picks };
}

export function buildTrees(veg) {
  const group = new THREE.Group();
  group.userData = { layer: 'arvores', circ: null };
  const pickables = [];
  const xyhr = decodeFloat32(veg.arvores.xyhr);
  if (!xyhr.length) return { groups: [group], pickables };

  // árvore unitária (altura 1, raio de copa 1), escalada por instância
  const geometry = mergeGeometries([
    painted(new THREE.IcosahedronGeometry(1, 0).scale(1, 0.4, 1).translate(0, 0.62, 0), 0x3f7d3a),
    painted(new THREE.CylinderGeometry(0.07, 0.1, 0.4, 5).translate(0, 0.2, 0), 0x6b4f32),
  ]);
  const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.9, flatShading: true });

  const chunks = new Map();
  for (let i = 0; i < xyhr.length; i += 4) {
    const key = `${Math.floor(xyhr[i] / TREE_CHUNK_M)}|${Math.floor(xyhr[i + 1] / TREE_CHUNK_M)}`;
    if (!chunks.has(key)) chunks.set(key, []);
    chunks.get(key).push(i);
  }

  const arvorePick = (i) => {
    const [x, y, h, r] = xyhr.subarray(i, i + 4);
    return {
      title: 'Árvore', kind: 'Vegetação', pos: toWorld(x, y, h),
      props: [['Altura da copa', fmt(h, 0, 'm')], ['Raio de copa', `${fmt(r, 1, 'm')} (estimado)`], ['Fonte', veg.fonte]],
    };
  };

  const dummy = new THREE.Object3D();
  const tint = new THREE.Color();
  for (const list of chunks.values()) {
    const mesh = new THREE.InstancedMesh(geometry, material, list.length);
    list.forEach((i, k) => {
      const [x, y, h, r] = xyhr.subarray(i, i + 4);
      dummy.position.copy(toWorld(x, y, 0));
      dummy.scale.set(r, h, r);
      dummy.updateMatrix();
      mesh.setMatrixAt(k, dummy.matrix);
      mesh.setColorAt(k, tint.setScalar(0.8 + ((i * 2654435761) % 97) / 400));
    });
    mesh.computeBoundingSphere();
    mesh.userData.pick = (hit) => arvorePick(list[hit.instanceId]);
    group.add(mesh);
    pickables.push(mesh);
  }
  const indices = [];
  for (let i = 0; i < xyhr.length; i += 4) indices.push(i);
  return {
    groups: [group], pickables,
    gerenciador: gerenciadorPorDistancia('arvores', group),
    amostras: { arvores: amostrarLista(indices, (i) => () => arvorePick(i)) },
  };
}

// Acende/apaga blocos já montados conforme a distância e o campo de visão da câmera.
function gerenciadorPorDistancia(camada, group) {
  const frustum = new THREE.Frustum();
  const matriz = new THREE.Matrix4();
  return {
    camada,
    atualizar(camera, raio) {
      matriz.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
      frustum.setFromProjectionMatrix(matriz);
      for (const mesh of group.children) {
        const esfera = mesh.boundingSphere;
        if (!esfera) continue;
        const perto = esfera.center.distanceTo(camera.position) - esfera.radius <= raio;
        mesh.visible = perto && frustum.intersectsSphere(esfera);
      }
    },
  };
}

// --- vãos com vegetação próxima ----------------------------------------------------------------
export function clearancePick(r, veg, data, net) {
  const span = net.spanPick(data.vaos[r.i]);
  const limit = veg.limiares.at(-1).folga_menor_que;
  return {
    title: `Trecho ${r.id}`, kind: `Vegetação: ${CLEARANCE_LABELS[r.classe]}`, pos: toWorld(r.x, r.y, r.cabo),
    props: [
      ['Folga cabo–copa (mínima)', fmt(r.folga, 1, 'm')],
      ['Altura da copa', fmt(r.copa, 0, 'm')],
      ['Altura do cabo no ponto', fmt(r.cabo, 1, 'm')],
      [`Extensão com folga < ${fmt(limit, 1)} m`, fmt(r.extensao, 0, 'm')],
      ...span.props,
    ],
  };
}

export function buildClearance(veg, data, net) {
  const groups = new Map();
  const pickables = [], lineMaterials = [];
  const groupFor = (circ) => {
    if (!groups.has(circ)) {
      const g = new THREE.Group();
      g.userData = { layer: 'folga', circ };
      groups.set(circ, g);
    }
    return groups.get(circ);
  };

  const lists = new Map();
  for (const r of veg.criticos) {
    const key = `${r.c}|${r.classe}`;
    if (!lists.has(key)) lists.set(key, []);
    lists.get(key).push(r);
  }

  for (const list of lists.values()) {
    const { c, classe } = list[0];
    const positions = [], segRow = [];
    list.forEach((r, ri) => {
      const n = net.spanPositions(data.vaos[r.i], positions);
      for (let k = 0; k < n; k++) segRow.push(ri);
    });
    const geo = new LineSegmentsGeometry();
    geo.setPositions(positions);
    const mat = new LineMaterial({ color: new THREE.Color(CLEARANCE_COLORS[classe]), linewidth: 5, worldUnits: false });
    mat.userData.extraWidth = 3; // desenhado por cima do condutor, mais grosso
    lineMaterials.push(mat);
    const mesh = new LineSegments2(geo, mat);
    mesh.renderOrder = 2;
    mesh.userData.pick = (hit) => clearancePick(list[segRow[hit.faceIndex]], veg, data, net);
    groupFor(c).add(mesh);
    pickables.push(mesh);

    const pts = new THREE.BufferGeometry();
    pts.setAttribute('position', new THREE.Float32BufferAttribute(list.flatMap((r) => toWorld(r.x, r.y, r.cabo + 3).toArray()), 3));
    const points = new THREE.Points(pts, new THREE.PointsMaterial({
      color: CLEARANCE_COLORS[classe], size: 14, sizeAttenuation: false, map: dotTexture(), alphaTest: 0.5,
    }));
    points.userData.pick = (hit) => clearancePick(list[hit.index], veg, data, net);
    groupFor(c).add(points);
    pickables.push(points);
  }
  return {
    groups: [...groups.values()], pickables, lineMaterials,
    amostras: { folga: amostrarLista(veg.criticos, (r) => () => clearancePick(r, veg, data, net)) },
  };
}

// --- prédios ---------------------------------------------------------------------------------
export function buildBuildings(predios) {
  const group = new THREE.Group();
  group.userData = { layer: 'predios', circ: null };
  const predioPick = (b) => {
    let cx = 0, cy = 0;
    for (let i = 0; i < b.c.length; i += 2) { cx += b.c[i]; cy += b.c[i + 1]; }
    cx /= b.c.length / 2;
    cy /= b.c.length / 2;
    return {
      title: b.k ? `Edificação (${b.k})` : 'Edificação', kind: 'Prédio / casa', pos: toWorld(cx, cy, b.h),
      props: [
        ['Altura', `${fmt(b.h, 1, 'm')} ${b.e ? '(estimada pela área)' : '(informada)'}`],
        ['Área construída', fmt(b.a, 0, 'm²')],
        ['Fonte', predios.fonte],
      ],
    };
  };
  const materials = {
    comum: new THREE.MeshStandardMaterial({ color: 0xd9d3c5, roughness: 0.95 }),
    grande: new THREE.MeshStandardMaterial({ color: 0xaeb7bf, roughness: 0.85, metalness: 0.1 }),
  };

  // Os prédios são divididos em células; só as próximas da câmera e dentro do campo de visão
  // são montadas, e o que sai do alcance é descartado. Em rede metropolitana são 140 mil prédios.
  const conteudo = new THREE.Group();
  group.add(conteudo);
  const celulas = new Map();
  for (const b of predios.itens) {
    if (b.c.length < 6) continue;
    const chave = `${Math.floor(b.c[0] / CELULA_PREDIOS_M)}|${Math.floor(b.c[1] / CELULA_PREDIOS_M)}`;
    let celula = celulas.get(chave);
    if (!celula) {
      celulas.set(chave, celula = { itens: [], caixa: new THREE.Box3(), malhas: null });
    }
    celula.itens.push(b);
    const p = toWorld(b.c[0], b.c[1], 0);
    celula.caixa.expandByPoint(p);
    celula.caixa.expandByPoint(p.clone().setY(p.y + b.h));
  }
  for (const celula of celulas.values()) {
    celula.caixa.min.x -= 60; celula.caixa.min.z -= 60;   // folga para o contorno dos prédios
    celula.caixa.max.x += 60; celula.caixa.max.z += 60;
  }

  function montar(celula) {
    celula.malhas = [];
    for (const kind of ['comum', 'grande']) {
      const itens = celula.itens.filter((b) => (b.a >= LARGE_BUILDING_M2) === (kind === 'grande'));
      if (!itens.length) continue;
      const geometrias = [], guardados = [], finais = [];
      let triangulos = 0;
      for (const b of itens) {
        const contorno = [];
        let baixo = Infinity, alto = -Infinity;
        for (let i = 0; i < b.c.length; i += 2) {
          contorno.push(new THREE.Vector2(b.c[i], b.c[i + 1]));
          const g = groundHeight(b.c[i], b.c[i + 1]);
          baixo = Math.min(baixo, g);
          alto = Math.max(alto, g);
        }
        // a base acompanha o ponto mais baixo e o telhado fica b.h acima do ponto mais alto
        const geo = new THREE.ExtrudeGeometry(new THREE.Shape(contorno), { depth: b.h + alto - baixo, bevelEnabled: false });
        geo.rotateX(-Math.PI / 2).translate(0, baixo, 0);
        geo.deleteAttribute('uv');
        geometrias.push(geo);
        triangulos += geo.attributes.position.count / 3;
        finais.push(triangulos);
        guardados.push(b);
      }
      const mesh = new THREE.Mesh(mergeGeometries(geometrias), materials[kind]);
      geometrias.forEach((g) => g.dispose());
      mesh.userData.pick = (hit) => predioPick(itemDoTriangulo(finais, guardados, hit.faceIndex));
      conteudo.add(mesh);
      celula.malhas.push(mesh);
    }
  }

  function descartar(celula) {
    for (const mesh of celula.malhas ?? []) {
      conteudo.remove(mesh);
      mesh.geometry.dispose();
    }
    celula.malhas = null;
  }

  const frustum = new THREE.Frustum();
  const matriz = new THREE.Matrix4();
  let fila = [];
  const gerenciador = {
    camada: 'predios',
    montadas: 0,
    total: celulas.size,
    atualizar(camera, raio) {
      conteudo.visible = raio > 0;
      matriz.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
      frustum.setFromProjectionMatrix(matriz);
      fila = [];
      for (const celula of celulas.values()) {
        const perto = celula.caixa.distanceToPoint(camera.position) <= raio;
        if (!perto) {
          if (celula.malhas) descartar(celula);   // fora do alcance: libera a memória
          continue;
        }
        const naFrente = frustum.intersectsBox(celula.caixa);
        if (!celula.malhas) {
          if (naFrente) fila.push(celula);        // atrás da câmera: nem chega a montar
        } else {
          for (const mesh of celula.malhas) mesh.visible = naFrente;
        }
      }
      fila.sort((a, b) => a.caixa.distanceToPoint(camera.position) - b.caixa.distanceToPoint(camera.position));
      this.montadas = [...celulas.values()].filter((c) => c.malhas).length;
    },
    montarLote() {
      for (let i = 0; i < CELULAS_POR_QUADRO && fila.length; i++) montar(fila.shift());
    },
  };

  return {
    groups: [group], pickables: [group], gerenciador,
    amostras: { predios: amostrarLista(predios.itens, (b) => () => predioPick(b)) },
  };
}

// --- ruas ------------------------------------------------------------------------------------
const ROAD_KINDS = {
  motorway: 'principal', trunk: 'principal', primary: 'principal', secondary: 'principal', tertiary: 'principal',
  footway: 'pedestre', path: 'pedestre', steps: 'pedestre', cycleway: 'pedestre', pedestrian: 'pedestre',
};
const ROAD_COLORS = { principal: 0x5a5a62, local: 0x474750, pedestre: 0x5d5546 };
const ROAD_LIFT_M = 0.25; // levanta a faixa para não brigar com a imagem de satélite no relevo
const ROAD_STEP_M = 5; // reamostragem do eixo: sem isso a faixa vira uma reta e fura o relevo
const ROAD_CHUNK = 2000;

// Insere vértices a cada ROAD_STEP_M para que a faixa acompanhe a curvatura do terreno.
function densify(coords) {
  const out = [coords[0], coords[1]];
  for (let i = 2; i < coords.length; i += 2) {
    const x0 = coords[i - 2], y0 = coords[i - 1], x1 = coords[i], y1 = coords[i + 1];
    const steps = Math.max(1, Math.ceil(Math.hypot(x1 - x0, y1 - y0) / ROAD_STEP_M));
    for (let k = 1; k <= steps; k++) out.push(x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps);
  }
  return out;
}

export async function buildRoads(ruas, onProgress) {
  const group = new THREE.Group();
  group.userData = { layer: 'ruas', circ: null };
  const pickables = [];
  const ruaPick = (r) => ({
    title: r.nome ?? 'Via sem nome', kind: 'Rua / via',
    pos: toWorld(r.c[0], r.c[1], 0),
    props: [['Classe', r.k ?? '—'], ['Largura desenhada', `${fmt(r.w, 1, 'm')} (padrão por classe)`], ['Fonte', ruas.fonte]],
  });
  const materials = Object.fromEntries(Object.entries(ROAD_COLORS)
    .map(([k, color]) => [k, new THREE.MeshStandardMaterial({
      color, roughness: 1, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
    })]));

  const byKind = new Map();
  for (const r of ruas.itens) {
    const kind = ROAD_KINDS[r.k] ?? 'local';
    if (!byKind.has(kind)) byKind.set(kind, []);
    byKind.get(kind).push(r);
  }

  for (const [kind, items] of byKind) {
    for (let start = 0; start < items.length; start += ROAD_CHUNK) {
      const positions = [], triangleEnds = [], kept = [];
      let triangles = 0;
      for (const r of items.slice(start, start + ROAD_CHUNK)) {
        if (r.c.length < 4) continue;
        const c = densify(r.c);
        const n = c.length / 2;
        const half = r.w / 2;
        const left = [], right = [];
        for (let i = 0; i < n; i++) {
          const x = c[i * 2], y = c[i * 2 + 1];
          const a = Math.max(i - 1, 0), b = Math.min(i + 1, n - 1);
          let dx = c[b * 2] - c[a * 2], dy = c[b * 2 + 1] - c[a * 2 + 1];
          const len = Math.hypot(dx, dy) || 1;
          dx /= len; dy /= len;
          const h = groundHeight(x, y) + ROAD_LIFT_M;
          left.push([x - dy * half, h, -(y + dx * half)]);
          right.push([x + dy * half, h, -(y - dx * half)]);
        }
        for (let i = 0; i < n - 1; i++) {
          positions.push(...left[i], ...right[i], ...left[i + 1], ...left[i + 1], ...right[i], ...right[i + 1]);
        }
        triangles += (n - 1) * 2;
        triangleEnds.push(triangles);
        kept.push(r);
      }
      if (!kept.length) continue;
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
      geo.computeVertexNormals();
      const mesh = new THREE.Mesh(geo, materials[kind]);
      mesh.userData.pick = (hit) => ruaPick(itemDoTriangulo(triangleEnds, kept, hit.faceIndex));
      group.add(mesh);
      pickables.push(mesh);
      onProgress?.(Math.min(start + ROAD_CHUNK, items.length), items.length);
      await yieldToBrowser();
    }
  }
  return {
    groups: [group], pickables,
    amostras: { ruas: amostrarLista(ruas.itens, (r) => () => ruaPick(r)) },
  };
}
