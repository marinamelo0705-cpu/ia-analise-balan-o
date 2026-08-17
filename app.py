import io
import re
import streamlit as st
from groq import Groq
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np
import cv2

# Configuração da página
st.set_page_config(page_title="Analisador Contábil Completo", page_icon="📊", layout="wide")
st.title("📊 Analisador Inteligente de Balanços e DRE")
st.markdown("Suba o arquivo PDF contábil da empresa para gerar o diagnóstico estruturado de Ativos, Passivos, Resultado e Recomendações.")

# Busca a chave salva nos Secrets do Streamlit ou pede na barra lateral
if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
    st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

# Upload do PDF
pdf_file = st.file_uploader("Arraste e solte o PDF do Balanço/DRE aqui", type=["pdf"])


def preparar_imagem_para_ocr(imagem_pil):
    """
    Pré-processa a imagem escaneada antes do OCR:
    - converte pra tons de cinza
    - aplica median blur pra atenuar marcas d'água / textura de fundo (hachurados finos)
    - binariza com threshold automático (Otsu), deixando o texto preto puro
      sobre fundo branco puro — isso melhora muito a leitura do Tesseract em
      documentos escaneados com ruído/marca d'água sobre a tabela.
    """
    arr = np.array(imagem_pil.convert('L'))
    arr = cv2.medianBlur(arr, 3)
    _, arr_bin = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(arr_bin)


def limpar_valor_br(valor_bruto):
    """Normaliza um número capturado via OCR pro formato brasileiro (1.234.567,89),
    corrigindo erros comuns de OCR como hífen/espaço no lugar do separador de milhar."""
    limpo = valor_bruto.replace(" ", "").replace("-", ".")
    m = re.match(r'^(.*)[.,](\d{2})$', limpo)
    if m:
        parte_inteira = m.group(1).replace(",", ".")
        parte_decimal = m.group(2)
        return f"{parte_inteira},{parte_decimal}"
    return limpo


def extrair_resultado_exercicio(texto):
    """
    Extração determinística (via regex, não via IA) do Lucro ou Prejuízo Líquido do
    Exercício na DRE. Esse valor é copiado direto do texto reconhecido pelo OCR, sem
    passar pela reescrita da IA — evita o erro de transcrição de dígito que a IA pode
    cometer ao reformatar números longos em meio a um texto grande.
    Retorna uma string tipo "Lucro Líquido do Exercício: R$ 793.376,08" ou None se
    não encontrar o padrão no texto.
    """
    padrao = re.compile(
        r'(LUCRO\s+L[IÍ]QUIDO|PREJU[IÍ]ZO\s+L[IÍ]QUIDO|PREJU[IÍ]ZO)\s+DO\s+EXERC[IÍ]CIO'
        r'[^\d\-]*([\d][\d.,\-\s]*\d)',
        re.IGNORECASE
    )
    m = padrao.search(texto)
    if not m:
        return None
    tipo = "Prejuízo" if "PREJU" in m.group(1).upper() else "Lucro"
    valor = limpar_valor_br(m.group(2))
    return f"{tipo} Líquido do Exercício (valor confirmado por extração direta do documento, não recalcule nem reescreva): R$ {valor}"


if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e extraindo dados (pode levar alguns segundos se for escaneado)..."):
            try:
                bytes_data = pdf_file.read()
                texto_pdf = ""

                # 1. Extração página a página: cada página é avaliada individualmente.
                with pdfplumber.open(io.BytesIO(bytes_data)) as pdf:
                    for i, page in enumerate(pdf.pages, start=1):
                        t = page.extract_text()
                        if t and len(t.strip()) >= 20:
                            texto_pdf += t + "\n"
                        else:
                            st.info(f"ℹ️ Página {i} parece escaneada/fotografada. Aplicando OCR com tratamento de imagem...")
                            imagens_pagina = convert_from_bytes(bytes_data, dpi=400, first_page=i, last_page=i)
                            for img in imagens_pagina:
                                img_tratada = preparar_imagem_para_ocr(img)
                                # Duas leituras com modos de segmentação diferentes: psm 6
                                # (bloco único) e psm 4 (colunas) — juntas dão mais chance
                                # de acertar números que o psm 6 às vezes funde com o rótulo.
                                texto_psm6 = pytesseract.image_to_string(img_tratada, lang="por", config='--oem 3 --psm 6')
                                texto_psm4 = pytesseract.image_to_string(img_tratada, lang="por", config='--oem 3 --psm 4')
                                texto_pdf += f"\n--- Página {i} (leitura A) ---\n{texto_psm6}\n"
                                texto_pdf += f"\n--- Página {i} (leitura B, mesma página, outro modo de OCR) ---\n{texto_psm4}\n"

                # Trava de segurança final
                if len(texto_pdf.strip()) < 30:
                    st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
                    st.stop()

                # 1.1 Extração determinística do Lucro/Prejuízo do Exercício
                resultado_confirmado = extrair_resultado_exercicio(texto_pdf)

                # 2. Envio para a Groq (GPT-OSS 120B) usando tags HTML para a cor amarela
                client = Groq(api_key=groq_api_key)

                bloco_valores_confirmados = ""
                if resultado_confirmado:
                    bloco_valores_confirmados = f"""
--- VALORES JÁ CONFIRMADOS (use exatamente estes, não recalcule nem reescreva com outros dígitos) ---
{resultado_confirmado}
--------------------------------------------------------------------------------------------------
"""

                prompt = f"""
Você é um auditor contábil sênior. Analise APENAS os dados explícitos contidos no texto abaixo.
O texto veio de OCR de um documento escaneado, então pode conter pequenos erros de leitura
(pontos e vírgulas trocados, algum caractere confundido, ou um dígito borrado). Quando houver
duas leituras da mesma página (leitura A e leitura B), compare as duas e escolha o valor mais
coerente com o contexto contábil.

REGRAS IMPORTANTES SOBRE VALORES:
- NÃO escreva "Não informado no documento" para um subtotal (ex: Imobilizado, Ativo Total) só
  porque o número ao lado do rótulo parece parcialmente corrompido pelo OCR. Nesses casos, use a
  identidade contábil pra conferir e, se possível, corrigir o valor: um subtotal deve ser igual à
  soma das contas que ficam logo abaixo dele, com a mesma indentação, até o próximo subtotal.
  Por exemplo: Ativo Não Circulante = Créditos e Valores + Investimento + Imobilizado + Bens
  Intangíveis. Se três dessas parcelas estiverem legíveis e o subtotal total também, calcule a
  quarta por subtração ao invés de responder "não informado".
- Só escreva "Não informado no documento" se o rótulo da conta realmente não aparecer em nenhuma
  lugar do texto (não porque um dígito ficou difícil de ler).
- Nunca invente uma conta ou valor que não tenha nenhuma base no texto.

REGRAS OBRIGATÓRIAS DE FORMATAÇÃO:
1. Dê sempre um espaço entre os títulos/textos e os valores numéricos.
2. DESTAQUE EM AMARELO TODOS OS VALORES NUMÉRICOS E DE MOEDA EM REAIS, inclusive dentro de parágrafos corridos (não só nos tópicos). Para destacar em amarelo, envolva OBRIGATORIAMENTE o valor na tag HTML: <span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>. Nunca escreva "R$" fora dessa tag.
3. Apresente os totais exatos que constam no balanço. NÃO tente inventar somas se o texto original do OCR já trouxe os totais.
{bloco_valores_confirmados}
--- TEXTO EXTRAÍDO DO PDF (via OCR) ---
{texto_pdf}
-----------------------------

Forneça um relatório em Markdown altamente estruturado contendo exatamente as seções abaixo:

### 1. 🏢 ESTRUTURA DO ATIVO
* **Ativo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Imobilizado (dentro do Não Circulante):** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Total:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>

### 2. 💳 ESTRUTURA DO PASSIVO E PATRIMÔNIO LÍQUIDO
* **Passivo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Exigível Não Circulante (Passivo Não Circulante):** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Patrimônio Líquido:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>

### 3. 📈 RESULTADO E PREJUÍZOS
* **Resultado do Exercício (Ano):** [Informe se teve Lucro ou Prejuízo com valor em <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>]
* **Prejuízos Acumulados:** [Informe o valor exato da conta Prejuízos Acumulados em <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>]

### 4. 💡 DIAGNÓSTICO FINANCEIRO E IDEIAS DE AÇÃO
* **Análise da Saúde Financeira:** [Resumo em 2 parágrafos destacando os principais valores em amarelo]
* **Ideias e Recomendações Práticas:** [3 a 5 sugestões práticas para a diretoria]
"""

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="openai/gpt-oss-120b",
                    temperature=0.1,
                    max_tokens=4096,
                )

                # 3. Exibição do relatório final.
                conteudo = response.choices[0].message.content
                conteudo_seguro = conteudo.replace("$", "\\$")

                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(conteudo_seguro, unsafe_allow_html=True)

                with st.expander("🔍 Ver texto bruto extraído do PDF (debug)"):
                    if resultado_confirmado:
                        st.markdown(f"**Valor confirmado por extração direta:** {resultado_confirmado}")
                    st.text(texto_pdf)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
