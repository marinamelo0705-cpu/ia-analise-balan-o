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

# Paleta (dataviz skill) — cores fixas por papel
COR_CIRCULANTE = "#2a78d6"       # azul
COR_NAO_CIRCULANTE = "#1baf7a"   # aqua
COR_PL = "#4a3aa7"               # violeta
COR_BOM = "#0ca30c"              # status "good"
COR_CRITICO = "#d03b3b"          # status "critical"
COR_SUPERFICIE = "#fcfcfb"

# =========================================================
# API KEY
# =========================================================
if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

MODELO = "llama-3.3-70b-versatile"

# =========================================================
# EXTRAÇÃO DE TEXTO DO PDF
# =========================================================
LIMIAR_TEXTO_PAGINA = 40  # abaixo disso, a página é considerada "sem texto direto"


def ocr_pagina(img):
    try:
        return pytesseract.image_to_string(img, lang="por", config="--psm 6")
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
                    "seu repositório do GitHub contendo a linha `tesseract-ocr-por`, depois "
                    "faça o **Reboot app** no Streamlit Cloud."
                )
            return pytesseract.image_to_string(img, lang="eng", config="--psm 6")
        raise


def limpar_numeros_ocr(texto):
    texto = re.sub(r"(?<=\d)\s+\.\s*(?=\d)", ".", texto)
    texto = re.sub(r"(?<=\d)\s*\.\s+(?=\d)", ".", texto)
    texto = re.sub(r"(?<=\d)\s+,\s*(?=\d)", ",", texto)
    texto = re.sub(r"(?<=\d)\s*,\s+(?=\d)", ",", texto)
    return texto


def extrair_texto_pdf(bytes_data, rotulo=""):
    texto_paginas = []
    paginas_para_ocr = []
    tabelas_texto = ""

    with pdfplumber.open(io.BytesIO(bytes_data)) as pdf:
        for i, page in enumerate(pdf.pages):
            t = page.extract_text(layout=True) or ""
            if len(t.strip()) < LIMIAR_TEXTO_PAGINA:
                paginas_para_ocr.append(i)
                texto_paginas.append("")
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
        st.info(f"ℹ️ {rotulo}: página(s) {numeros} com pouco texto digital — aplicando OCR...")
        for i in paginas_para_ocr:
            try:
                imgs = convert_from_bytes(bytes_data, dpi=400, first_page=i + 1, last_page=i + 1)
                texto_ocr = ""
                for img in imgs:
                    texto_ocr += ocr_pagina(img.convert("L")) + "\n"
                texto_paginas[i] = texto_ocr
            except Exception as e:
                st.warning(f"Não foi possível aplicar OCR na página {i + 1} de {rotulo}: {e}")

    texto_pdf = "\n".join(f"--- página {i + 1} ---\n{t}" for i, t in enumerate(texto_paginas))
    if tabelas_texto:
        texto_pdf += "\n\n--- TABELAS DETECTADAS ---\n" + tabelas_texto

    return limpar_numeros_ocr(texto_pdf)


# =========================================================
# HELPERS DE NÚMERO / JSON
# =========================================================
def parse_valor_brl(valor_str):
    if valor_str is None:
        return None
    s = str(valor_str).strip()
    if not s or s.lower() in ("null", "none", "n/d", "nd"):
        return None

    negativo = "(" in s and ")" in s or s.lstrip().startswith("-")
    s = re.sub(r"[^0-9,.]", "", s)
    if not s:
        return None

    ultimo_sep = max(s.rfind("."), s.rfind(","))
    if ultimo_sep == -1:
        parte_inteira, parte_decimal = s, ""
    else:
        parte_inteira = re.sub(r"[.,]", "", s[:ultimo_sep])
        parte_decimal = s[ultimo_sep + 1:]

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


def extrair_json_da_resposta(texto):
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        raise ValueError("Nenhum JSON encontrado na resposta do modelo.")
    return json.loads(match.group(0))


def chamada_groq_segura(client, prompt, temperature=0.0):
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
# ETAPA 1: EXTRAÇÃO ESTRUTURADA (JSON)
# =========================================================
CAMPOS_BALANCO = [
    "ativo_circulante", "ativo_nao_circulante", "imobilizado", "ativo_total",
    "passivo_circulante", "passivo_nao_circulante", "patrimonio_liquido",
    "resultado_exercicio", "prejuizos_acumulados"
]

CAMPOS_DRE = [
    "receita_liquida", "custo_produtos_servicos", "lucro_bruto", "despesas_operacionais",
    "resultado_financeiro", "resultado_antes_ir", "ir_csll", "resultado_liquido_dre"
]

CAMPOS_ESPERADOS = CAMPOS_BALANCO + CAMPOS_DRE + ["resultado_tipo", "resultado_dre_tipo"]

PROMPT_EXTRACAO = """
Você é um auditor contábil sênior extraindo dados de um Balanço Patrimonial e DRE.
Analise APENAS os dados explícitos contidos no texto. NÃO calcule, NÃO invente valores.

ATENÇÃO CRÍTICA:
1. NÃO CONFUNDA ATIVO CIRCULANTE COM PASSIVO CIRCULANTE.
2. Copie o valor exatamente como consta no documento para a coluna mais recente.

--- TEXTO EXTRAÍDO ---
{texto_pdf}
----------------------

Responda SOMENTE com um JSON válido:
{{
  "ativo_circulante": "...",
  "ativo_nao_circulante": "...",
  "imobilizado": "...",
  "ativo_total": "...",
  "passivo_circulante": "...",
  "passivo_nao_circulante": "...",
  "patrimonio_liquido": "...",
  "resultado_exercicio": "...",
  "resultado_tipo": "lucro" ou "prejuizo",
  "prejuizos_acumulados": "...",
  "receita_liquida": "...",
  "custo_produtos_servicos": "...",
  "lucro_bruto": "...",
  "despesas_operacionais": "...",
  "resultado_financeiro": "...",
  "resultado_antes_ir": "...",
  "ir_csll": "...",
  "resultado_liquido_dre": "...",
  "resultado_dre_tipo": "lucro" ou "prejuizo"
}}
"""


def extrair_dados_estruturados(client, texto_pdf):
    conteudo, erro = chamada_groq_segura(client, PROMPT_EXTRACAO.format(texto_pdf=texto_pdf), temperature=0.0)
    if erro:
        raise RuntimeError(f"Falha na API da Groq: {erro}")
    dados = extrair_json_da_resposta(conteudo)
    for campo in CAMPOS_ESPERADOS:
        dados.setdefault(campo, None)
    return dados


# =========================================================
# ETAPA 2: VALIDAÇÃO ARITMÉTICA E REGRAS DRE
# =========================================================
CAMPOS_DEDUCAO_DRE = ["custo_produtos_servicos", "despesas_operacionais", "ir_csll"]


def tolerancia(base):
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
                f"⚠️ Ativo Circulante + Ativo Não Circulante ({formatar_brl(soma)}) difere do "
                f"Ativo Total ({formatar_brl(at)})."
            )

    if pc is not None and pnc is not None and pl is not None and at is not None:
        soma = pc + pnc + pl
        if abs(soma - at) > tolerancia(at):
            avisos.append(
                f"⚠️ Passivo + PL ({formatar_brl(soma)}) difere do Ativo Total ({formatar_brl(at)})."
            )

    return avisos


# =========================================================
# ETAPA 3: GERAÇÃO DE DIAGNÓSTICO
# =========================================================
def gerar_diagnostico_ia(client, dados_num):
    prompt_diag = f"""
    Como auditor contábil, analise os seguintes dados validados:
    - Ativo Total: {formatar_brl(dados_num.get('ativo_total'))}
    - Ativo Circulante: {formatar_brl(dados_num.get('ativo_circulante'))}
    - Passivo Circulante: {formatar_brl(dados_num.get('passivo_circulante'))}
    - Patrimônio Líquido: {formatar_brl(dados_num.get('patrimonio_liquido'))}
    - Resultado do Exercício: {formatar_brl(dados_num.get('resultado_exercicio'))}

    Forneça uma análise de saúde financeira com:
    1. Análise da Liquidez (Ativo Circulante vs Passivo Circulante)
    2. Nível de Endividamento e Solvência
    3. 3 Recomendações Práticas
    """
    resp, erro = chamada_groq_segura(client, prompt_diag, temperature=0.2)
    return resp if not erro else "Não foi possível gerar o diagnóstico textual."


# =========================================================
# INTERFACE PRINCIPAL (STREAMLIT APP)
# =========================================================
pdf_file = st.file_uploader("Suba o PDF do Balanço / DRE aqui", type=["pdf"])

if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e auditando dados..."):
            client = Groq(api_key=groq_api_key)
            bytes_pdf = pdf_file.read()
            texto_extraido = extrair_texto_pdf(bytes_pdf, rotulo="Balanço")

            if texto_extraido:
                raw_json = extrair_dados_estruturados(client, texto_extraido)
                
                # Parse numérico
                dados_num = {k: parse_valor_brl(v) for k, v in raw_json.items() if k in CAMPOS_ESPERADOS}

                # Exibição dos resultados
                st.success("Análise de Balanço Concluída com Sucesso!")
                
                # Alertas de Validação
                alertas = validar_balanco(dados_num)
                for al in alertas:
                    st.warning(al)

                # Colunas de exibição
                c1, c2 = st.columns(2)
                with c1:
                    st.subheader("🏢 Estrutura do Ativo")
                    st.write(f"**Ativo Circulante:** {formatar_brl(dados_num.get('ativo_circulante'))}")
                    st.write(f"**Ativo Não Circulante:** {formatar_brl(dados_num.get('ativo_nao_circulante'))}")
                    st.write(f"**Ativo Total:** {formatar_brl(dados_num.get('ativo_total'))}")

                with c2:
                    st.subheader("💳 Passivo e Patrimônio Líquido")
                    st.write(f"**Passivo Circulante:** {formatar_brl(dados_num.get('passivo_circulante'))}")
                    st.write(f"**Passivo Não Circulante:** {formatar_brl(dados_num.get('passivo_nao_circulante'))}")
                    st.write(f"**Patrimônio Líquido:** {formatar_brl(dados_num.get('patrimonio_liquido'))}")

                st.markdown("---")
                st.subheader("💡 Diagnóstico do Auditor")
                diag = gerar_diagnostico_ia(client, dados_num)
                st.markdown(diag)

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Forneça uma API Key da Groq para iniciar a análise.")
