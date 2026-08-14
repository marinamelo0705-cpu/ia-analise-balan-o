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

    return texto_pdf


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
    """
    if valor_str is None:
        return None
    s = str(valor_str).strip()
    if not s or s.lower() in ("null", "none", "n/d", "nd"):
        return None

    negativo = "(" in s and ")" in s
    if s.lstrip().startswith("-"):
        negativo = True

    s = re.sub(r"[^0-9,.\-]", "", s)
    s = s.replace(".", "").replace(",", ".")
    if s in ("", "-", "."):
        return None
    try:
        valor = float(s)
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
# (ex.: "13. 474. 832,27" em vez de "13.474.832,27") — testado com OCR real.
PADRAO_VALOR = re.compile(r"\(?-?\s?R?\$?\s?\d{1,3}(?:\s?\.\s?\d{3})*\s?,\s?\d{2}\)?")


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
    Passo B: alguns balanços (confirmado testando um balanço real) não
    escrevem "Ativo Circulante" como frase única — em vez disso imprimem
    "ATIVO" e "CIRCULANTE" como cabeçalhos de seção EM LINHAS SEPARADAS,
    cada um com seu próprio total ao lado (formato de "balancete"
    hierárquico: ATIVO > CIRCULANTE > contas individuais > NÃO CIRCULANTE >
    IMOBILIZADO...). Esse parser acompanha o contexto (dentro de ATIVO ou de
    PASSIVO) conforme lê o texto de cima pra baixo, então "CIRCULANTE"
    sozinho vira "ativo_circulante" ou "passivo_circulante" dependendo de
    qual cabeçalho apareceu por último.
    """
    encontrados = {}
    contexto = None

    def registrar(campo, valor):
        if campo not in encontrados:
            encontrados[campo] = _limpar_valor_ocr(valor)

    for linha in texto_pdf.split("\n"):
        s = linha.strip()
        # troca (não remove) caracteres de ruído de OCR por espaço, senão
        # duas palavras coladas por um caractere de ruído (ex: "ATIVO!Secao")
        # quebram o casamento de fronteira de palavra (\b) do regex abaixo.
        low = re.sub(r"[^\wçãõáéíóúâêôà,.\-()]", " ", s.lower())
        low = re.sub(r"\s+", " ", low).strip()

        if re.match(r"^ativo\b", low) and "circulante" not in low:
            contexto = "ativo"
            vals = PADRAO_VALOR.findall(s)
            if vals:
                registrar("ativo_total", vals[-1])
            continue
        if re.match(r"^passivo\b", low) and "circulante" not in low:
            contexto = "passivo"
            continue
        if contexto and re.match(r"^n[ãa]o[\s-]*circulante\b", low):
            vals = PADRAO_VALOR.findall(s)
            if vals:
                registrar(f"{contexto}_nao_circulante", vals[-1])
            continue
        if contexto and re.match(r"^circulante\b", low):
            vals = PADRAO_VALOR.findall(s)
            if vals:
                registrar(f"{contexto}_circulante", vals[-1])
            continue
        if re.match(r"^imobilizado\b", low):
            vals = PADRAO_VALOR.findall(s)
            if vals:
                registrar("imobilizado", vals[-1])
            continue

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

    return avisos


def validar_dre(n):
    avisos = []
    rl, cpv, lb = n.get("receita_liquida"), n.get("custo_produtos_servicos"), n.get("lucro_bruto")
    if rl is not None and cpv is not None and lb is not None:
        calc = rl + cpv if cpv < 0 else rl - cpv  # aceita CPV já negativo ou positivo
        if abs(calc - lb) > tolerancia(rl):
            avisos.append(
                f"⚠️ Receita Líquida − Custo dos Produtos/Serviços ({formatar_brl(calc)}) não bate com "
                f"o Lucro Bruto informado na DRE ({formatar_brl(lb)}). Confira os valores extraídos."
            )
    return avisos


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
        # dados_brl é sempre derivado de dados_num (float já parseado), nunca da string crua da IA
        # ou do regex — assim o formato exibido é sempre consistente ("R$ 1.234,56" / "-R$ ..." / "N/D"),
        # não importa se o valor veio da IA ou do fallback.
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
