import streamlit as st
from groq import Groq
import pdfplumber
from pdf2image import convert_from_bytes
import base64
import io

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

INSTRUCOES_ANALISE = """
Você é um auditor contábil sênior. Analise APENAS os dados explícitos contidos no documento (texto ou imagem abaixo).

REGRAS OBRIGATÓRIAS DE FORMATAÇÃO:
1. Dê sempre um espaço entre os títulos/textos e os valores numéricos.
2. DESTAQUE EM AMARELO TODOS OS VALORES NUMÉRICOS E DE MOEDA EM REAIS. Para destacar em amarelo, envolva OBRIGATORIAMENTE o valor na tag HTML: <span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>.
3. Apresente os totais exatos que constam no balanço. NÃO tente inventar somas.

REGRAS OBRIGATÓRIAS DE PRECISÃO NUMÉRICA (MUITO IMPORTANTE):
4. Leia cada número dígito por dígito, com atenção total. NUNCA arredonde, corrija ou "adivinhe" um valor. Se um número tiver muitos dígitos, releia antes de responder.
5. Localize a ÚLTIMA ocorrência de cada rótulo (ex: "LUCRO LÍQUIDO DO EXERCÍCIO"), pois normalmente é o valor totalizado/oficial da linha final da demonstração.
6. Contas como "Imobilizado", "Investimentos" e "Intangível" costumam estar DENTRO do Ativo Não Circulante. Procure essas linhas no documento inteiro antes de dizer "não informado".
7. Só escreva "não informado" se o rótulo realmente não aparecer em NENHUM lugar do documento, mesmo de forma abreviada. Antes de concluir isso, procure variações como "ATIVO CIRC", "TOTAL DO ATIVO", "ATIVO TOTAL" etc.
8. Se um valor numérico tiver 6 dígitos ou mais, cite entre parênteses e aspas o trecho exato (linha) de onde ele foi retirado, logo após o valor, para conferência.

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


def imagem_para_base64(img) -> str:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e analisando (pode levar alguns segundos)..."):
            try:
                bytes_data = pdf_file.read()
                texto_pdf = ""

                # 1. Tentativa de extração direta via pdfplumber (PDF digital/nativo)
                with pdfplumber.open(pdf_file) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t:
                            texto_pdf += t + "\n"

                client = Groq(api_key=groq_api_key)

                if len(texto_pdf.strip()) >= 50:
                    # PDF nativo: manda o texto direto (mais barato e rápido)
                    prompt_completo = f"{INSTRUCOES_ANALISE}\n\n--- TEXTO EXTRAÍDO DO PDF ---\n{texto_pdf}\n-----------------------------"
                    response = client.chat.completions.create(
                        messages=[{"role": "user", "content": prompt_completo}],
                        model="openai/gpt-oss-120b",
                        temperature=0.1
                    )
                else:
                    # PDF escaneado/foto: manda a IMAGEM direto pro modelo com visão (sem OCR)
                    st.info("ℹ️ PDF escaneado/fotografado detectado. Analisando as páginas com IA de visão (sem depender de OCR)...")
                    images = convert_from_bytes(bytes_data, dpi=300)
                    images = images[:5]  # limite da API: máx. 5 imagens por requisição

                    conteudo = [{"type": "text", "text": INSTRUCOES_ANALISE}]
                    for img in images:
                        img_b64 = imagem_para_base64(img)
                        conteudo.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}"}
                        })

                    response = client.chat.completions.create(
                        messages=[{"role": "user", "content": conteudo}],
                        model="qwen/qwen3.6-27b",
                        temperature=0.1
                    )

                # 2. Exibição do relatório final (unsafe_allow_html=True permite o HTML amarelo)
                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(response.choices[0].message.content, unsafe_allow_html=True)

                # 3. Se foi extração por texto, mostra o texto bruto para conferência
                if len(texto_pdf.strip()) >= 50:
                    with st.expander("🔍 Ver texto bruto extraído do PDF (para conferência)"):
                        st.text(texto_pdf)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
