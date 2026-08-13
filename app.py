import streamlit as st
from groq import Groq
import pypdf

# Configuração da página
st.set_page_config(page_title="Analisador Contábil", page_icon="📊", layout="wide")

st.title("📊 Analisador Inteligente de Balanços e DRE")
st.markdown("Suba o arquivo PDF contábil da empresa para gerar o diagnóstico de lucros, prejuízos e dívidas.")

# Input seguro para a chave da Groq
groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

# Upload do PDF
pdf_file = st.file_uploader("Arraste e solte o PDF do Balanço/DRE aqui", type=["pdf"])

if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e extraindo indicadores contábeis..."):
            try:
                # 1. Extração de texto do PDF
                reader = pypdf.PdfReader(pdf_file)
                texto_pdf = ""
                for page in reader.pages:
                    texto_pdf += page.extract_text() or ""

                # 2. Conexão com o Llama 3 via Groq
                client = Groq(api_key=groq_api_key)
                
                prompt = f"""
                Você é um auditor contábil sênior. Analise o documento contábil abaixo:

                {texto_pdf}

                Forneça um relatório completo em Markdown estruturado nos seguintes tópicos:
                1. 📈 **RESULTADO DO PERÍODO:** A empresa teve Lucro ou Prejuízo no ano? Qual o valor exato?
                2. 💳 **ANÁLISE DE DÍVIDAS E PASSIVOS:** Apresente os valores do Passivo Circulante (curto prazo), Não Circulante (longo prazo) e total de empréstimos/financiamentos.
                3. ⚖️ **BALANÇO PATRIMONIAL:** O Patrimônio Líquido é positivo? Há prejuízos acumulados de anos anteriores?
                4. 💡 **DIAGNÓSTICO E RECOMENDAÇÕES:** Faça um resumo em 3 parágrafos para os diretores sobre os riscos e a saúde financeira atual da empresa.
                """

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                )

                # 3. Exibição do relatório
                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(response.choices[0].message.content)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq na barra lateral para continuar.")
