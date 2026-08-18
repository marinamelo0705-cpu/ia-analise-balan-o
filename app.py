import sys
import io
import streamlit as st
from groq import Groq
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np
import cv2

# Garante que a saída padrão use UTF-8, evitando erros de codificação com
# emojis/acentos quando o app roda em ambientes com locale ASCII (comum
# no Windows ou em alguns provedores de hospedagem restritos)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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


def ocr_dupla_leitura(img_tratada):
    """
    Roda o Tesseract em dois modos de segmentação de página (psm 6 e psm 4) e
    retorna as duas leituras concatenadas. Em tabelas contábeis, um modo às
    vezes lê corretamente um número que o outro erra — ter as duas versões
    no texto aumenta a chance da IA identificar o valor correto (reduz o
    risco de troca de dígito, ex: 7 lido como 1).
    """
    texto_psm6 = pytesseract.image_to_string(img_tratada, lang="por", config='--oem 3 --psm 6')
    texto_psm4 = pytesseract.image_to_string(img_tratada, lang="por", config='--oem 3 --psm 4')
    return (
        "\n[Leitura OCR - modo A]\n" + texto_psm6 +
        "\n[Leitura OCR - modo B]\n" + texto_psm4
    )


if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e extraindo dados (pode levar alguns segundos se for escaneado)..."):
            try:
                bytes_data = pdf_file.read()
                texto_pdf = ""

                # 1. Extração página a página: cada página é avaliada individualmente.
                #    Isso evita o problema de PDFs "mistos" (algumas páginas com texto
                #    digital e outras escaneadas) — se o documento inteiro tivesse texto
                #    suficiente no total, páginas escaneadas isoladas eram ignoradas.
                with pdfplumber.open(io.BytesIO(bytes_data)) as pdf:
                    for i, page in enumerate(pdf.pages, start=1):
                        t = page.extract_text()
                        if t and len(t.strip()) >= 20:
                            texto_pdf += t + "\n"
                        else:
                            # Página sem texto digital suficiente: escaneada/imagem.
                            # Renderiza em alta resolução (400 dpi), pré-processa
                            # e roda o OCR em dois modos diferentes para reduzir
                            # erro de leitura de dígitos.
                            st.info(f"ℹ️ Página {i} parece escaneada/fotografada. Aplicando OCR com tratamento de imagem...")
                            imagens_pagina = convert_from_bytes(bytes_data, dpi=400, first_page=i, last_page=i)
                            for img in imagens_pagina:
                                img_tratada = preparar_imagem_para_ocr(img)
                                texto_pdf += ocr_dupla_leitura(img_tratada) + "\n"

                # Trava de segurança final
                if len(texto_pdf.strip()) < 30:
                    st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
                    st.stop()

                # 2. Envio para a Groq (GPT-OSS 120B) usando tags HTML para a cor amarela
                client = Groq(api_key=groq_api_key)

                prompt = f"""
Você é um auditor contábil sênior. Analise APENAS os dados explícitos contidos no texto abaixo.
O texto pode conter DUAS leituras de OCR da mesma página (modo A e modo B) — quando os dois
modos divergirem num número, compare os dois e escolha o valor que fizer mais sentido
contábil (ex: mantendo Ativo = Passivo + Patrimônio Líquido), mas NUNCA invente uma conta
ou valor que não exista em nenhuma das leituras.

REGRAS OBRIGATÓRIAS DE FORMATAÇÃO:
1. Dê sempre um espaço entre os títulos/textos e os valores numéricos.
2. DESTAQUE EM AMARELO TODOS OS VALORES NUMÉRICOS E DE MOEDA EM REAIS. Para destacar em amarelo, envolva OBRIGATORIAMENTE o valor na tag HTML: <span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>. Nunca escreva "R$" fora dessa tag.
3. Apresente os totais exatos que constam no balanço. NÃO tente inventar somas se o texto original já trouxe os totais. Se um valor realmente não constar no texto, escreva "Não informado no documento" ao invés de inventar.
4. NÃO escreva parágrafos, diagnóstico, análise ou recomendações. A resposta deve conter APENAS a lista de itens abaixo, nada além disso.
5. Calcule o Capital de Giro Líquido como: Ativo Circulante − Passivo Circulante. Mostre a conta feita entre parênteses.
6. Para o resultado do período, procure a linha "LUCRO LÍQUIDO DO EXERCÍCIO" ou "PREJUÍZO DO EXERCÍCIO" (é sempre uma OU outra, nunca as duas) em AMBAS as leituras de OCR disponíveis, compare os dígitos entre elas e escolha a versão mais consistente. No rótulo da resposta, escreva apenas a que realmente aparecer: "Lucro do Exercício" ou "Prejuízo do Exercício". NUNCA escreva as duas opções juntas.
7. Releia cada valor com 6 dígitos ou mais comparando as duas leituras de OCR antes de responder, para não trocar nenhum dígito (ex: não confundir 7 com 1, 9 com 4, 3 com 8).

--- TEXTO EXTRAÍDO DO PDF ---
{texto_pdf}
-----------------------------

Responda EXATAMENTE neste formato, preenchendo os valores em Markdown (o rótulo do item de resultado deve ser "Lucro do Exercício" OU "Prejuízo do Exercício", nunca os dois juntos):

### 📊 RESULTADO DA ANÁLISE

* **Ativo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Total:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Passivo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Exigível Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Patrimônio Líquido:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Capital de Giro Líquido:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span> (Ativo Circulante − Passivo Circulante)
* **[Lucro do Exercício OU Prejuízo do Exercício — escolha um]:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Prejuízos Acumulados:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
"""

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="openai/gpt-oss-120b",
                    temperature=0.1,
                    max_tokens=4096,
                )

                # 3. Exibição do relatório final.
                # Escapa "$" soltos para o Streamlit não confundir com LaTeX (\$...\$),
                # o que causava a renderização quebrada ("R`" no lugar de "R$").
                conteudo = response.choices[0].message.content
                conteudo_seguro = conteudo.replace("$", "\\$")

                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(conteudo_seguro, unsafe_allow_html=True)

                with st.expander("🔍 Ver texto bruto extraído do PDF (debug)"):
                    st.text(texto_pdf)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
