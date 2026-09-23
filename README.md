# Expansion 3D · Interplan

Visualizador 3D de redes de distribuição a partir das bases Access (`rede.mdb`) do Interplan.
Você aponta para uma rede e o sistema monta a cena com subestação, postes, cruzetas, condutores
com catenária, transformadores, cabines de cliente MT, chaves, reguladores, capacitores, GD e
interligações, sobre **relevo real**, com **prédios**, **ruas**, **árvores** e a **folga entre cabos e
vegetação**. Qualquer ponto selecionado abre no **Google Street View**. Só usa ferramentas e dados gratuitos.

## Stack

| Camada | Tecnologia | Por quê |
|---|---|---|
| Leitura da base | **Python** + `pyodbc` (driver Access 64-bit) | acesso nativo ao `.mdb` com senha, sem exportações |
| Geografia | `pyproj`, `rasterio`, `scipy`, `shapely` | reprojeção, recorte de rasters e análise espacial |
| Prédios | `duckdb` (httpfs + spatial) | consulta direta aos arquivos Parquet do Overture, só da área da rede |
| GIS corporativo | `oracledb` (modo thin) | dispensa o Oracle Client (o instalado é 32-bit) |
| API | FastAPI + Uvicorn (serviço NSSM) | acervo de redes e modelo 3D em JSON |
| Acervo | SQLite | registro de quem enviou, descrição, subestações e circuitos de cada base |
| 3D | **JavaScript + Three.js** (WebGL) | roda no navegador, instancing para dezenas de milhares de objetos |

## Como rodar

```bash
pip install -r requirements.txt
npm install
run.bat
```

Abre na porta definida em `conf.ini` (padrão 8010). O usuário **envia o `rede.mdb`** pelo painel
"Acervo de redes", informando o nome e uma descrição (projeto, estudo, ano). O servidor guarda o
arquivo, extrai subestações e circuitos e publica a rede na lista, que é **compartilhada por todos**.
Qualquer usuário pode abrir ou excluir uma rede do acervo.

## Implantação como serviço (Windows + NSSM)

Roda em porta própria, ao lado de outros sistemas (por exemplo, um Atlas em waitress).

```bash
servico\instalar_servico.bat
```

O script registra o serviço `Expansion3D` apontando para `run_server.py` (uvicorn lendo host e porta
do `conf.ini`), com log em `data/servico.log` e início automático. Para liberar no firewall:

```bash
netsh advfirewall firewall add rule name="Expansion 3D" dir=in action=allow protocol=TCP localport=8010
```

Requisitos no servidor: Python 64-bit, **Microsoft Access Database Engine 2016 x64** (se houver Office
32-bit instalado, use `AccessDatabaseEngine_X64.exe /passive`) e saída para a internet nas primeiras
cargas de cada região.

## Regiões e sistema de coordenadas

Cada estado fica numa zona UTM diferente: **SP na 23S (EPSG 31983)** e **ES na 24S (EPSG 31984)**. Ler
uma base do ES como se fosse de SP desloca a rede em ~630 km sem dar erro nenhum, então:

- o envio tem um **seletor de região** e o acervo tem um **filtro por região**;
- o EPSG fica gravado por rede, não global;
- o servidor **confere pelas coordenadas**: se o centro da rede não cair na área da região escolhida,
  ele procura entre as regiões configuradas, registra na correta e devolve um aviso na tela.

As regiões (EPSG e área aproximada) ficam na seção `[regioes]` do `conf.ini`; para atender outro estado,
basta acrescentar uma linha.

## Configuração (conf.ini)

Todas as opções ficam em `conf.ini`, criado com os padrões na primeira execução: porta e host do
serviço, pasta de dados e limite de upload, senha do Interplan e região padrão, regiões com seus EPSG,
provedor do mapa, limiares de folga da vegetação, altura por andar dos prédios e bitolas tratadas como
Spacer Cable.

Os arquivos da aplicação (`/static`) são servidos com `Cache-Control: no-cache`: sem isso um navegador
pode ficar com o JavaScript antigo depois de uma atualização e apresentar falhas difíceis de explicar.

### Cache regional compartilhado

O que vem da internet é guardado em **ladrilhos fixos de coordenada UTM**, não por rede: dossel a cada
2 km, prédios e ruas a cada 10 km, relevo por grau. Quando alguém abre uma rede, o sistema verifica
quais ladrilhos já existem e baixa só os que faltam, numa única consulta. Duas redes vizinhas, ou o
mesmo alimentador em anos diferentes, reaproveitam tudo o que já foi baixado por qualquer usuário.
A rede aparece antes; vegetação, prédios e ruas entram na cena quando ficam prontos.

A **análise de vegetação** (detecção de árvores + folga vão a vão) é cara em redes grandes: 2m13s na base
do ES (84 ladrilhos de dossel, 15 mil vãos). O resultado fica em `data/cache/contexto/vegetacao/` por rede,
e passa a ser lido em ~0,7 s, inclusive depois de reiniciar o serviço. A assinatura do cache inclui os
parâmetros de vegetação do `conf.ini`: mudar um limiar refaz o cálculo.

Duas opções ajudam na leitura da cena:

- **Imagem: alta resolução** divide cada ladrilho em quatro do zoom seguinte (imagem mais nítida, 4× mais textura).
- **Abrir no Google Street View** no painel de detalhes abre o ponto selecionado numa aba nova. É só um
  link (`map_action=pano`), sem chave de API nem custo; a cobertura depende do Google.

## Interface

- **Painel lateral ajustável**: arraste a borda direita do painel; a largura fica salva no navegador.
- **Grupos recolhíveis**: clique no título de qualquer grupo (Acervo, Circuitos, Camadas…) para recolher;
  o estado de cada grupo também fica salvo.
- **Busca no acervo**: filtra por subestação, circuito, arquivo, usuário ou descrição.
- **Tela de carregamento** ao abrir uma rede, e **chips de progresso na barra superior** com o % de cada
  conjunto (rede, vegetação, prédios, ruas), incluindo a fase de montagem na cena e quantos ladrilhos
  foram baixados naquele momento.
- **Carregamento por proximidade**: prédios são divididos em células de 800 m e só as que estão dentro
  do alcance **e à frente da câmera** são montadas; o que sai do alcance é descartado da memória. As
  árvores acendem e apagam pelo mesmo critério. Com a câmera muito alta, ambos somem (não são legíveis
  nessa escala e custam desempenho). O alcance é ajustável em Camadas e o painel mostra quantos blocos
  estão carregados. Teste da lógica: `node tools/teste_celulas.mjs`.
- **Camadas** com quadrado da cor usada no 3D e a quantidade de itens. As chaves aparecem separadas em
  **Religadoras (RL)**, **Chaves faca (CF)** e **Outras chaves**. Clicar no nome da camada leva a câmera
  a um item sorteado dela e abre os detalhes.
- **Barra de etapas** sobre a cena quando o projeto tem mais de uma, com o botão de comparação lado a lado.
- **Diagnóstico**: no console do navegador (F12), `expansion3d` dá acesso ao estado da cena
  (`expansion3d.etapas`, `.layerOn`, `.usuario`), útil quando alguém relata um problema.

## Login único com o Atlas

O Expansion roda no mesmo servidor do Atlas (GCIWeb), em outra porta. Como o cookie de sessão do
Atlas não é compartilhável entre as duas aplicações, a entrada usa um **ticket assinado**:

```
Expansion  ->  GET {atlas}/api/expansion/ticket?retorno=...
Atlas      ->  volta com ?ticket=<assinado>   (ou ticket=nenhum, se não houver sessão)
Expansion  ->  confere a assinatura e grava o próprio cookie
```

O ticket leva apenas matrícula, nome, e-mail e o indicador de administrador; vale 2 minutos e é
assinado com HMAC-SHA256 pela frase secreta compartilhada. Nenhuma senha ou token do Azure sai do
Atlas. O `retorno` é conferido contra `EXPANSION_URL` antes de qualquer redirecionamento, para que a
rota não possa mandar o ticket a um site de fora.

Ao abrir a página, a tentativa de login acontece num **quadro escondido**: se o Atlas estiver fora do
ar, o usuário fica em modo Visualização em vez de cair numa página de erro. O botão "Entrar com o
Atlas" continua disponível.

**Modo Visualização** (ninguém logado): vê e exporta as redes públicas, mas não envia, importa nem
exclui nada.

### O que configurar nas duas pontas

| Onde | Chave | Valor |
|---|---|---|
| Expansion · `conf.ini` `[servidor]` | `url_publica` | endereço do **Expansion**, com esquema e porta |
| Expansion · `conf.ini` `[atlas]` | `url` | endereço do **Atlas** |
| Expansion · `conf.ini` `[atlas]` | `segredo` | a mesma frase nas duas pontas |
| Atlas · `.env` | `EXPANSION_URL` | endereço do Expansion (único retorno aceito) |
| Atlas · `.env` | `EXPANSION_SECRET` | a mesma frase |

`url_publica` e `EXPANSION_URL` precisam ser **idênticos**, esquema e porta inclusive. O Atlas
compara os dois antes de devolver o ticket e recusa com uma mensagem que mostra o que recebeu e o
que esperava.

> **Por que `url_publica` existe.** Deduzir o endereço da requisição erra sempre que houver proxy
> no caminho: basta um `X-Forwarded-Proto: https` para o endereço virar `https://host:8010`, e a
> 8010 responde em HTTP puro. O mesmo esquema errado ainda faria o cookie de sessão sair marcado
> como `Secure` numa conexão HTTP, e aí o navegador o descarta sem avisar — o login falharia em
> silêncio, mesmo com o redirecionamento certo.
> Teste: `python tools/teste_retorno_login.py` (com o serviço no ar).

### A volta do Atlas não usa `Location`

O nginx à frente do Atlas reescreve `http://` para `https://` no cabeçalho `Location`
(`proxy_redirect`, comum em quem termina TLS). Como o Expansion responde em HTTP puro na 8010, o
endereço reescrito virava `https://172.20.70.54:8010/...` e o navegador mostrava *"a conexão com
este site não é segura — resposta inválida"*, mesmo com o Atlas tendo recebido o endereço certo.

Por isso a rota do Atlas **devolve uma página** que navega sozinha (meta refresh + `location.replace`),
em vez de um 302: o nginx reescreve cabeçalhos, não o corpo da resposta. Assim a integração não
depende de mexer na configuração do nginx.

Teste (precisa do Flask, que está no venv do Atlas):

```bash
& "...\GCIWeb\.venv\Scripts\python.exe" tools\teste_volta_atlas.py
```

No Atlas, a integração é o blueprint `app/expansion/`, registrado em `app/main/__init__.py`.
Para desligar tudo e liberar o acesso sem login (máquina de desenvolvimento): `ativo = nao`.

Testes: `python tools/teste_ticket_atlas.py` (o ticket do Atlas é aceito pelo Expansion, e só com o
segredo certo), `python tools/teste_retorno_login.py` (endereço de retorno e flags do cookie, com e
sem proxy) e `python tools/teste_api.py` (regras de acesso da API, ponta a ponta).

## Redes públicas e privadas

Cada rede, camada KML e projeto é **pública** (todos veem) ou **privada** (só a matrícula que enviou).
O filtro é aplicado no SQL, então uma entrada privada não chega ao navegador de outro usuário nem
pelo id direto — a resposta é 404, igual à de algo inexistente.

Excluir: item privado, só o dono; item público, qualquer usuário logado (como antes).

## Projetos em etapas

Um projeto reúne etapas — normalmente **Etapa 0 = rede atual** e as seguintes = obras projetadas.
Cada etapa aponta para uma rede do acervo.

- Todas as etapas ficam **carregadas na memória**, então a troca é imediata (medido: ~36 ms).
- O relevo, o mapa, as árvores, as casas e as ruas são **carregados uma vez** e reaproveitados por
  todas as etapas: ficam na camada 0 do Three.js, enquanto cada etapa ocupa a sua própria camada e a
  câmera escolhe qual enxergar.
- Como cada base do Interplan tem origem e cota de base próprias, as etapas seguintes são
  **recolocadas no referencial da Etapa 0** (deslocamento em E, N e altura).
- **Comparar lado a lado** divide a tela em dois; as duas câmeras andam juntas, para comparar o mesmo
  ponto nas duas etapas.

Para criar: crie o projeto, abra a rede que será a etapa e clique em **Nova etapa** no cartão do
projeto.

## Exportar KMZ do que está visível

O botão **Exportar** monta um KMZ com as camadas e os circuitos **marcados naquele momento** — o que
estiver desmarcado não entra no arquivo. As altitudes saem absolutas (relevo + altura do equipamento),
então o Google Earth mostra a rede na altura certa em vez de deitada no chão. Cada camada vira uma
pasta, e os condutores ainda se subdividem por circuito, com a cor do circuito.

## Importar KML/KMZ

Arquivos KML ou KMZ entram no acervo como camadas e são posicionados sobre a rede aberta (pontos,
linhas e áreas, com a cor do `Style`/`StyleMap` do arquivo e os campos de `ExtendedData` nos detalhes).
Feições sem altitude acompanham o relevo. Limites em `conf.ini` `[importacao]`.
Teste: `python tools/teste_kml.py`.

## Fontes de dados abertas

| Camada | Fonte | Resolução | Licença |
|---|---|---|---|
| Relevo | Copernicus DEM GLO-30 (AWS Open Data) | 30 m | uso livre com atribuição |
| Altura das árvores | Meta & WRI High Resolution Canopy Height | ~1 m | CC BY 4.0 |
| Prédios | Overture Maps (Google Open Buildings, Microsoft, OSM) | contorno | ODbL / CDLA |
| Ruas | Overture Maps · transportation (OpenStreetMap) | eixo da via | ODbL |
| Imagem | Esri World Imagery ou OpenStreetMap | ladrilhos z16 | termos dos provedores |

## Análise de vegetação × rede

Para cada vão, o cabo é amostrado a cada 1 m (altura de fixação do arranjo MT + relevo nas duas pontas − flecha)
e comparado com a maior altura de copa numa faixa de ±2,5 m sob o eixo do vão. A menor folga classifica o vão:

| Classe | Folga cabo–copa |
|---|---|
| Contato provável | < 0 m |
| Crítico | < 1,5 m |
| Atenção | < 3 m |

Os limiares ficam em `CLEARANCE_CLASSES` (`app/context3d.py`) e devem ser ajustados ao critério de poda da EDP.
É **triagem**: altura de copa estimada por satélite e relevo de 30 m têm erros de alguns metros.

## Premissas a confirmar

- Coordenadas em **SIRGAS 2000**, UTM 23S no SP (31983) e 24S no ES (31984), conforme a região da rede.
- `FASES2`: 1/2/3 = monofásico A/B/C, 13 = trifásico, demais (9, 12) tratados como bifásico.
- `ESTADO` da chave: 1 = fechada, 0 = aberta.
- O Copernicus é modelo de **superfície**; o terreno é aproximado descontando a altura média do dossel por célula.
- Nesta região o Overture não traz altura: ela é **estimada pela área construída** (marcado na tela).
- Postes com altura padrão (arranjo MT + 1 m) até a integração com o GIS.
- Largura das ruas é um padrão por classe do OSM (`ROAD_WIDTH_M`), não a largura real medida.
- Cabos de **50, 70 e 185 mm² são tratados como Spacer Cable** (`spacer_mm2` no `conf.ini`): desenhados
  em losango, com mensageiro no topo e espaçadores a cada 9 m.
- Só a rede de MT é desenhada. As bases do ES trazem as tabelas `BT_*` preenchidas (70 mil trechos de
  baixa na base de Vitória), mas o secundário está fora do escopo.
- Prédios e ruas são servidos apenas dentro do retângulo da rede (+250 m), não de todo o mapa, para
  não mandar centenas de milhares de feições ao navegador em regiões metropolitanas.

## Precisão do assentamento no relevo

Ruas, prédios e postes amostram a mesma grade de relevo que a superfície desenhada. Para não flutuarem
nem afundarem, o eixo das ruas é reamostrado a cada 5 m e a malha do terreno é subdividida a ~17 m
(era ~35 m). Medido sobre os 446 km de vias: erro médio 0,07 m, p95 0,31 m, máximo 2,9 m — o pico
ocorre onde o relevo de 30 m não representa corte ou aterro da via.

## GIS corporativo (Oracle)

`tools/inspect_gis_postes.py` lê a estrutura da tabela `USU_GSA.POSTE` e grava uma amostra em
`data/cache/gis/`. As credenciais **nunca** ficam no código; defina-as no terminal antes de rodar:

```bash
$env:GIS_USER = "seu_usuario"; $env:GIS_PASSWORD = "sua_senha"; python tools\inspect_gis_postes.py
```

## Mapeamento Interplan → 3D

| Tabela(s) | Elemento 3D |
|---|---|
| `BARRA` (X/Y em decímetros UTM) | nós; barras coincidentes ou ligadas por trechos ≤ 2,5 m viram **um poste** |
| `TRECHO` + `CABOS` + `ARRANJO` | vãos entre postes, fios por fase na cruzeta (alturas e afastamentos do arranjo MT) |
| `CABOS` (TIPO 1) | cabo de fase de MT; `CABO_ID` se repete no TIPO 2 (BT multiplexado), por isso o filtro por tipo |
| `CARGA` código `ET` | transformador de distribuição no poste (tamanho proporcional ao kVA) |
| `CARGA` código `EP` + `MT_CONS` | cabine de cliente MT ao lado do poste, com os clientes |
| `CHAVE` + `TIPOCHAVE` | chave no poste (verde fechada / vermelha aberta) |
| `REGULADOR`, `CAPACITOR` | equipamentos no poste |
| `MT_GERADOR_CONS`, `GERADOR` | painéis de geração distribuída |
| `SE`, `SE_TRAFO`, `SE_CHAVE`, `CIRCUITO` | pátio da subestação, trafo de força, disjuntores e saídas dos alimentadores (uma base pode ter várias SEs) |
| `SOCORRO_EXT` | interligações com alimentadores vizinhos |
| `BLOCO` | circuito de cada barra/trecho (cor e filtro) |
