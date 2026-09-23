"""Configuração do Expansion 3D, lida de conf.ini (criado com os padrões na primeira execução)."""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONF_PATH = Path(os.environ.get("EXPANSION3D_CONF", PROJECT_DIR / "conf.ini"))

MODELO_CONF = """\
; Configuração do Expansion 3D. Reinicie o serviço depois de alterar.

[servidor]
; 0.0.0.0 aceita conexões da rede; 127.0.0.1 libera só a própria máquina.
host = 0.0.0.0
porta = 8010
titulo = Expansion 3D
; Endereço pelo qual os usuários abrem o Expansion, com http:// ou https:// e a porta.
; É o endereço que o Atlas recebe para devolver o ticket do login. Deixe igual ao
; EXPANSION_URL do .env do Atlas. Em branco, o endereço é deduzido da requisição, o que
; erra o esquema quando há proxy ou balanceador no caminho (vira https numa porta http).
url_publica = http://172.20.70.54:8010

[dados]
; Pasta raiz: acervo de bases enviadas, banco do acervo e cache das fontes abertas.
; Caminho relativo é resolvido a partir da pasta do projeto.
pasta = data
; Tamanho máximo aceito no envio de uma base do Interplan.
tamanho_max_mb = 300

[interplan]
; Senha das bases do Interplan e região marcada por padrão no envio.
senha = dai365mon
regiao_padrao = SP

[atlas]
; Login único com o Atlas (GCIWeb). Sem ninguém logado o Expansion abre em modo Visualização:
; mostra as redes públicas e não deixa enviar, importar nem excluir nada.
; ativo = nao desliga a integração e libera tudo sem login (uso em máquina de desenvolvimento).
ativo = sim
; Endereço do Atlas, usado para pedir o ticket do usuário logado e para o botão "Entrar".
url = https://172.20.70.54
; Mesma frase secreta configurada no .env do Atlas como EXPANSION_SECRET.
; Troque por outra antes de colocar em produção e mantenha as duas pontas iguais.
segredo = mcwNSkEN_IRIZZQslVVaq8pdZ-XN00BK821iGzDw_aJ1FterTY6Ccj9hpFwnpa4X
; Validade do ticket de entrada, em segundos (só do salto Atlas -> Expansion).
ticket_validade_s = 120
; Validade da sessão do Expansion, em horas.
sessao_validade_h = 12
; Tenta o login automático ao abrir a página (salto silencioso no Atlas e volta).
login_automatico = sim

[regioes]
; Regiões oferecidas no envio de uma rede. Para cada uma: EPSG (UTM SIRGAS 2000) e a área
; aproximada (lon_min, lat_min, lon_max, lat_max), usada para conferir se o CRS bate com as
; coordenadas da base e avisar quando a região escolhida parecer errada.
SP = 31983, -47.2, -24.8, -43.9, -22.2
ES = 31984, -42.0, -21.5, -39.6, -17.8

[mapa]
; satelite | osm | nenhum
provedor = satelite
; Limite de ladrilhos do mapa base por rede.
max_ladrilhos = 400

[vegetacao]
; Faixa ao redor dos vãos onde as árvores são desenhadas e onde a folga é medida.
corredor_m = 40
faixa_m = 2.5
altura_min_arvore_m = 3
; Folga vertical (m) entre cabo e copa em cada classe. Ajuste ao critério de poda.
limiar_contato = 0.0
limiar_critico = 1.5
limiar_atencao = 3.0

[predios]
; Usada quando o Overture traz número de andares mas não a altura.
altura_por_andar_m = 3.0

[cabos]
; Bitolas (mm²) desenhadas como Spacer Cable, em losango com mensageiro no topo.
spacer_mm2 = 50, 70, 185

[importacao]
; Tamanho máximo de um arquivo KML/KMZ importado como camada.
kml_max_mb = 40
; Limite de feições lidas de um KML/KMZ (protege a cena de arquivos enormes).
kml_max_feicoes = 50000
"""


@dataclass(frozen=True)
class Regiao:
    sigla: str
    epsg: int
    bbox: tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max

    def contem(self, lon: float, lat: float) -> bool:
        lon0, lat0, lon1, lat1 = self.bbox
        return lon0 <= lon <= lon1 and lat0 <= lat <= lat1


@dataclass(frozen=True)
class Settings:
    host: str
    porta: int
    titulo: str
    url_publica: str
    pasta_dados: Path
    tamanho_max_mb: int
    senha: str
    regioes: tuple[Regiao, ...]
    regiao_padrao: str
    provedor_mapa: str
    max_ladrilhos: int
    corredor_m: float
    faixa_m: float
    altura_min_arvore_m: float
    limiares: tuple[tuple[float, int, str], ...]
    altura_por_andar_m: float
    spacer_mm2: frozenset[float]
    atlas_ativo: bool
    atlas_url: str
    atlas_segredo: str
    ticket_validade_s: int
    sessao_validade_h: int
    login_automatico: bool
    kml_max_mb: int
    kml_max_feicoes: int

    @property
    def sso_pronto(self) -> bool:
        """Só há login se a integração está ligada e o segredo foi preenchido nas duas pontas."""
        return self.atlas_ativo and bool(self.atlas_segredo.strip())

    @property
    def epsg(self) -> int:
        """CRS da região padrão (usado quando o registro não informa outra)."""
        return self.regiao(self.regiao_padrao).epsg

    def regiao(self, sigla: str | None) -> Regiao:
        alvo = (sigla or self.regiao_padrao).upper()
        for r in self.regioes:
            if r.sigla == alvo:
                return r
        return self.regioes[0]

    @property
    def redes_dir(self) -> Path:
        return self.pasta_dados / "redes"

    @property
    def cache_dir(self) -> Path:
        return self.pasta_dados / "cache"

    @property
    def banco(self) -> Path:
        return self.pasta_dados / "acervo.db"


def carregar(path: Path = CONF_PATH) -> Settings:
    if not path.exists():
        path.write_text(MODELO_CONF, encoding="utf-8")
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    # o conf.ini é escrito em português: "sim/não" valem tanto quanto "yes/no"
    cp.BOOLEAN_STATES = {**configparser.ConfigParser.BOOLEAN_STATES,
                         "sim": True, "s": True, "nao": False, "não": False, "n": False}
    cp.read_string(MODELO_CONF)  # padrões; o arquivo só precisa trazer o que muda
    cp.read(path, encoding="utf-8")

    pasta = Path(cp.get("dados", "pasta"))
    if not pasta.is_absolute():
        pasta = PROJECT_DIR / pasta
    bitolas = frozenset(
        float(b.strip().replace(",", ".")) for b in cp.get("cabos", "spacer_mm2").split(",") if b.strip()
    )
    regioes = []
    for sigla in cp.options("regioes"):
        partes = [p.strip() for p in cp.get("regioes", sigla).split(",")]
        regioes.append(Regiao(sigla.upper(), int(partes[0]), tuple(float(v) for v in partes[1:5])))
    return Settings(
        host=cp.get("servidor", "host"),
        porta=cp.getint("servidor", "porta"),
        titulo=cp.get("servidor", "titulo"),
        url_publica=cp.get("servidor", "url_publica", fallback="").strip().rstrip("/"),
        pasta_dados=pasta,
        tamanho_max_mb=cp.getint("dados", "tamanho_max_mb"),
        senha=cp.get("interplan", "senha"),
        regioes=tuple(regioes),
        regiao_padrao=cp.get("interplan", "regiao_padrao").upper(),
        provedor_mapa=cp.get("mapa", "provedor"),
        max_ladrilhos=cp.getint("mapa", "max_ladrilhos"),
        corredor_m=cp.getfloat("vegetacao", "corredor_m"),
        faixa_m=cp.getfloat("vegetacao", "faixa_m"),
        altura_min_arvore_m=cp.getfloat("vegetacao", "altura_min_arvore_m"),
        limiares=(
            (cp.getfloat("vegetacao", "limiar_contato"), 3, "contato"),
            (cp.getfloat("vegetacao", "limiar_critico"), 2, "critico"),
            (cp.getfloat("vegetacao", "limiar_atencao"), 1, "atencao"),
        ),
        altura_por_andar_m=cp.getfloat("predios", "altura_por_andar_m"),
        spacer_mm2=bitolas,
        atlas_ativo=cp.getboolean("atlas", "ativo"),
        atlas_url=cp.get("atlas", "url").rstrip("/"),
        atlas_segredo=cp.get("atlas", "segredo"),
        ticket_validade_s=cp.getint("atlas", "ticket_validade_s"),
        sessao_validade_h=cp.getint("atlas", "sessao_validade_h"),
        login_automatico=cp.getboolean("atlas", "login_automatico"),
        kml_max_mb=cp.getint("importacao", "kml_max_mb"),
        kml_max_feicoes=cp.getint("importacao", "kml_max_feicoes"),
    )


settings = carregar()
settings.redes_dir.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)
