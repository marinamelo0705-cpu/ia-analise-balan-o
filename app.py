import json
import re

import streamlit as st
from groq import Groq
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image

# =========================================================
# CONFIGURAÇÃO DA PÁGINA
# =========================================================
st.set_page_config(page_title="Analisador Contábil Completo", page_icon="📊", layout="wide")

st.title("📊 Analisador Contábil de Balanços e DRE")
st.caption(
    "Envie o PDF do Balanço Patrimonial (e, se tiver, a DRE) para extrair os valores, "
    "conferir a saúde financeira da empresa e identificar prejuízos."
)

# =========================================================
# API KEY
# =========================================================
if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

MODELO = "llama-3.3-70b-versatile"  # confira em console.groq.com/docs/models se este nome ainda está disponível


# =========================================================
# EXTRAÇÃO DE TEXTO DO PDF
# =========================================================
def extrair_texto_pdf(pdf_file):
    """
    Extrai texto preservando o layout de colunas (Ativo x Passivo lado a lado),
    e complementa com uma leitura estruturada de tabelas quando disponível.
    Isso evita que o valor do Passivo Circulante seja lido como se fosse
    o Ativo Circulante (e vice-versa) quando as duas colunas do balanço
    são "achatadas" em uma única linha de texto sem noção de posição.
    """
    texto_pdf = ""
    tabelas_texto = ""

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            # layout=True preserva os espaços em branco de acordo com a posição
            # x/y original do PDF, o que mantém as colunas do balanço alinhadas
            # em vez de misturar Ativo e Passivo numa única sequência de números.
            t = page.extract_text(layout=True)
            if t:
                texto_pdf += t + "\n"

            # Tentativa adicional: extrair tabelas de forma estruturada.
            # Quando o PDF tem grade/tabela real, isso dá ao modelo uma
            # visão "linha = conta, coluna = valor" muito mais confiável
            # do que o texto corrido.
            try:
                tabelas = page.extract_tables()
                for tabela in tabelas:
                    for linha in tabela:
                        celulas = [c.strip() for c in linha if c and c.strip()]
                        if celulas:
                            tabelas_texto += " | ".join(celulas) + "\n"
            except Exception:
                pass

    if tabelas_texto:
        texto_pdf += "\n\n--- TABELAS DETECTADAS (linha = conta, colunas = valores) ---\n" + tabelas_texto

    return texto_pdf


def extrair_texto_ocr(bytes_data):
    """
    Fallback para PDFs escaneados/fotografados. Usa DPI mais alto (300) para
    melhorar a legibilidade de números pequenos e --psm 4 (texto organizado
    em colunas/blocos verticais), que tende a preservar melhor a ordem de
    leitura de balanços do que --psm 6 (bloco único de texto).
    """
    images = convert_from_bytes(bytes_data, dpi=300)
    texto_pdf = ""
    for img in images:
        img_gray = img.convert("L")
        texto_ocr = pytesseract.image_to_string(img_gray, lang="por", config="--psm 4")
        texto_pdf += texto_ocr + "\n"
    return texto_pdf


# =========================================================
# HELPERS DE NÚMERO / JSON
# =========================================================
def parse_valor_brl(valor_str):
    """Converte 'R$ 21.966.947,43' ou '21.966.947,43' em float 21966947.43."""
    if valor_str is None:
        return None
    s = str(valor_str)
    s = re.sub(r"[^0-9,.\-]", "", s)
    if s in ("", "-", None):
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def formatar_brl(valor):
    if valor is None:
        return "N/D"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def extrair_json_da_resposta(texto):
    """
    Extrai o primeiro bloco JSON válido da resposta do modelo, mesmo que ele
    venha cercado de texto ou de ```json ... ```.
    """
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        raise ValueError("Nenhum JSON encontrado na resposta do modelo.")
    return json.loads(match.group(0))


# =========================================================
# ETAPA 1: EXTRAÇÃO ESTRUTURADA (JSON) — SEM TEXTO LIVRE
# =========================================================
CAMPOS_ESPERADOS = [
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

PROMPT_EXTRACAO = """
Você é um auditor contábil sênior extraindo dados de um Balanço Patrimonial e/ou DRE.
Analise APENAS os dados explícitos contidos no texto abaixo. NÃO calcule, NÃO invente
e NÃO estime nenhum valor — copie exatamente o número que está escrito no texto ao lado
de cada conta contábil.

ATENÇÃO: balanços patrimoniais brasileiros normalmente têm DUAS colunas lado a lado —
o ATIVO (esquerda) e o PASSIVO + PATRIMÔNIO LÍQUIDO (direita). Preste muita atenção
para não confundir "Ativo Circulante" com "Passivo Circulante", nem "Ativo Não Circulante"
com "Passivo Não Circulante" — são contas diferentes que costumam aparecer na mesma
altura da página, em colunas diferentes. Use os rótulos exatos do texto para decidir a
qual conta cada valor pertence.

--- TEXTO EXTRAÍDO DO PDF ---
{texto_pdf}
-----------------------------

Responda SOMENTE com um JSON válido (sem markdown, sem texto antes ou depois), no
formato abaixo. Se um valor não existir explicitamente no texto, use null.

{{
  "ativo_circulante": "valor exatamente como está escrito, ex: 21.966.947,43",
  "ativo_nao_circulante": "...",
  "imobilizado": "...",
  "ativo_total": "...",
  "passivo_circulante": "...",
  "passivo_nao_circulante": "... (Exigível Não Circulante)",
  "patrimonio_liquido": "...",
  "resultado_exercicio": "... (valor do lucro ou prejuízo do exercício)",
  "resultado_tipo": "lucro" ou "prejuizo" ou null,
  "prejuizos_acumulados": "..."
}}
"""


def extrair_dados_estruturados(client, texto_pdf):
    resp = client.chat.completions.create(
        messages=[{"role": "user", "content": PROMPT_EXTRACAO.format(texto_pdf=texto_pdf)}],
        model=MODELO,
        temperature=0.1,
    )
    dados = extrair_json_da_resposta(resp.choices[0].message.content)
    for campo in CAMPOS_ESPERADOS:
        dados.setdefault(campo, None)
    return dados


# =========================================================
# ETAPA 2: VALIDAÇÃO ARITMÉTICA (CONFERE SE OS NÚMEROS BATEM)
# =========================================================
def validar_balanco(dados_num):
    avisos = []

    ac = dados_num.get("ativo_circulante")
    anc = dados_num.get("ativo_nao_circulante")
    at = dados_num.get("ativo_total")
    pc = dados_num.get("passivo_circulante")
    pnc = dados_num.get("passivo_nao_circulante")
    pl = dados_num.get("patrimonio_liquido")

    TOL = 5.0  # tolerância de arredondamento em R$

    if ac is not None and anc is not None and at is not None:
        soma_ativo = ac + anc
        if abs(soma_ativo - at) > TOL:
            avisos.append(
                f"⚠️ Ativo Circulante + Ativo Não Circulante ({formatar_brl(soma_ativo)}) "
                f"não bate com o Ativo Total informado ({formatar_brl(at)}). "
                f"Confira os valores extraídos com o PDF original."
            )

    if pc is not None and pnc is not None and pl is not None and at is not None:
        soma_passivo = pc + pnc + pl
        if abs(soma_passivo - at) > TOL:
            avisos.append(
                f"⚠️ Passivo Circulante + Exigível Não Circulante + Patrimônio Líquido "
                f"({formatar_brl(soma_passivo)}) não bate com o Ativo Total "
                f"({formatar_brl(at)}). Pela equação contábil (Ativo = Passivo + PL), "
                f"esses valores deveriam ser iguais — pode indicar erro de extração."
            )

    return avisos


# =========================================================
# ETAPA 3: DIAGNÓSTICO E RECOMENDAÇÕES (usa os números já validados)
# =========================================================
PROMPT_DIAGNOSTICO = """
Você é um auditor contábil sênior. Os valores abaixo já foram extraídos e conferidos
do balanço — use-os exatamente como estão, não os recalcule nem os altere.

- Ativo Circulante: R$ {ativo_circulante}
- Ativo Não Circulante: R$ {ativo_nao_circulante}
- Imobilizado: R$ {imobilizado}
- Ativo Total: R$ {ativo_total}
- Passivo Circulante: R$ {passivo_circulante}
- Exigível Não Circulante: R$ {passivo_nao_circulante}
- Patrimônio Líquido: R$ {patrimonio_liquido}
- Resultado do Exercício: R$ {resultado_exercicio} ({resultado_tipo})
- Prejuízos Acumulados: R$ {prejuizos_acumulados}

Escreva em Markdown, com estas duas seções:

### 📈 Resultado e Prejuízos
Explique se a empresa teve lucro ou prejuízo no exercício, de quanto foi, e comente
os prejuízos acumulados, se houver.

### 💡 Diagnóstico Financeiro e Ideias de Ação
- **Análise da Saúde Financeira:** 2 parágrafos avaliando liquidez, endividamento e
  se o Patrimônio Líquido está positivo ou negativo, com base nos números acima.
- **Ideias e Recomendações Práticas:** de 3 a 5 sugestões práticas para a diretoria.

Sempre que citar um valor monetário, destaque-o em amarelo usando:
<span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>.
"""


def gerar_diagnostico(client, dados_brl):
    prompt = PROMPT_DIAGNOSTICO.format(**dados_brl)
    resp = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=MODELO,
        temperature=0.2,
    )
    return resp.choices[0].message.content


# =========================================================
# MONTAGEM DA SEÇÃO DE ESTRUTURA (Python puro, sem risco de o modelo trocar valores)
# =========================================================
def montar_secao_estrutura(dados_brl):
    def destaque(v):
        return f'<span style="color: #F1C40F; font-weight: bold;">R$ {v}</span>' if v and v != "None" else "N/D"

    return f"""
### 1. 🏢 Estrutura do Ativo

* **Ativo Circulante:** {destaque(dados_brl['ativo_circulante'])}
* **Ativo Não Circulante:** {destaque(dados_brl['ativo_nao_circulante'])}
* **Imobilizado (dentro do Não Circulante):** {destaque(dados_brl['imobilizado'])}
* **Ativo Total:** {destaque(dados_brl['ativo_total'])}

### 2. 💳 Estrutura do Passivo e Patrimônio Líquido

* **Passivo Circulante:** {destaque(dados_brl['passivo_circulante'])}
* **Exigível Não Circulante (Passivo Não Circulante):** {destaque(dados_brl['passivo_nao_circulante'])}
* **Patrimônio Líquido:** {destaque(dados_brl['patrimonio_liquido'])}
"""


# =========================================================
# FLUXO PRINCIPAL
# =========================================================
pdf_file = st.file_uploader("Arraste e solte o PDF do Balanço/DRE aqui", type=["pdf"])

if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        bytes_data = pdf_file.getvalue()

        texto_pdf = extrair_texto_pdf(pdf_file)

        if len(texto_pdf.strip()) < 50:
            st.info("ℹ️ PDF escaneado/fotografado detectado. Executando leitura via OCR com tratamento de imagem...")
            texto_pdf = extrair_texto_ocr(bytes_data)

        if len(texto_pdf.strip()) < 30:
            st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
            st.stop()

        client = Groq(api_key=groq_api_key)

        with st.spinner("Extraindo valores do balanço..."):
            dados = extrair_dados_estruturados(client, texto_pdf)

        # versões numéricas (para validar) e versões em texto (para exibir)
        dados_num = {campo: parse_valor_brl(dados.get(campo)) for campo in CAMPOS_ESPERADOS}
        dados_brl = {campo: (dados.get(campo) if dados.get(campo) else "N/D") for campo in CAMPOS_ESPERADOS}
        dados_brl["resultado_tipo"] = dados.get("resultado_tipo") or "não identificado"

        avisos = validar_balanco(dados_num)
        if avisos:
            st.warning(
                "Encontrei uma inconsistência entre os valores extraídos. "
                "Isso costuma acontecer quando o PDF tem colunas lado a lado (Ativo x Passivo) "
                "e o texto extraído embaralha a ordem. Revise os valores abaixo com atenção "
                "antes de usar o relatório:"
            )
            for aviso in avisos:
                st.markdown(aviso)
        else:
            st.success("✅ Ativo Total e Passivo + Patrimônio Líquido batem — valores consistentes.")

        st.markdown(montar_secao_estrutura(dados_brl), unsafe_allow_html=True)

        with st.spinner("Gerando diagnóstico..."):
            diagnostico = gerar_diagnostico(client, dados_brl)
        st.markdown(diagnostico, unsafe_allow_html=True)

        with st.expander("🔍 Ver texto extraído do PDF (para conferência manual)"):
            st.text(texto_pdf)
