import streamlit as st
from groq import Groq
import pdfplumber

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
        with st.spinner("Lendo documento e extraindo indicadores contábeis..."):
            try:
                # 1. Extração robusta de texto e tabelas usando pdfplumber
                texto_pdf = ""
                with pdfplumber.open(pdf_file) as pdf:
                    for page in pdf.pages:
                        texto_pagina = page.extract_text()
                        if texto_pagina:
                            texto_pdf += texto_pagina + "\n"

                # Trava de segurança: se o texto extraído for muito curto
                if len(texto_pdf.strip()) < 50:
                    st.error("⚠️ Não foi possível extrair o texto deste PDF. O arquivo pode ser uma imagem digitalizada sem camada de OCR.")
                    st.stop()

                # 2. Conexão com a Groq
                client = Groq(api_key=groq_api_key)
                
                prompt = f"""
                Você é um auditor contábil sênior. Analise APENAS os dados reais contidos no texto contábil abaixo. 
                NÃO invente, estime ou use números fictícios. Se uma informação não constar no texto, diga expressamente que não foi informada.

                --- TEXTO EXTRAÍDO DO PDF ---
                {texto_pdf}
                -----------------------------

                Forneça um relatório completo em Markdown nos seguintes tópicos:
                1. 📈 **RESULTADO DO PERÍODO:** A empresa teve Lucro ou Prejuízo no ano? Qual o valor exato? (Busque pelo Lucro/Prejuízo Líquido do Exercício na DRE).
                2. 💳 **ANÁLISE DE DÍVIDAS E PASSIVOS:** Apresente os valores do Passivo Circulante, Passivo Não Circulante e total de empréstimos/financiamentos.
                3. ⚖️ **BALANÇO PATRIMONIAL:** Qual o valor do Patrimônio Líquido? Há prejuízos acumulados?
                4. 💡 **DIAGNÓSTICO E RECOMENDAÇÕES:** Resumo em 3 parágrafos para a diretoria sobre a saúde financeira observada nos dados reais.
                """

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                    temperature=0.1 # Temperatura baixa para evitar alucinações
                )

                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(response.choices[0].message.content)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")
