import streamlit as st
from groq import Groq
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image

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

if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e extraindo dados (pode levar alguns segundos se for escaneado)..."):
            try:
                bytes_data = pdf_file.read()
                texto_pdf = ""

                # 1. Tentativa de extração direta via pdfplumber
                with pdfplumber.open(pdf_file) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t:
                            texto_pdf += t + "\n"

                # 2. Se o PDF for escaneado, aplica OCR otimizado
                if len(texto_pdf.strip()) < 50:
                    st.info("ℹ️ PDF escaneado/fotografado detectado. Executando leitura via OCR com tratamento de imagem...")
                    images = convert_from_bytes(bytes_data, dpi=300)
                    texto_pdf = ""
                    for img in images:
                        img_gray = img.convert('L')
                        texto_ocr = pytesseract.image_to_string(img_gray, lang="por", config='--psm 6')
                        texto_pdf += texto_ocr + "\n"

                # Trava de segurança final
                if len(texto_pdf.strip()) < 30:
                    st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
                    st.stop()

                # 3. Envio para a Groq usando tags HTML para a cor amarela
                client = Groq(api_key=groq_api_key)

                prompt = f"""
Você é um auditor contábil sênior. Analise APENAS os dados explícitos contidos no texto abaixo.

REGRAS OBRIGATÓRIAS DE FORMATAÇÃO:
1. Dê sempre um espaço entre os títulos/textos e os valores numéricos.
2. DESTAQUE EM AMARELO TODOS OS VALORES NUMÉRICOS E DE MOEDA EM REAIS. Para destacar em amarelo, envolva OBRIGATORIAMENTE o valor na tag HTML: <span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>.
3. Apresente os totais exatos que constam no balanço. NÃO tente inventar somas se o texto original do OCR já trouxe os totais.

REGRAS OBRIGATÓRIAS DE PRECISÃO NUMÉRICA (MUITO IMPORTANTE):
4. NUNCA arredonde, corrija ou "adivinhe" um valor numérico. Copie os dígitos EXATAMENTE como aparecem no texto extraído, incluindo pontos e vírgulas.
5. Antes de responder, releia o texto extraído e localize a ÚLTIMA ocorrência de cada rótulo (ex: "LUCRO LÍQUIDO DO EXERCÍCIO", "PREJUÍZO DO EXERCÍCIO"), pois normalmente é o valor totalizado/oficial da linha final da demonstração.
6. Contas como "Imobilizado", "Investimentos" e "Intangível" costumam estar DENTRO do Ativo Não Circulante, mesmo que não apareçam isoladas no topo do documento. Procure essas linhas no corpo do texto inteiro antes de dizer "não informado".
7. Se um valor numérico tiver 6 dígitos ou mais, cite entre parênteses e aspas o trecho exato (linha) de onde ele foi retirado do texto extraído, logo após o valor, para conferência. Exemplo: <span style="color: #F1C40F; font-weight: bold;">R$ 793.376,08</span> ("LUCRO LÍQUIDO DO EXERCÍCIO ... 793.376,08")

--- TEXTO EXTRAÍDO DO PDF ---
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
                    temperature=0.1
                )

                # 4. Exibição do relatório final (unsafe_allow_html=True permite o HTML amarelo)
                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(response.choices[0].message.content, unsafe_allow_html=True)

                # 5. Texto bruto extraído, para conferência manual dos valores
                with st.expander("🔍 Ver texto bruto extraído do PDF (para conferência)"):
                    st.text(texto_pdf)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
