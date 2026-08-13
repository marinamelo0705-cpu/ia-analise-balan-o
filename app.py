import streamlit as st
from groq import Groq
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image

st.set_page_config(page_title="Analisador Contábil", page_icon="📊", layout="wide")

st.title("📊 Analisador Inteligente de Balanços e DRE")
st.markdown("Suba o arquivo PDF contábil da empresa para gerar o diagnóstico de lucros, prejuízos e dívidas.")

if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")

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

                # 2. Se o PDF for uma imagem escaneada, aplica OCR (Reconhecimento Óptico)
                if len(texto_pdf.strip()) < 50:
                    st.info("ℹ️ PDF escaneado/fotografado detectado. Executando leitura via OCR...")
                    images = convert_from_bytes(bytes_data)
                    texto_pdf = ""
                    for img in images:
                        # Extrai o texto da imagem em português
                        texto_ocr = pytesseract.image_to_string(img, lang="por")
                        texto_pdf += texto_ocr + "\n"

                # Trava de segurança final
                if len(texto_pdf.strip()) < 30:
                    st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
                    st.stop()

                # 3. Envio para a Groq (Llama 3.3)
                client = Groq(api_key=groq_api_key)
                
                prompt = f"""
                Você é um auditor contábil sênior. Analise APENAS os dados reais contidos no texto extraído do documento abaixo.
                NÃO invente, estime ou use números fictícios.

                --- TEXTO EXTRAÍDO DO PDF ---
                {texto_pdf}
                -----------------------------

                Forneça um relatório em Markdown estruturado nos tópicos:
                1. 📈 **RESULTADO DO PERÍODO:** A empresa teve Lucro ou Prejuízo no ano? Qual o valor exato? (Busque pelo Lucro/Prejuízo Líquido do Exercício na DRE).
                2. 💳 **ANÁLISE DE DÍVIDAS E PASSIVOS:** Apresente os valores do Passivo Circulante, Passivo Não Circulante e Empréstimos/Financiamentos.
                3. ⚖️ **BALANÇO PATRIMONIAL:** Qual o valor do Patrimônio Líquido? Há prejuízos acumulados?
                4. 💡 **DIAGNÓSTICO E RECOMENDAÇÕES:** Resumo em 3 parágrafos para a diretoria com base nos dados reais lidos.
                """

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                    temperature=0.1
                )

                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(response.choices[0].message.content)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")
