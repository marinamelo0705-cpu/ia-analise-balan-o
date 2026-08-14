import io
import json
import re

import streamlit as st
import plotly.graph_objects as go
from groq import Groq
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes

# =========================================================
# CONFIGURAÇÃO DA PÁGINA
# =========================================================
st.set_page_config(page_title="Analisador Contábil Completo", page_icon="📊", layout="wide")

st.title("📊 Analisador Contábil de Balanços e DRE")
st.caption(
    "Envie o PDF do Balanço Patrimonial (e, se tiver, a DRE em arquivo separado) para "
    "extrair os valores, conferir a saúde financeira da empresa e identificar prejuízos."
)

# Paleta (dataviz skill) — cores fixas por papel, nunca por ordem/ciclo.
COR_CIRCULANTE = "#2a78d6"       # azul — slot categórico 1
COR_NAO_CIRCULANTE = "#1baf7a"   # aqua — slot categórico 3
COR_PL = "#4a3aa7"                # violeta — slot categórico 7 (entidade própria)
COR_BOM = "#0ca30c"               # status "good"
COR_CRITICO = "#d03b3b"           # status "critical"
COR_SUPERFICIE = "#fcfcfb"

# =========================================================
# API KEY
# =========================================================
if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

MODELO = "llama-3.3-70b-versatile"  # confira em console.groq.com/docs/models se ainda está disponível


# =========================================================
# EXTRAÇÃO DE TEXTO DO PDF
# =========================================================
LIMIAR_TEXTO_PAGINA = 40  # abaixo disso, a página é considerada "sem texto direto"


def ocr_pagina(img):
    """
    Roda o Tesseract em português. Se o pacote de idioma 'por' não estiver
    instalado no ambiente (comum no Streamlit Community Cloud quando o
    packages.txt não foi aplicado), o Tesseract lança um TesseractError
    específico ("Failed loading language 'por'") e NENHUM texto sai — isso
    reproduz exatamente um cenário de "N/D em tudo", porque a IA recebe um
    texto quase vazio. Detectamos esse erro específico, avisamos bem alto na
    tela (uma vez por sessão) com o que precisa ser corrigido, e usamos
    inglês como OCR de emergência só pra não deixar o app 100% cego
    enquanto isso não é corrigido no deploy.
    """
    try:
        return pytesseract.image_to_string(img, lang="por", config="--psm 4")
    except pytesseract.TesseractError as e:
        msg = str(e)
        if "por" in msg.lower() and ("tessdata" in msg.lower() or "language" in msg.lower()):
            if not st.session_state.get("aviso_por_ausente"):
                st.session_state["aviso_por_ausente"] = True
                st.error(
                    "🚨 **Causa provável do N/D encontrada**: o pacote de idioma Português do "
                    "Tesseract OCR não está instalado neste deploy (erro do OCR: "
                    f"`{msg.strip()}`). Sem ele, o OCR falha silenciosamente em toda página "
                    "escaneada — é exatamente isso que produz N/D em tudo.\n\n"
                    "**Como corrigir:** confirme que existe um arquivo `packages.txt` na RAIZ do "
                    "seu repositório do GitHub (mesmo nível do `app.py`) contendo a linha "
                    "`tesseract-ocr-por`, depois vá no painel do Streamlit Cloud → seu app → "
                    "menu ⋮ → **Reboot app**. Sem o reboot, o Cloud não reinstala os pacotes de "
                    "sistema mesmo após o commit.\n\n"
                    "Usando inglês como OCR de emergência agora só pra não travar o app, mas a "
                    "leitura vai sair com bem mais erros até isso ser corrigido."
                )
            return pytesseract.image_to_string(img, lang="eng", config="--psm 4")
        raise


def extrair_texto_pdf(bytes_data, rotulo=""):
    """
    Extrai texto página por página, preservando o layout de colunas (Ativo x
    Passivo lado a lado) e complementando com leitura estruturada de tabelas.

    IMPORTANTE: o OCR é decidido POR PÁGINA, não pelo documento inteiro. Em
    balanços reais é comum uma página ter texto digital normal (ex.: o lado
    do Passivo) e outra ser uma imagem/scan (ex.: o lado do Ativo, ou uma
    página assinada digitalizada). Se a decisão de usar OCR fosse tomada só
    pelo total de caracteres do documento, uma página com bastante texto
    "esconde" a página vazia — e essa página inteira simplesmente some do
    texto final, fazendo a IA devolver N/D para tudo que estava nela. Por
    isso cada página abaixo do limiar recebe OCR individualmente.
    """
    texto_paginas = []
    paginas_para_ocr = []
    tabelas_texto = ""

    with pdfplumber.open(io.BytesIO(bytes_data)) as pdf:
        for i, page in enumerate(pdf.pages):
            # layout=True preserva os espaços em branco de acordo com a posição
            # x/y original do PDF, mantendo as colunas alinhadas em vez de
            # misturar Ativo e Passivo numa única sequência de números.
            t = page.extract_text(layout=True) or ""
            if len(t.strip()) < LIMIAR_TEXTO_PAGINA:
                paginas_para_ocr.append(i)
                texto_paginas.append("")  # preenchido abaixo via OCR
            else:
                texto_paginas.append(t)

            try:
                tabelas = page.extract_tables()
                for tabela in tabelas:
                    for linha in tabela:
                        celulas = [c.strip() for c in linha if c and c.strip()]
                        if celulas:
                            tabelas_texto += " | ".join(celulas) + "\n"
            except Exception:
                pass

    if paginas_para_ocr:
        numeros = ", ".join(str(p + 1) for p in paginas_para_ocr)
        st.info(f"ℹ️ {rotulo}: página(s) {numeros} com pouco texto digital — aplicando OCR nelas individualmente...")
        for i in paginas_para_ocr:
            try:
                # DPI 400 (em vez de 300): em balanços digitalizados os totais ao lado de
                # cabeçalhos como "ATIVO"/"CIRCULANTE" costumam estar em fonte pequena — a
                # 300 DPI eles frequentemente saem ilegíveis/ausentes do OCR; a 400 DPI ficam
                # nítidos. Testado diretamente com um balanço real digitalizado.
                imgs = convert_from_bytes(bytes_data, dpi=400, first_page=i + 1, last_page=i + 1)
                texto_ocr = ""
                for img in imgs:
                    texto_ocr += ocr_pagina(img.convert("L")) + "\n"
                texto_paginas[i] = texto_ocr
            except Exception as e:
                st.warning(f"Não foi possível aplicar OCR na página {i + 1} de {rotulo}: {e}")

    texto_pdf = "\n".join(f"--- página {i + 1} ---\n{t}" for i, t in enumerate(texto_paginas))
    if tabelas_texto:
        texto_pdf += "\n\n--- TABELAS DETECTADAS (linha = conta, colunas = valores) ---\n" + tabelas_texto

    return limpar_numeros_ocr(texto_pdf)


def limpar_numeros_ocr(texto):
    """
    O OCR de balanços escaneados costuma inserir espaços dentro dos próprios
    números (ex.: "13. 474. 832,27" em vez de "13.474.832,27"). Isso já
    confundiu a IA numa extração real: ela leu só "13.832,27", descartando o
    grupo "474" do meio. Aqui colamos de volta apenas os espaços que estão
    GRUDADOS a um ponto ou vírgula já existente (não inventamos separadores
    que não estão lá, só removemos o espaço espúrio ao redor de um que já
    existe) — isso é seguro e não junta números que são legitimamente
    diferentes.
    """
    texto = re.sub(r"(?<=\d)\s+\.\s*(?=\d)", ".", texto)
    texto = re.sub(r"(?<=\d)\s*\.\s+(?=\d)", ".", texto)
    texto = re.sub(r"(?<=\d)\s+,\s*(?=\d)", ",", texto)
    texto = re.sub(r"(?<=\d)\s*,\s+(?=\d)", ",", texto)
    return texto


def processar_pdf(bytes_data, rotulo):
    texto = extrair_texto_pdf(bytes_data, rotulo)
    if len(texto.strip()) < 30:
        st.error(f"⚠️ Não foi possível reconhecer o texto do documento «{rotulo}». Verifique se a imagem está legível.")
        return None
    return texto


# =========================================================
# HELPERS DE NÚMERO / JSON
# =========================================================
def parse_valor_brl(valor_str):
    """
    Converte 'R$ 21.966.947,43', '(1.234,56)' (negativo entre parênteses) ou
    '-1.234,56' em float. Retorna None se não houver valor.

    Usa sempre o ÚLTIMO separador ("." ou ",") como decimal, e remove todos
    os anteriores — em vez de assumir "." = milhar e "," = decimal de forma
    rígida. Isso importa porque o OCR de balanços reais às vezes mistura os
    dois caracteres dentro do MESMO número (ex.: "1.438.819,63" sai como
    "1.438,819,63" — dois separadores diferentes antes do decimal). Como o
    valor decimal em contabilidade brasileira sempre tem exatamente 2 dígitos,
    o último separador é sempre o decimal, não importa qual caractere seja.
    """
    if valor_str is None:
        return None
    s = str(valor_str).strip()
    if not s or s.lower() in ("null", "none", "n/d", "nd"):
        return None

    negativo = "(" in s and ")" in s
    if s.lstrip().startswith("-"):
        negativo = True

    s = re.sub(r"[^0-9,.]", "", s)
    if not s:
        return None

    ultimo_sep = max(s.rfind("."), s.rfind(","))
    if ultimo_sep == -1:
        parte_inteira, parte_decimal = s, ""
    else:
        parte_inteira = re.sub(r"[.,]", "", s[:ultimo_sep])
        parte_decimal = s[ultimo_sep + 1:]

    if not parte_inteira and not parte_decimal:
        return None
    try:
        valor = float(f"{parte_inteira or '0'}.{parte_decimal or '0'}")
    except ValueError:
        return None
    return -abs(valor) if negativo else valor


def formatar_brl(valor):
    if valor is None:
        return "N/D"
    sinal = "-" if valor < 0 else ""
    return f"{sinal}R$ {abs(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def escapar_dolar(texto):
    """
    Escapa 'R$' antes de mandar pro st.markdown. Versões recentes do Streamlit
    interpretam um único '$' como abertura de fórmula LaTeX/MathJax; quando o
    texto tem duas ou mais ocorrências de 'R$', tudo entre elas pode virar
    matemática e corromper o texto (foi o que gerou aquele "R`" estranho em
    vez de "R$" no seu resultado). Escapando, o cifrão volta a ser só texto.
    """
    if not texto:
        return texto
    return texto.replace("R$", "R\\$")


def extrair_json_da_resposta(texto):
    """Extrai o primeiro bloco JSON válido, mesmo cercado de texto ou ```json```."""
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        raise ValueError("Nenhum JSON encontrado na resposta do modelo.")
    return json.loads(match.group(0))


def chamada_groq_segura(client, prompt, temperature=0.1):
    """Encapsula a chamada à API com tratamento de erro amigável."""
    try:
        resp = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=MODELO,
            temperature=temperature,
        )
        return resp.choices[0].message.content, None
    except Exception as e:
        return None, str(e)


# =========================================================
# ETAPA 1: EXTRAÇÃO ESTRUTURADA (JSON) — SEM TEXTO LIVRE
# =========================================================
CAMPOS_BALANCO = [
    "ativo_circulante",
    "ativo_nao_circulante",
    "imobilizado",
    "ativo_total",
    "passivo_circulante",
    "passivo_nao_circulante",
    "patrimonio_liquido",
    "resultado_exercicio",
    "prejuizos_acumulados",
]

CAMPOS_DRE = [
    "receita_liquida",
    "custo_produtos_servicos",
    "lucro_bruto",
    "despesas_operacionais",
    "resultado_financeiro",
    "resultado_antes_ir",
    "ir_csll",
    "resultado_liquido_dre",
]

CAMPOS_ESPERADOS = CAMPOS_BALANCO + CAMPOS_DRE + ["resultado_tipo", "resultado_dre_tipo"]

PROMPT_EXTRACAO = """
Você é um auditor contábil sênior extraindo dados de um Balanço Patrimonial e,
se houver, de uma DRE (Demonstração do Resultado do Exercício). Analise APENAS
os dados explícitos contidos no texto abaixo. NÃO calcule, NÃO invente e NÃO
estime nenhum valor — copie exatamente o número escrito ao lado de cada conta.

ATENÇÃO 1: balanços patrimoniais brasileiros aparecem em formatos diferentes.
Pode ser (a) duas colunas lado a lado — ATIVO à esquerda, PASSIVO + PL à
direita — ou (b) um "balancete" sequencial, onde "ATIVO" e "PASSIVO" aparecem
como cabeçalhos de seção, cada um seguido por "CIRCULANTE" e depois "NÃO
CIRCULANTE" como subcabeçalhos (cada um com seu próprio total ao lado, ANTES
da lista de contas individuais daquela seção), e só depois vem a lista
detalhada de contas (ex.: Caixa, Bancos, Clientes, Estoques...). Nesse
segundo formato, o valor de "Ativo Circulante" é o número ao lado da palavra
"CIRCULANTE" que aparece IMEDIATAMENTE DEPOIS do cabeçalho "ATIVO" (e antes
de "PASSIVO" aparecer) — não confunda com o "CIRCULANTE" que aparece depois
do cabeçalho "PASSIVO", que é o Passivo Circulante. Preste muita atenção pra
não confundir os dois lados, seja qual for o formato.

ATENÇÃO 2: valores entre parênteses, como (1.234,56), ou precedidos de sinal
de menos representam números NEGATIVOS (ex: prejuízo, despesas, deduções).
Preserve o sinal exatamente como está no texto.

ATENÇÃO 3: se houver mais de uma coluna de valores (ex: "Ano Atual" e
"Ano Anterior"), utilize sempre a coluna do exercício MAIS RECENTE.

ATENÇÃO 4: o texto vem de OCR e pode ter pequenos erros de espaçamento.
Leia o número completo mesmo que ele pareça ter mais de 3 grupos de milhar
(ex.: "13.474.832,27" tem TRÊS pontos — não descarte nenhum grupo do meio,
copie o número inteiro do primeiro ao último dígito antes da vírgula
decimal).

--- TEXTO EXTRAÍDO DO(S) PDF(S) ---
{texto_pdf}
-----------------------------

Responda SOMENTE com um JSON válido (sem markdown, sem texto antes ou depois),
no formato abaixo. Se um valor não existir explicitamente no texto, use null.

{{
  "ativo_circulante": "valor exatamente como está escrito, ex: 21.966.947,43",
  "ativo_nao_circulante": "...",
  "imobilizado": "...",
  "ativo_total": "...",
  "passivo_circulante": "...",
  "passivo_nao_circulante": "... (Exigível Não Circulante)",
  "patrimonio_liquido": "...",
  "resultado_exercicio": "... (lucro ou prejuízo do exercício, conforme consta no Balanço/PL)",
  "resultado_tipo": "lucro" ou "prejuizo" ou null,
  "prejuizos_acumulados": "...",

  "receita_liquida": "... (só se houver DRE no texto)",
  "custo_produtos_servicos": "...",
  "lucro_bruto": "...",
  "despesas_operacionais": "...",
  "resultado_financeiro": "...",
  "resultado_antes_ir": "...",
  "ir_csll": "...",
  "resultado_liquido_dre": "... (resultado líquido final da DRE)",
  "resultado_dre_tipo": "lucro" ou "prejuizo" ou null
}}
"""


def extrair_dados_estruturados(client, texto_pdf):
    conteudo, erro = chamada_groq_segura(client, PROMPT_EXTRACAO.format(texto_pdf=texto_pdf), temperature=0.1)
    if erro:
        raise RuntimeError(f"Falha ao chamar a API da Groq na extração dos dados: {erro}")
    try:
        dados = extrair_json_da_resposta(conteudo)
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"O modelo não retornou um JSON válido na extração dos dados ({e}). "
            f"Tente processar novamente — respostas de IA podem variar."
        )
    for campo in CAMPOS_ESPERADOS:
        dados.setdefault(campo, None)
    return dados


# =========================================================
# ETAPA 1B: EXTRAÇÃO DE SEGURANÇA VIA REGEX (rede de proteção contra N/D)
# =========================================================
# Se a IA devolver null para um campo, tentamos achar o valor "na marra" com
# regex direto no texto extraído, antes de desistir e mostrar N/D. Isso cobre
# tanto falhas de leitura do modelo quanto rótulos que ele não reconheceu.
#
# O padrão de valor tolera espaços que o OCR costuma inserir dentro do número
# (ex.: "13. 474. 832,27" em vez de "13.474.832,27") e também aceita "." OU ","
# como separador de milhar em qualquer posição — em balanços reais o OCR às
# vezes confunde os dois caracteres dentro do MESMO número (ex.: o real
# "1.438.819,63" saiu como "1.438,819,63"). Qual separador é o decimal fica
# a cargo de parse_valor_brl (usa sempre o ÚLTIMO separador antes de 2 dígitos
# finais, seja "." ou ",").
PADRAO_VALOR = re.compile(r"\(?-?\s?R?\$?\s?\d{1,3}(?:\s?[.,]\s?\d{3})*\s?[.,]\s?\d{2}\)?")


def _limpar_valor_ocr(v):
    return re.sub(r"\s+", "", v).strip()


# Passo A: rótulo composto na mesma linha do valor (balanços "resumo", 2 colunas).
ROTULOS_FALLBACK = {
    "ativo_circulante": r"ativo\s+circulante",
    "ativo_nao_circulante": r"ativo\s+n[ãa]o[\s-]*circulante",
    "imobilizado": r"imobilizado(?!s)",
    "ativo_total": r"(ativo\s+total|total\s+do\s+ativo)",
    "passivo_circulante": r"passivo\s+circulante",
    "passivo_nao_circulante": r"(passivo\s+n[ãa]o[\s-]*circulante|exig[íi]vel\s*(a\s*longo\s*prazo|n[ãa]o[\s-]*circulante)?)",
    "patrimonio_liquido": r"patrim[oô]nio\s+l[íi]quido",
    "prejuizos_acumulados": r"preju[íi]zos?\s+acumulados?",
    "resultado_exercicio": r"(resultado\s+do\s+exerc[íi]cio|lucro[s]?\s*/?\s*preju[íi]zo[s]?\s+(l[íi]quido\s+)?(do\s+)?exerc[íi]cio)",
    "receita_liquida": r"receita\s+l[íi]quida",
    "custo_produtos_servicos": r"custo\s+(dos?\s+)?(produtos?|mercadorias?|servi[çc]os?)",
    "lucro_bruto": r"lucro\s+bruto",
    "despesas_operacionais": r"despesas?\s+operacionais?",
    "resultado_financeiro": r"resultado\s+financeiro",
    "resultado_antes_ir": r"resultado\s+antes\s+(do\s+)?(ir|imposto)",
    "ir_csll": r"(ir\s*/?\s*csll|imposto\s+de\s+renda)",
    "resultado_liquido_dre": r"(resultado|lucro)\s+l[íi]quido\s+(do\s+)?exerc[íi]cio",
}


def extrair_fallback_regex(texto_pdf):
    """Varre o texto linha a linha e casa rótulo + valor monetário na mesma linha."""
    encontrados = {}
    linhas = texto_pdf.split("\n")
    for campo, padrao_rotulo in ROTULOS_FALLBACK.items():
        regex_rotulo = re.compile(padrao_rotulo, re.IGNORECASE)
        for linha in linhas:
            m = regex_rotulo.search(linha)
            if not m:
                continue
            valores = PADRAO_VALOR.findall(linha[m.end():])
            if valores:
                encontrados[campo] = _limpar_valor_ocr(valores[0])
                break
    return encontrados


def extrair_fallback_hierarquico(texto_pdf):
    """
    Passo B: alguns balanços (confirmado testando balanços reais) não
    escrevem "Ativo Circulante" como frase única — em vez disso imprimem
    "ATIVO" e "CIRCULANTE" como cabeçalhos de seção separados, cada um com
    seu próprio total (formato de "balancete" hierárquico). E o total nem
    sempre vem DEPOIS do rótulo na mesma linha — em documentos reais já vimos
    o total aparecer na linha ANTERIOR ao cabeçalho seguinte (o OCR reordena
    um pouco quando a página tem colunas/quebras estranhas). Por isso, em vez
    de procurar só "valor depois do rótulo na mesma linha", este parser
    trabalha sobre o texto inteiro "achatado" (sem depender de onde caem as
    quebras de linha do OCR) e pega o valor monetário MAIS PRÓXIMO de cada
    rótulo, seja antes ou depois dele.
    """
    # achata quebras de linha em espaço: o OCR real já mostrou linhas
    # quebradas de forma inconsistente, então não dá pra confiar nelas.
    texto_flat = re.sub(r"\s+", " ", texto_pdf)

    encontrados = {}

    # 1) localiza os cabeçalhos de seção ATIVO / PASSIVO (aceita "ATIVO:" com
    # dois-pontos), pra saber em que contexto cada "CIRCULANTE" está.
    marcadores = []
    for m in re.finditer(r"\bativo\s*:?(?!\s*(n[ãa]o|circulante))\b", texto_flat, re.IGNORECASE):
        marcadores.append((m.start(), "ativo"))
    for m in re.finditer(r"\bpassivo\s*:?(?!\s*circulante)\b", texto_flat, re.IGNORECASE):
        marcadores.append((m.start(), "passivo"))
    marcadores.sort()

    def contexto_em(pos):
        atual = None
        for p, ctx in marcadores:
            if p <= pos:
                atual = ctx
            else:
                break
        return atual

    def valor_mais_proximo(pos, janela=150):
        ini = max(0, pos - janela)
        trecho = texto_flat[ini: pos + janela]
        candidatos = []
        for vm in PADRAO_VALOR.finditer(trecho):
            pos_abs = ini + vm.start()
            candidatos.append((abs(pos_abs - pos), vm.group()))
        if not candidatos:
            return None
        candidatos.sort(key=lambda x: x[0])
        return candidatos[0][1]

    def registrar(campo, valor):
        if campo not in encontrados and valor:
            encontrados[campo] = _limpar_valor_ocr(valor)

    # 2) "ativo total" = valor mais próximo do próprio cabeçalho ATIVO
    marcadores_ativo = [p for p, ctx in marcadores if ctx == "ativo"]
    if marcadores_ativo:
        registrar("ativo_total", valor_mais_proximo(marcadores_ativo[0], janela=60))

    # 3) "não circulante" — checa o contexto (ativo/passivo) mais recente antes do rótulo
    for m in re.finditer(r"\bn[ãa]o[\s-]*circulante\b", texto_flat, re.IGNORECASE):
        ctx = contexto_em(m.start())
        if ctx:
            registrar(f"{ctx}_nao_circulante", valor_mais_proximo(m.start()))

    # 4) "circulante" sozinho (não "não circulante", já tratado acima)
    for m in re.finditer(r"\bcirculante\b", texto_flat, re.IGNORECASE):
        prefixo = texto_flat[max(0, m.start() - 10): m.start()].lower()
        if "não" in prefixo or "nao" in prefixo:
            continue
        ctx = contexto_em(m.start())
        if ctx:
            registrar(f"{ctx}_circulante", valor_mais_proximo(m.start()))

    # 5) imobilizado (não depende de contexto ativo/passivo — só existe no ativo)
    for m in re.finditer(r"\bimobilizado\b", texto_flat, re.IGNORECASE):
        registrar("imobilizado", valor_mais_proximo(m.start()))
        break

    return encontrados


def preencher_campos_faltantes(dados, texto_pdf):
    """
    Completa com fallback regex qualquer campo que a IA deixou null, e devolve
    a lista dos que foram recuperados assim. Tenta primeiro o Passo A (rótulo
    composto, ex. "Ativo Circulante" numa frase só) e depois o Passo B
    (cabeçalhos hierárquicos separados, ex. "ATIVO" e "CIRCULANTE" em linhas
    distintas) pra qualquer campo que o Passo A não achou.
    """
    fallback = extrair_fallback_regex(texto_pdf)
    fallback_hierarquico = extrair_fallback_hierarquico(texto_pdf)
    for campo, valor in fallback_hierarquico.items():
        fallback.setdefault(campo, valor)

    recuperados = []
    for campo, valor in fallback.items():
        if not dados.get(campo) and campo in CAMPOS_BALANCO + CAMPOS_DRE:
            dados[campo] = valor
            recuperados.append(campo)
    return recuperados


def corrigir_confusao_imobilizado(dados_num, texto_pdf):
    """
    Bug observado num caso real: a IA às vezes devolve exatamente o mesmo
    valor para 'ativo_nao_circulante' e para 'imobilizado' — ou seja, troca o
    TOTAL da seção pelo valor de apenas UM item dentro dela. Isso é logicamente
    impossível na estrutura do balanço: o Imobilizado é só um dos componentes
    do Ativo Não Circulante (junto de Créditos e Valores, Investimentos,
    Intangível etc.), então o total tem que ser MAIOR que esse componente
    isolado — a menos que ele seja de fato o único item da seção, o que é raro.

    Quando os dois valores vêm idênticos (dentro da tolerância), isso é sinal
    forte desse bug específico. Para corrigir, buscamos no texto original um
    valor para o Ativo Não Circulante por leitura direta (sem IA, via
    extrair_fallback_hierarquico) e só substituímos se esse candidato for
    maior que o Imobilizado (ou seja, se ele resolve o problema em vez de
    repetir o mesmo erro). Retorna o valor corrigido, ou None se não havia
    sinal do bug ou não foi possível corrigir com segurança.
    """
    anc = dados_num.get("ativo_nao_circulante")
    imo = dados_num.get("imobilizado")
    if anc is None or imo is None:
        return None
    if abs(anc - imo) > tolerancia(imo):
        return None  # não há sinal desse bug específico

    fallback_hierarquico = extrair_fallback_hierarquico(texto_pdf)
    valor_fallback = fallback_hierarquico.get("ativo_nao_circulante")
    if not valor_fallback:
        return None
    anc_alt = parse_valor_brl(valor_fallback)
    if anc_alt is None or anc_alt <= imo:
        return None  # candidato não resolve o problema

    dados_num["ativo_nao_circulante"] = anc_alt
    return anc_alt


# =========================================================
# ETAPA 1C: COMPLETAR POR EQUAÇÃO CONTÁBIL (quando falta só 1 peça)
# =========================================================
def completar_por_equacao_contabil(dados_num):
    """
    Ativo Total = Ativo Circulante + Ativo Não Circulante, e
    Ativo Total = Passivo Circulante + Exigível Não Circulante + Patrimônio
    Líquido, são identidades contábeis exatas — não são "cálculos" no
    sentido de estimativa, são fatos que sempre valem num balanço fechado.
    Se sobrar exatamente UMA peça faltando numa dessas equações (e as
    outras já bateram / vieram da IA ou do fallback), preenchemos essa peça
    por diferença em vez de deixar N/D à toa. Devolve a lista de campos
    preenchidos assim, pra mostrar transparência ao usuário.
    """
    calculados = []

    def preencher(campo, valor):
        if dados_num.get(campo) is None:
            dados_num[campo] = valor
            calculados.append(campo)

    ac, anc, at = dados_num.get("ativo_circulante"), dados_num.get("ativo_nao_circulante"), dados_num.get("ativo_total")
    faltando_ativo = [v is None for v in (ac, anc, at)].count(True)
    if faltando_ativo == 1:
        if ac is None:
            preencher("ativo_circulante", at - anc)
        elif anc is None:
            preencher("ativo_nao_circulante", at - ac)
        elif at is None:
            preencher("ativo_total", ac + anc)

    # Recarrega valores (podem ter sido preenchidos acima)
    at = dados_num.get("ativo_total")
    pc, pnc, pl = dados_num.get("passivo_circulante"), dados_num.get("passivo_nao_circulante"), dados_num.get("patrimonio_liquido")
    faltando_passivo = [v is None for v in (pc, pnc, pl, at)].count(True)
    if faltando_passivo == 1:
        if pc is None and at is not None:
            preencher("passivo_circulante", at - (pnc or 0) - (pl or 0))
        elif pnc is None and at is not None:
            preencher("passivo_nao_circulante", at - (pc or 0) - (pl or 0))
        elif pl is None and at is not None:
            preencher("patrimonio_liquido", at - (pc or 0) - (pnc or 0))
        elif at is None:
            preencher("ativo_total", (pc or 0) + (pnc or 0) + (pl or 0))

    return calculados


# =========================================================
# ETAPA 2: VALIDAÇÃO ARITMÉTICA (CONFERE SE OS NÚMEROS BATEM)
# =========================================================
def tolerancia(base):
    """Tolerância relativa (0,5% do valor de referência), com piso de R$ 5."""
    if base is None:
        return 5.0
    return max(5.0, abs(base) * 0.005)


def validar_balanco(n):
    avisos = []
    ac, anc, at = n.get("ativo_circulante"), n.get("ativo_nao_circulante"), n.get("ativo_total")
    pc, pnc, pl = n.get("passivo_circulante"), n.get("passivo_nao_circulante"), n.get("patrimonio_liquido")

    if ac is not None and anc is not None and at is not None:
        soma = ac + anc
        if abs(soma - at) > tolerancia(at):
            avisos.append(
                f"⚠️ Ativo Circulante + Ativo Não Circulante ({formatar_brl(soma)}) não bate com o "
                f"Ativo Total informado ({formatar_brl(at)}). Confira os valores com o PDF original."
            )

    if pc is not None and pnc is not None and pl is not None and at is not None:
        soma = pc + pnc + pl
        if abs(soma - at) > tolerancia(at):
            avisos.append(
                f"⚠️ Passivo Circulante + Exigível Não Circulante + Patrimônio Líquido ({formatar_brl(soma)}) "
                f"não bate com o Ativo Total ({formatar_brl(at)}). Pela equação contábil "
                f"(Ativo = Passivo + PL), esses valores deveriam ser iguais — pode indicar erro de extração."
            )

    imo = n.get("imobilizado")
    if imo is not None and anc is not None:
        if imo > anc + tolerancia(anc):
            avisos.append(
                f"⚠️ Imobilizado ({formatar_brl(imo)}) é maior que o Ativo Não Circulante "
                f"({formatar_brl(anc)}). Como o Imobilizado é só uma parte do Ativo Não Circulante, "
                f"isso não é possível — provavelmente a extração confundiu os dois valores."
            )

    return avisos


def sugerir_correcao_ativo(dados_num):
    """
    Quando Ativo Circulante + Ativo Não Circulante não bate com o Ativo Total,
    mas o lado do Passivo (Passivo Circulante + Exigível Não Circulante + PL)
    bate exatamente com o Ativo Total, isso confirma que o Ativo Total está
    correto — então um dos dois componentes do Ativo (Circulante ou Não
    Circulante) está errado (normalmente por erro de OCR truncando dígitos), e
    o OUTRO determina o valor certo por diferença: at - anc ou at - ac.

    Para decidir QUAL dos dois está errado, comparamos a ordem de grandeza de
    cada um com o Passivo Circulante (numa empresa em operação normal, o Ativo
    Circulante costuma ser da mesma ordem de grandeza do Passivo Circulante) —
    o que estiver desproporcionalmente menor que o valor corrigido é o suspeito.

    Retorna um dict {"campo": ..., "valor_sugerido": ..., "valor_extraido": ...}
    ou None se não houver base segura para sugerir nada.
    """
    ac = dados_num.get("ativo_circulante")
    anc = dados_num.get("ativo_nao_circulante")
    at = dados_num.get("ativo_total")
    pc = dados_num.get("passivo_circulante")
    pnc = dados_num.get("passivo_nao_circulante")
    pl = dados_num.get("patrimonio_liquido")

    if None in (ac, anc, at):
        return None
    if abs((ac + anc) - at) <= tolerancia(at):
        return None  # já bate, nada a sugerir

    # só confia no Ativo Total como âncora se o lado do Passivo bater com ele
    if None in (pc, pnc, pl):
        return None
    if abs((pc + pnc + pl) - at) > tolerancia(at):
        return None  # o próprio total está suspeito, não dá pra usar como âncora

    ac_alt = at - anc
    anc_alt = at - ac

    # heurística: o candidato "errado" é o que está desproporcionalmente menor
    # que a referência de mesma ordem de grandeza (Passivo Circulante / ac_alt)
    if pc and pc > 0 and ac_alt > 0 and ac < ac_alt * 0.05:
        return {"campo": "ativo_circulante", "valor_sugerido": ac_alt, "valor_extraido": ac}

    if anc_alt > 0 and ac > 0 and anc < ac_alt * 0.02:
        return {"campo": "ativo_nao_circulante", "valor_sugerido": anc_alt, "valor_extraido": anc}

    return None


def validar_dre(n):
    """
    Removida a checagem "Receita − Custo = Lucro Bruto": na prática muitas DREs
    têm VÁRIAS linhas de custo/dedução (ex.: Custo dos Produtos Vendidos +
    Custos de Mercadorias + Custos de Serviços, cada uma separada), e o app só
    extrai um único campo "custo_produtos_servicos" — então essa conta dava
    falso positivo sempre que havia mais de uma linha de custo (confirmado
    com um caso real). Diferente do balanço patrimonial (Ativo = Passivo + PL
    é uma identidade universal), a estrutura de custos da DRE varia demais
    entre empresas pra validar com uma fórmula fixa sem gerar ruído.
    """
    return []


# =========================================================
# ETAPA 3: INDICADORES FINANCEIROS
# =========================================================
def calcular_indicadores(n):
    ac, pc = n.get("ativo_circulante"), n.get("passivo_circulante")
    pnc, pl, at = n.get("passivo_nao_circulante"), n.get("patrimonio_liquido"), n.get("ativo_total")
    ind = {}

    if ac is not None and pc not in (None, 0):
        ind["liquidez_corrente"] = ac / pc

    divida_total = (pc or 0) + (pnc or 0) if (pc is not None or pnc is not None) else None
    if divida_total is not None and at not in (None, 0):
        ind["endividamento_geral"] = divida_total / at

    if divida_total is not None and pl not in (None, 0):
        ind["capital_terceiros_sobre_pl"] = divida_total / pl

    ind["pl_negativo"] = pl is not None and pl < 0
    return ind


# =========================================================
# ETAPA 4: GRÁFICOS (paleta e regras do dataviz skill)
# =========================================================
def grafico_composicao(titulo, segmentos):
    """
    Barra horizontal única (100% empilhada) mostrando a composição de um
    grupo (ex.: Ativo, ou Passivo + PL). `segmentos` é uma lista de
    (rótulo, valor, cor). Valores None/negativos indevidos são ignorados.
    """
    segmentos_validos = [(r, v, c) for r, v, c in segmentos if v is not None and v > 0]
    if not segmentos_validos:
        return None

    fig = go.Figure()
    for rotulo, valor, cor in segmentos_validos:
        fig.add_trace(
            go.Bar(
                y=[titulo],
                x=[valor],
                name=rotulo,
                orientation="h",
                marker=dict(color=cor, line=dict(color=COR_SUPERFICIE, width=2)),
                text=[formatar_brl(valor)],
                textposition="inside",
                insidetextanchor="middle",
                hovertemplate=f"<b>{rotulo}</b><br>%{{text}}<extra></extra>",
            )
        )

    fig.update_layout(
        barmode="stack",
        title=titulo,
        height=180,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=-0.4),
        xaxis=dict(showgrid=True, gridcolor="#e1e0d9", title="R$"),
        yaxis=dict(showticklabels=False),
        plot_bgcolor=COR_SUPERFICIE,
        paper_bgcolor=COR_SUPERFICIE,
    )
    return fig


# =========================================================
# ETAPA 5: DIAGNÓSTICO E RECOMENDAÇÕES (usa números já validados)
# =========================================================
PROMPT_DIAGNOSTICO = """
Você é um auditor contábil sênior. Os valores abaixo já foram extraídos e conferidos
— use-os exatamente como estão, não os recalcule nem os altere.

DADOS DO BALANÇO
- Ativo Circulante: {ativo_circulante}
- Ativo Não Circulante: {ativo_nao_circulante}
- Imobilizado: {imobilizado}
- Ativo Total: {ativo_total}
- Passivo Circulante: {passivo_circulante}
- Exigível Não Circulante: {passivo_nao_circulante}
- Patrimônio Líquido: {patrimonio_liquido}
- Resultado do Exercício: {resultado_exercicio} ({resultado_tipo})
- Prejuízos Acumulados: {prejuizos_acumulados}

DADOS DA DRE (se disponíveis; ignore se todos forem N/D)
- Receita Líquida: {receita_liquida}
- Custo dos Produtos/Serviços: {custo_produtos_servicos}
- Lucro Bruto: {lucro_bruto}
- Despesas Operacionais: {despesas_operacionais}
- Resultado Financeiro: {resultado_financeiro}
- Resultado antes do IR/CSLL: {resultado_antes_ir}
- IR/CSLL: {ir_csll}
- Resultado Líquido da DRE: {resultado_liquido_dre} ({resultado_dre_tipo})

INDICADORES JÁ CALCULADOS
- Liquidez Corrente: {liquidez_corrente}
- Endividamento Geral: {endividamento_geral}
- Capital de Terceiros / Patrimônio Líquido: {capital_terceiros_sobre_pl}
- Patrimônio Líquido negativo (passivo a descoberto)? {pl_negativo}

Escreva em Markdown, com estas seções:

### 📈 Resultado e Prejuízos
Diga claramente se a empresa teve lucro ou prejuízo no exercício, de quanto foi, e
comente os prejuízos acumulados, se houver. Se houver dados de DRE, explique também
o resultado operacional (receita, custos, despesas) que levou a esse resultado.

### 💡 Diagnóstico Financeiro e Ideias de Ação
- **Análise da Saúde Financeira:** 2 parágrafos avaliando liquidez, nível de
  endividamento e se o Patrimônio Líquido está positivo ou negativo, com base nos
  números e indicadores acima.
- **Ideias e Recomendações Práticas:** de 3 a 5 sugestões práticas para a diretoria.

Sempre que citar um valor monetário, destaque-o em amarelo usando:
<span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>.
"""


def gerar_diagnostico(client, dados_brl, indicadores):
    contexto = dict(dados_brl)
    contexto["liquidez_corrente"] = (
        f"{indicadores['liquidez_corrente']:.2f}" if "liquidez_corrente" in indicadores else "N/D"
    )
    contexto["endividamento_geral"] = (
        f"{indicadores['endividamento_geral']:.1%}" if "endividamento_geral" in indicadores else "N/D"
    )
    contexto["capital_terceiros_sobre_pl"] = (
        f"{indicadores['capital_terceiros_sobre_pl']:.2f}" if "capital_terceiros_sobre_pl" in indicadores else "N/D"
    )
    contexto["pl_negativo"] = "SIM ⚠️" if indicadores.get("pl_negativo") else "Não"

    prompt = PROMPT_DIAGNOSTICO.format(**contexto)
    conteudo, erro = chamada_groq_segura(client, prompt, temperature=0.2)
    if erro:
        st.error(f"Falha ao gerar o diagnóstico: {erro}")
        return "_Não foi possível gerar o diagnóstico. Os valores extraídos acima continuam válidos._"
    return conteudo


# =========================================================
# MONTAGEM DAS SEÇÕES EM PYTHON (sem risco de a IA trocar valores)
# =========================================================
def destaque(v):
    """v já vem formatado (ex: 'R$ 1.234,56', '-R$ 1.234,56' ou 'N/D') via formatar_brl()."""
    return f'<span style="color: #F1C40F; font-weight: bold;">{v}</span>' if v and v != "N/D" else "N/D"


def montar_secao_balanco(b):
    return f"""
### 1. 🏢 Estrutura do Ativo

* **Ativo Circulante:** {destaque(b['ativo_circulante'])}
* **Ativo Não Circulante:** {destaque(b['ativo_nao_circulante'])}
* **Imobilizado (dentro do Não Circulante):** {destaque(b['imobilizado'])}
* **Ativo Total:** {destaque(b['ativo_total'])}

### 2. 💳 Estrutura do Passivo e Patrimônio Líquido

* **Passivo Circulante:** {destaque(b['passivo_circulante'])}
* **Exigível Não Circulante (Passivo Não Circulante):** {destaque(b['passivo_nao_circulante'])}
* **Patrimônio Líquido:** {destaque(b['patrimonio_liquido'])}
"""


def montar_secao_dre(b):
    if all(b.get(c, "N/D") == "N/D" for c in CAMPOS_DRE):
        return ""
    return f"""
### 📄 Demonstração do Resultado do Exercício (DRE)

* **Receita Líquida:** {destaque(b['receita_liquida'])}
* **Custo dos Produtos/Serviços Vendidos:** {destaque(b['custo_produtos_servicos'])}
* **Lucro Bruto:** {destaque(b['lucro_bruto'])}
* **Despesas Operacionais:** {destaque(b['despesas_operacionais'])}
* **Resultado Financeiro:** {destaque(b['resultado_financeiro'])}
* **Resultado antes do IR/CSLL:** {destaque(b['resultado_antes_ir'])}
* **IR/CSLL:** {destaque(b['ir_csll'])}
* **Resultado Líquido do Exercício (DRE):** {destaque(b['resultado_liquido_dre'])}
"""


# =========================================================
# FLUXO PRINCIPAL
# =========================================================
col1, col2 = st.columns(2)
with col1:
    pdf_balanco = st.file_uploader("📁 Balanço Patrimonial (obrigatório)", type=["pdf"])
with col2:
    pdf_dre = st.file_uploader("📁 DRE — Demonstração do Resultado (opcional, se for um arquivo separado)", type=["pdf"])

if pdf_balanco and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        st.session_state["processar"] = True

    if st.session_state.get("processar"):
        texto_balanco = processar_pdf(pdf_balanco.getvalue(), "Balanço Patrimonial")
        if texto_balanco is None:
            st.stop()

        texto_completo = texto_balanco
        if pdf_dre is not None:
            texto_dre = processar_pdf(pdf_dre.getvalue(), "DRE")
            if texto_dre:
                texto_completo += "\n\n--- DOCUMENTO DA DRE ---\n" + texto_dre

        client = Groq(api_key=groq_api_key)

        with st.spinner("Extraindo valores do balanço..."):
            try:
                dados = extrair_dados_estruturados(client, texto_completo)
            except RuntimeError as e:
                st.error(str(e))
                st.stop()

        recuperados = preencher_campos_faltantes(dados, texto_completo)
        if recuperados:
            st.caption(
                "🔎 Estes campos vieram null da IA e foram recuperados por leitura direta do texto: "
                + ", ".join(recuperados)
            )

        dados_num = {c: parse_valor_brl(dados.get(c)) for c in CAMPOS_BALANCO + CAMPOS_DRE}

        correcao_anc = corrigir_confusao_imobilizado(dados_num, texto_completo)
        if correcao_anc:
            st.caption(
                "🔧 O Ativo Não Circulante veio idêntico ao Imobilizado — sinal de erro de extração "
                "(o Imobilizado é só uma parte do Ativo Não Circulante, não o total da seção). "
                f"Corrigido automaticamente para {formatar_brl(correcao_anc)} usando leitura direta do texto."
            )

        calculados = completar_por_equacao_contabil(dados_num)
        if calculados:
            st.caption(
                "🧮 Estes campos não vieram no texto — foram calculados por diferença "
                "contábil (Ativo = Passivo + PL, e Ativo = Circulante + Não Circulante): "
                + ", ".join(calculados)
            )

        sugestao = sugerir_correcao_ativo(dados_num)
        if sugestao:
            nome_campo = "Ativo Circulante" if sugestao["campo"] == "ativo_circulante" else "Ativo Não Circulante"
            st.markdown(escapar_dolar(
                f"🔧 **Possível correção automática encontrada.** O valor extraído de **{nome_campo}** "
                f"({formatar_brl(sugestao['valor_extraido'])}) não bate com o Ativo Total, mas o Passivo + PL "
                f"confirma que o Ativo Total ({formatar_brl(dados_num.get('ativo_total'))}) está correto. "
                f"Isso costuma acontecer quando o OCR lê dígitos como letras num PDF escaneado. Pela diferença "
                f"algébrica, o valor correto de {nome_campo} deveria ser "
                f"**{formatar_brl(sugestao['valor_sugerido'])}**."
            ))
            aplicar_correcao = st.checkbox(
                f"✅ Aplicar o valor sugerido para {nome_campo} ({formatar_brl(sugestao['valor_sugerido'])})",
                key="aplicar_correcao_ativo",
            )
            if aplicar_correcao:
                dados_num[sugestao["campo"]] = sugestao["valor_sugerido"]
                st.caption(
                    f"↪️ Aplicado: {nome_campo} ajustado para {formatar_brl(sugestao['valor_sugerido'])} "
                    "(valor calculado por diferença contábil, não extraído diretamente do PDF — confira com o original)."
                )

        # dados_brl é sempre derivado de dados_num (float já parseado), nunca da string crua da IA
        # ou do regex — assim o formato exibido é sempre consistente ("R$ 1.234,56" / "-R$ ..." / "N/D"),
        # não importa se o valor veio da IA, do fallback ou foi calculado por diferença.
        dados_brl = {c: formatar_brl(dados_num.get(c)) for c in CAMPOS_BALANCO + CAMPOS_DRE}
        dados_brl["resultado_tipo"] = dados.get("resultado_tipo") or "não identificado"
        dados_brl["resultado_dre_tipo"] = dados.get("resultado_dre_tipo") or "não identificado"

        avisos = validar_balanco(dados_num) + validar_dre(dados_num)
        if avisos:
            st.warning(
                "Encontrei inconsistências nos valores extraídos. Isso costuma acontecer quando o PDF "
                "tem colunas lado a lado (Ativo x Passivo) e o texto extraído embaralha a ordem. "
                "Revise com atenção antes de usar o relatório:"
            )
            for aviso in avisos:
                st.markdown(escapar_dolar(aviso))
        else:
            st.success("✅ Os totais extraídos são consistentes entre si (Ativo = Passivo + PL).")

        indicadores = calcular_indicadores(dados_num)

        # --- Resumo rápido (métricas) ---
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Ativo Total", formatar_brl(dados_num.get("ativo_total")))
        with m2:
            resultado_val = dados_num.get("resultado_exercicio")
            st.metric(
                "Resultado do Exercício",
                formatar_brl(resultado_val),
                delta=None if resultado_val is None else ("Lucro" if resultado_val >= 0 else "Prejuízo"),
                delta_color="normal" if (resultado_val is None or resultado_val >= 0) else "inverse",
            )
        with m3:
            liq = indicadores.get("liquidez_corrente")
            st.metric("Liquidez Corrente", f"{liq:.2f}" if liq is not None else "N/D")

        if indicadores.get("pl_negativo"):
            st.markdown(
                f'<span style="color:{COR_CRITICO}; font-weight:bold;">⚠️ Patrimônio Líquido negativo '
                f'(passivo a descoberto) — a empresa deve mais do que possui.</span>',
                unsafe_allow_html=True,
            )

        # --- Gráficos de composição ---
        g1, g2 = st.columns(2)
        fig_ativo = grafico_composicao(
            "Ativo",
            [
                ("Ativo Circulante", dados_num.get("ativo_circulante"), COR_CIRCULANTE),
                ("Ativo Não Circulante", dados_num.get("ativo_nao_circulante"), COR_NAO_CIRCULANTE),
            ],
        )
        fig_passivo = grafico_composicao(
            "Passivo + Patrimônio Líquido",
            [
                ("Passivo Circulante", dados_num.get("passivo_circulante"), COR_CIRCULANTE),
                ("Exigível Não Circulante", dados_num.get("passivo_nao_circulante"), COR_NAO_CIRCULANTE),
                ("Patrimônio Líquido", dados_num.get("patrimonio_liquido"), COR_PL),
            ],
        )
        if fig_ativo:
            g1.plotly_chart(fig_ativo, use_container_width=True)
        if fig_passivo:
            g2.plotly_chart(fig_passivo, use_container_width=True)

        # --- Seções detalhadas ---
        secao_balanco = montar_secao_balanco(dados_brl)
        secao_dre = montar_secao_dre(dados_brl)
        st.markdown(escapar_dolar(secao_balanco), unsafe_allow_html=True)
        if secao_dre:
            st.markdown(escapar_dolar(secao_dre), unsafe_allow_html=True)

        with st.spinner("Gerando diagnóstico..."):
            diagnostico = gerar_diagnostico(client, dados_brl, indicadores)
        st.markdown(escapar_dolar(diagnostico), unsafe_allow_html=True)

        # --- Download do relatório completo ---
        relatorio_md = escapar_dolar(
            f"# Relatório de Análise Contábil\n\n{secao_balanco}\n{secao_dre}\n{diagnostico}"
        )
        st.download_button(
            "⬇️ Baixar relatório em Markdown",
            data=relatorio_md.encode("utf-8"),
            file_name="relatorio_analise_contabil.md",
            mime="text/markdown",
        )

        with st.expander("🔍 Ver texto extraído do PDF (para conferência manual)"):
            st.text(texto_completo)
elif not groq_api_key:
    st.info("Insira sua API Key da Groq na barra lateral para começar.")
