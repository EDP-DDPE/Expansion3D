import * as THREE from 'three';
import { toWorld } from './network.js';

export const BASEMAPS = {
  satelite: {
    label: 'Satélite (Esri)',
    url: (z, x, y) => `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`,
    attribution: 'Imagens © Esri, Maxar, Earthstar Geographics',
  },
  osm: {
    label: 'Mapa (OpenStreetMap)',
    url: (z, x, y) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`,
    attribution: 'Mapa © colaboradores do OpenStreetMap',
  },
  nenhum: { label: 'Sem imagem (só relevo)' },
};

// Subdivisões por ladrilho: ~17 m, bem abaixo dos 30 m do relevo, para o chão desenhado
// coincidir com a altura amostrada por postes, ruas e prédios.
const SEGMENTS_BY_DETAIL = { 1: 32, 2: 16 };

const indexCache = new Map();
function tileIndexFor(segments) {
  if (!indexCache.has(segments)) {
    const index = [];
    for (let j = 0; j < segments; j++) {
      for (let i = 0; i < segments; i++) {
        const nw = j * (segments + 1) + i, ne = nw + 1, sw = nw + segments + 1, se = sw + 1;
        index.push(nw, sw, ne, ne, sw, se);
      }
    }
    indexCache.set(segments, index);
  }
  return indexCache.get(segments);
}

// Canto do ladrilho em (u, v) por interpolação bilinear dos 4 cantos (NO, NE, SE, SO).
const cornerAt = (c, u, v) => [
  (c[0] + (c[2] - c[0]) * u) * (1 - v) + (c[6] + (c[4] - c[6]) * u) * v,
  (c[1] + (c[3] - c[1]) * u) * (1 - v) + (c[7] + (c[5] - c[7]) * u) * v,
];

// Divide cada ladrilho em 4 filhos do zoom seguinte, dobrando a resolução da imagem.
function subdivide(tiles) {
  return tiles.flatMap((t) => [0, 1].flatMap((j) => [0, 1].map((i) => {
    const [u0, u1, v0, v1] = [i / 2, (i + 1) / 2, j / 2, (j + 1) / 2];
    return {
      x: t.x * 2 + i, y: t.y * 2 + j,
      c: [...cornerAt(t.c, u0, v0), ...cornerAt(t.c, u1, v0), ...cornerAt(t.c, u1, v1), ...cornerAt(t.c, u0, v1)],
    };
  })));
}

// Cada ladrilho vem do servidor com os 4 cantos (NO, NE, SE, SO) no plano local e é drapeado sobre o relevo.
export function buildTerrainSurface(mapa, key, renderer, detail = 1) {
  const group = new THREE.Group();
  if (!mapa) return group;
  let { z, tiles } = mapa;
  for (let k = 1; k < detail; k++) { tiles = subdivide(tiles); z += 1; }
  const segments = SEGMENTS_BY_DETAIL[detail] ?? 16;
  const tileIndex = tileIndexFor(segments);
  const provider = BASEMAPS[key];
  const loader = new THREE.TextureLoader();
  loader.setCrossOrigin('anonymous');
  const anisotropy = renderer.capabilities.getMaxAnisotropy();

  for (const t of tiles) {
    const [nwx, nwy, nex, ney, sex, sey, swx, swy] = t.c;
    const positions = [], uvs = [];
    for (let j = 0; j <= segments; j++) {
      const v = j / segments;
      for (let i = 0; i <= segments; i++) {
        const u = i / segments;
        const x = (nwx + (nex - nwx) * u) * (1 - v) + (swx + (sex - swx) * u) * v;
        const y = (nwy + (ney - nwy) * u) * (1 - v) + (swy + (sey - swy) * u) * v;
        positions.push(...toWorld(x, y, 0).toArray());
        uvs.push(u, 1 - v);
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geo.setAttribute('uv', new THREE.Float32BufferAttribute(uvs, 2));
    geo.setIndex(tileIndex);
    geo.computeVertexNormals();

    const mat = new THREE.MeshStandardMaterial({ color: provider?.url ? 0x3a4048 : 0x7d8a62, roughness: 1, metalness: 0 });
    if (provider?.url) {
      loader.load(provider.url(z, t.x, t.y), (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = anisotropy;
        mat.map = tex;
        mat.color.set(0xffffff);
        mat.needsUpdate = true;
      });
    }
    group.add(new THREE.Mesh(geo, mat));
  }
  return group;
}
