import * as THREE from 'three';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { fmt, groundHeight, toWorld } from './network.js';

const COR_PADRAO = '#ff8a3d';
const ALTURA_PONTO = 2; // ponto sem altitude fica um pouco acima do chão, para não sumir no relevo

// Altitude do KML costuma vir zerada; nesse caso o ponto acompanha o relevo.
const altura = (x, y, alt) => (alt ? alt - groundHeight(x, y) : ALTURA_PONTO);

/**
 * Monta em 3D uma camada importada de KML/KMZ.
 * As feições chegam já no referencial da rede (metros a partir da origem dela).
 */
export function buildImportada(camada, { camadaRender = 0 } = {}) {
  const group = new THREE.Group();
  group.userData = { layer: 'importado', circ: null, camadaId: camada.id };
  const pickables = [];
  const lineMaterials = [];
  const picks = [];

  const propsDe = (f, tipo) => () => ({
    title: f.nome || tipo,
    kind: `${tipo} · ${camada.nome}`,
    pos: toWorld(f.coords[0][0], f.coords[0][1], altura(f.coords[0][0], f.coords[0][1], f.coords[0][2])),
    props: [['Camada', camada.nome], ...(f.props ?? []).map(([k, v]) => [k, v])],
  });

  // --- pontos: uma esfera instanciada por feição ---------------------------------------------
  const pontos = camada.feicoes.filter((f) => f.tipo === 'ponto' && f.coords.length);
  if (pontos.length) {
    const malha = new THREE.InstancedMesh(
      new THREE.SphereGeometry(1.6, 10, 8),
      new THREE.MeshStandardMaterial({ roughness: 0.5, vertexColors: true }),
      pontos.length,
    );
    const molde = new THREE.Object3D();
    pontos.forEach((f, i) => {
      const [x, y, alt] = f.coords[0];
      molde.position.copy(toWorld(x, y, altura(x, y, alt)));
      molde.updateMatrix();
      malha.setMatrixAt(i, molde.matrix);
      malha.setColorAt(i, new THREE.Color(f.cor || COR_PADRAO));
    });
    malha.computeBoundingSphere();
    malha.userData.pick = (hit) => propsDe(pontos[hit.instanceId], 'Ponto importado')();
    group.add(malha);
    pickables.push(malha);
    pontos.forEach((f) => picks.push(propsDe(f, 'Ponto importado')));
  }

  // --- linhas e áreas: agrupadas por cor, para virar poucos objetos ----------------------------
  const porCor = new Map();
  for (const f of camada.feicoes) {
    if (f.tipo === 'ponto' || f.coords.length < 2) continue;
    const cor = f.cor || COR_PADRAO;
    if (!porCor.has(cor)) porCor.set(cor, []);
    porCor.get(cor).push(f);
  }
  for (const [cor, feicoes] of porCor) {
    const posicoes = [];
    const feicaoDoSegmento = [];
    for (const f of feicoes) {
      const pontosMundo = f.coords.map(([x, y, alt]) => toWorld(x, y, altura(x, y, alt)));
      // área fecha o anel; linha vai só de ponta a ponta
      const seq = f.tipo === 'area' ? [...pontosMundo, pontosMundo[0]] : pontosMundo;
      for (let i = 0; i < seq.length - 1; i++) {
        posicoes.push(seq[i].x, seq[i].y, seq[i].z, seq[i + 1].x, seq[i + 1].y, seq[i + 1].z);
        feicaoDoSegmento.push(f);
      }
      picks.push(propsDe(f, f.tipo === 'area' ? 'Área importada' : 'Linha importada'));
    }
    if (!posicoes.length) continue;
    const geo = new LineSegmentsGeometry();
    geo.setPositions(posicoes);
    const mat = new LineMaterial({ color: new THREE.Color(cor), linewidth: 3, worldUnits: false });
    mat.userData.extraWidth = 1;
    lineMaterials.push(mat);
    const malha = new LineSegments2(geo, mat);
    malha.userData.pick = (hit) => {
      const f = feicaoDoSegmento[hit.faceIndex] ?? feicoes[0];
      return propsDe(f, f.tipo === 'area' ? 'Área importada' : 'Linha importada')();
    };
    group.add(malha);
    pickables.push(malha);
  }

  if (camadaRender) group.traverse((o) => o.layers.set(camadaRender));

  return {
    groups: [group],
    pickables,
    lineMaterials,
    amostras: { importado: { total: camada.n, picks: picks.slice(0, 800) } },
    resumo: `${fmt(camada.n)} feição(ões) de ${camada.nome}`,
  };
}
