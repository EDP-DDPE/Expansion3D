// Teste da carga de prédios por proximidade (node tools/teste_celulas.mjs).
// Roda sem navegador: usa a Three.js de verdade e o mesmo código de web/context.js.
import * as THREE from 'three';
import { setTerrain } from '../web/network.js';
import { buildBuildings } from '../web/context.js';

setTerrain(null); // terreno plano

const itens = [];
for (let cx = 0; cx < 20; cx++) {
  for (let cy = 0; cy < 20; cy++) {
    for (let k = 0; k < 4; k++) {
      const x = cx * 800 + 100 + k * 30, y = cy * 800 + 100;
      itens.push({ c: [x, y, x + 20, y, x + 20, y + 20, x, y + 20], h: 6, e: 1, k: null, a: 400 });
    }
  }
}
const { gerenciador } = buildBuildings({ itens, fonte: 'teste' });
console.log('prédios:', itens.length, '| células:', gerenciador.total);

const camera = new THREE.PerspectiveCamera(50, 1.6, 0.5, 20000);
function posicionar(x, altura, y, olharX, olharY) {
  camera.position.set(x, altura, -y);
  camera.lookAt(new THREE.Vector3(olharX, 0, -olharY));
  camera.updateMatrixWorld(true);
  camera.updateProjectionMatrix();
}
function ciclo(raio, quadros = 80) {
  gerenciador.atualizar(camera, raio);
  for (let i = 0; i < quadros; i++) gerenciador.montarLote();
  gerenciador.atualizar(camera, raio);
  return gerenciador.montadas;
}

posicionar(8000, 150, 8000, 8000, 12000);
console.log('alcance 1800 m, olhando para o norte:', ciclo(1800));
posicionar(8000, 150, 8000, 8000, 4000);
console.log('mesma posição, olhando para o sul: ', ciclo(1800));
posicionar(8000, 150, 8000, 8000, 12000);
console.log('alcance 3500 m:                    ', ciclo(3500));
posicionar(60000, 150, 60000, 60000, 64000);
console.log('câmera a 50 km de distância:       ', ciclo(1800));
posicionar(8000, 9000, 8000, 8000, 12000);
console.log('raio 0 (câmera alta demais):       ', ciclo(0));
