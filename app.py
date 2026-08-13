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
                    images = convert_from_bytes(bytes_data)
                    texto_pdf = ""
                    for img in images:
                        img_gray = img.convert('L')
                        texto_ocr = pytesseract.image_to_string(img_gray, lang="por", config='--psm 6')
                        texto_pdf += texto_ocr + "\n"

                # Trava de segurança final
                if len(texto_pdf.strip()) < 30:
                    st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
                    st.stop()

                # 3. Envio para a Groq (Llama 3.3) com os novos tópicos detalhados
                client = Groq(api_key=groq_api_key)
                
                prompt = f"""
                Você é um auditor contábil sênior. Analise APENAS os dados contidos no texto extraído do documento abaixo.
                Atenção: O texto foi extraído via OCR. Preste atenção aos dígitos de números contábeis.

                --- TEXTO EXTRAÍDO DO PDF ---
                {texto_pdf}
                -----------------------------

                Forneça um relatório em Markdown altamente estruturado contendo exatamente as seções abaixo:

                ### 1. 🏢 ESTRUTURA DO ATIVO
                * **Ativo Circulante:** [Valor exato em R$]
                * **Ativo Não Circulante:** [Valor exato em R$]
                * **Imobilizado (dentro do Não Circulante):** [Valor exato em R$, se informado]
                * **Ativo Total:** [Valor exato em R$]

                ### 2. 💳 ESTRUTURA DO PASSIVO E PATRIMÔNIO LÍQUIDO
                * **Passivo Circulante:** [Valor exato em R$]
                * **Exigível Não Circulante (Passivo Não Circulante):** [Valor exato em R$]
                * **Patrimônio Líquido:** [Valor exato em R$]

                ### 3. 📈 RESULTADO E PREJUÍZOS
                * **Resultado do Exercício (Ano):** [Informe se teve Lucro ou Prejuízo no ano e o valor exato em R$]
                * **Prejuízos Acumulados:** [Informe se há saldo de Prejuízos Acumulados de anos anteriores no Patrimônio Líquido e o valor exato em R$]

                ### 4. 💡 DIAGNÓSTICO FINANCEIRO E IDEIAS DE AÇÃO
                * **Análise da Saúde Financeira:** [Resumo em 2 parágrafos sobre a relação entre o Ativo, Passivo e Patrimônio Líquido]
                * **Ideias e Recomendações Práticas:** [Forneça 3 a 5 sugestões práticas/ideias estratégicas para os diretores melhorarem a gestão financeira, reduzirem passivos e mitigarem os prejuízos acumulados]
                """

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                    temperature=0.1
                )

                # 4. Exibição do relatório final
                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(response.choices[0].message.content)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
