import io
import base64
import streamlit as st
from groq import Groq
import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image

# Configuração da página
st.set_page_config(page_title="Analisador Contábil Completo", page_icon="📊", layout="wide")
st.title("📊 Analisador Inteligente de Balanços e DRE")
st.markdown("Suba o arquivo PDF contábil da empresa para extrair Ativo, Passivo, Patrimônio Líquido, Resultado e Capital de Giro.")

# Busca a chave salva nos Secrets do Streamlit ou pede na barra lateral
if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
    st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

# Upload do PDF
pdf_file = st.file_uploader("Arraste e solte o PDF do Balanço/DRE aqui", type=["pdf"])

INSTRUCOES = """
Você é um auditor contábil sênior. Analise APENAS os dados explícitos contidos no documento
fornecido (texto e/ou imagens das páginas abaixo). Algumas páginas podem vir como imagem —
leia os números diretamente na imagem, com atenção total a cada dígito.

REGRAS OBRIGATÓRIAS DE FORMATAÇÃO:
1. Dê sempre um espaço entre os títulos/textos e os valores numéricos.
2. DESTAQUE EM AMARELO TODOS OS VALORES NUMÉRICOS E DE MOEDA EM REAIS. Para destacar em amarelo, envolva OBRIGATORIAMENTE o valor na tag HTML: <span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>. Nunca escreva "R$" fora dessa tag.
3. Apresente os totais exatos que constam no documento. NÃO tente inventar somas. Se um valor realmente não constar em nenhum lugar do documento, escreva "Não informado no documento" ao invés de inventar.
4. NÃO escreva parágrafos, diagnóstico, análise ou recomendações. A resposta deve conter APENAS a lista de itens abaixo, nada além disso.

REGRAS OBRIGATÓRIAS DE PRECISÃO NUMÉRICA:
5. Leia cada número dígito por dígito, com atenção máxima, antes de responder. Números com 6 dígitos ou mais são os que mais erram — releia cada um deles ao menos duas vezes mentalmente antes de escrever a resposta final. NUNCA troque um dígito por outro parecido (7↔1, 9↔4, 3↔8, 6↔0).
6. Calcule o Capital de Giro Líquido como: Ativo Circulante − Passivo Circulante. Mostre a conta feita entre parênteses.
7. Para o resultado do período, procure a linha "LUCRO LÍQUIDO DO EXERCÍCIO" ou "PREJUÍZO DO EXERCÍCIO" (é sempre uma OU outra, nunca as duas). No rótulo da resposta, escreva apenas a que realmente aparecer: "Lucro do Exercício" ou "Prejuízo do Exercício". NUNCA escreva as duas opções juntas.
8. O "Imobilizado" É uma conta que compõe o Ativo Não Circulante (junto com Investimentos e Intangível) — não fica isolado no topo do documento. Procure no documento inteiro por linhas como "IMOBILIZADO", "IMOBILIZADO LÍQUIDO" ou "ATIVO IMOBILIZADO". Só escreva "Não informado no documento" se, mesmo assim, não encontrar nenhuma linha com esse termo.

Responda EXATAMENTE neste formato, preenchendo os valores em Markdown (o rótulo do item de resultado deve ser "Lucro do Exercício" OU "Prejuízo do Exercício", nunca os dois juntos):

### 📊 RESULTADO DA ANÁLISE

* **Ativo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Total:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Imobilizado:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Passivo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Exigível Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Patrimônio Líquido:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Capital de Giro Líquido:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span> (Ativo Circulante − Passivo Circulante)
* **[Lucro do Exercício OU Prejuízo do Exercício — escolha um]:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Prejuízos Acumulados:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
"""


def imagem_para_base64(img: Image.Image, largura_max: int = 1800, qualidade: int = 90) -> str:
    """Redimensiona/comprime a imagem em JPEG antes de mandar pra API,
    evitando o erro 413 (Request Entity Too Large) sem perder legibilidade."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > largura_max:
        proporcao = largura_max / img.width
        img = img.resize((largura_max, int(img.height * proporcao)))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=qualidade, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e analisando (pode levar alguns segundos)..."):
            try:
                bytes_data = pdf_file.read()
                texto_pdf = ""
                paginas_escaneadas = []  # números das páginas que precisam ser lidas como imagem

                # 1. Avalia página a página: texto nativo quando existir, senão marca
                #    a página para ser lida como imagem pela IA de visão (sem OCR).
                with pdfplumber.open(io.BytesIO(bytes_data)) as pdf:
                    for i, page in enumerate(pdf.pages, start=1):
                        t = page.extract_text()
                        if t and len(t.strip()) >= 20:
                            texto_pdf += f"\n--- Página {i} (texto digital) ---\n{t}\n"
                        else:
                            paginas_escaneadas.append(i)

                imagens_b64 = []
                if paginas_escaneadas:
                    st.info(f"ℹ️ {len(paginas_escaneadas)} página(s) escaneada(s)/fotografada(s) detectada(s). Serão analisadas diretamente por IA de visão, sem OCR.")
                    if len(paginas_escaneadas) > 3:
                        st.warning(f"⚠️ O modelo de visão só processa até 3 imagens por análise. Apenas as páginas {paginas_escaneadas[:3]} serão lidas; as demais ({paginas_escaneadas[3:]}) serão ignoradas nesta análise.")
                    # Limite da API: no máximo 3 imagens por requisição
                    for i in paginas_escaneadas[:3]:
                        pagina_imgs = convert_from_bytes(bytes_data, dpi=250, first_page=i, last_page=i)
                        for img in pagina_imgs:
                            imagens_b64.append(imagem_para_base64(img))

                if not texto_pdf.strip() and not imagens_b64:
                    st.error("⚠️ Não foi possível ler o documento. Certifique-se de que o PDF esteja legível.")
                    st.stop()

                # 2. Monta o conteúdo da mensagem: texto das páginas digitais + imagens das escaneadas
                partes_conteudo = [{"type": "text", "text": INSTRUCOES}]
                if texto_pdf.strip():
                    partes_conteudo.append({
                        "type": "text",
                        "text": f"--- TEXTO EXTRAÍDO DAS PÁGINAS DIGITAIS DO PDF ---\n{texto_pdf}\n-----------------------------"
                    })
                for img_b64 in imagens_b64:
                    partes_conteudo.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                    })

                # 3. Usa modelo com visão se houver imagens; senão, modelo de texto (mais rápido)
                modelo = "qwen/qwen3.6-27b" if imagens_b64 else "openai/gpt-oss-120b"

                client = Groq(api_key=groq_api_key)
                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": partes_conteudo}],
                    model=modelo,
                    temperature=0.1,
                    max_tokens=4096,
                )

                # 4. Exibição do relatório final.
                # Escapa "$" soltos para o Streamlit não confundir com LaTeX.
                conteudo = response.choices[0].message.content
                conteudo_seguro = conteudo.replace("$", "\\$")

                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(conteudo_seguro, unsafe_allow_html=True)

                if texto_pdf.strip():
                    with st.expander("🔍 Ver texto bruto extraído das páginas digitais (debug)"):
                        st.text(texto_pdf)
                if imagens_b64:
                    st.caption(f"📄 {len(imagens_b64)} página(s) foram lidas diretamente como imagem pela IA (sem OCR).")

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
