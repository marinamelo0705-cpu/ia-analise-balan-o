import sys
import io
import streamlit as st
from groq import Groq
import pdfplumber
import pytesseract
from pytesseract import Output
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np
import cv2

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

st.set_page_config(page_title="Analisador Contábil Completo", page_icon="📊", layout="wide")
st.title("📊 Analisador Inteligente de Balanços e DRE")
st.markdown("Suba o arquivo PDF contábil da empresa para extrair Ativo, Passivo, Patrimônio Líquido, Resultado e Capital de Giro.")

if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
    st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

pdf_file = st.file_uploader("Arraste e solte o PDF do Balanço/DRE aqui", type=["pdf"])


def preparar_imagem_para_ocr(imagem_pil):
    arr = np.array(imagem_pil.convert('L'))
    arr = cv2.medianBlur(arr, 3)
    _, arr_bin = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(arr_bin)


def reler_linha_resultado(imagem_pil):
    """
    Localiza a linha "LUCRO/PREJUÍZO ... EXERCÍCIO" na imagem original,
    recorta SÓ essa linha, amplia a resolução em 3x e refaz o OCR isolado
    nela. Exige "LUCRO"/"PREJU" E "EXERC" na mesma linha para não confundir
    com outras linhas parecidas (ex: "Lucro Bruto").
    """
    try:
        arr_cinza = np.array(imagem_pil.convert('L'))
        dados = pytesseract.image_to_data(
            arr_cinza, lang="por", config='--oem 3 --psm 6', output_type=Output.DICT
        )

        palavras_resultado = ["LUCRO", "PREJU"]
        linha_alvo = None
        n = len(dados['text'])

        linhas = {}
        for idx in range(n):
            palavra = dados['text'][idx].strip()
            if not palavra:
                continue
            chave = (dados['block_num'][idx], dados['par_num'][idx], dados['line_num'][idx])
            linhas.setdefault(chave, []).append(idx)

        for chave, indices in linhas.items():
            texto_linha_completo = " ".join(dados['text'][idx] for idx in indices).upper()
            tem_resultado = any(p in texto_linha_completo for p in palavras_resultado)
            tem_exercicio = "EXERC" in texto_linha_completo
            if tem_resultado and tem_exercicio:
                linha_alvo = chave
                break

        if linha_alvo is None:
            return None

        xs, ys_topo, ys_base = [], [], []
        for idx in linhas[linha_alvo]:
            x, y, w, h = dados['left'][idx], dados['top'][idx], dados['width'][idx], dados['height'][idx]
            xs.append(x)
            xs.append(x + w)
            ys_topo.append(y)
            ys_base.append(y + h)

        if not xs:
            return None

        altura_img, largura_img = arr_cinza.shape
        y0 = max(0, min(ys_topo) - 12)
        y1 = min(altura_img, max(ys_base) + 12)
        x0 = max(0, min(xs) - 20)
        x1 = min(largura_img, max(xs) + 400)

        recorte = arr_cinza[y0:y1, x0:x1]
        if recorte.size == 0:
            return None

        recorte_ampliado = cv2.resize(recorte, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        recorte_ampliado = cv2.medianBlur(recorte_ampliado, 3)
        _, recorte_bin = cv2.threshold(recorte_ampliado, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        texto_linha = pytesseract.image_to_string(recorte_bin, lang="por", config='--oem 3 --psm 7').strip()
        return texto_linha if texto_linha else None
    except Exception:
        return None


if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e extraindo dados (pode levar alguns segundos se for escaneado)..."):
            try:
                bytes_data = pdf_file.read()
                texto_pdf = ""
                leitura_precisa_resultado = None

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
                                texto_ocr = pytesseract.image_to_string(img_tratada, lang="por", config='--oem 3 --psm 6')
                                texto_pdf += texto_ocr + "\n"

                                # Roda em todas as páginas escaneadas, pois a linha do
                                # resultado pode estar em qualquer uma delas
                                leitura_desta_pagina = reler_linha_resultado(img)
                                if leitura_desta_pagina:
                                    leitura_precisa_resultado = leitura_desta_pagina

                if len(texto_pdf.strip()) < 30:
                    st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
                    st.stop()

                bloco_leitura_precisa = ""
                if leitura_precisa_resultado:
                    bloco_leitura_precisa = f"""
--- LEITURA DE ALTA PRECISÃO DA LINHA DE RESULTADO (recorte ampliado 3x, mais confiável que o texto geral acima para este valor específico) ---
{leitura_precisa_resultado}
-----------------------------
"""

                client = Groq(api_key=groq_api_key)

                prompt = f"""
Você é um auditor contábil sênior. Analise APENAS os dados explícitos contidos no texto abaixo.
O texto veio de OCR de um documento escaneado, então pode conter pequenos erros de leitura
(ex: pontos e vírgulas trocados, algum caractere confundido). Use o contexto contábil e as
regras conhecidas de balanço (Ativo = Passivo + Patrimônio Líquido) para interpretar os
valores mais prováveis quando um número parecer inconsistente, mas NUNCA invente uma conta
ou valor que não exista no texto.

REGRAS OBRIGATÓRIAS DE FORMATAÇÃO:
1. Dê sempre um espaço entre os títulos/textos e os valores numéricos.
2. DESTAQUE EM AMARELO TODOS OS VALORES NUMÉRICOS E DE MOEDA EM REAIS. Para destacar em amarelo, envolva OBRIGATORIAMENTE o valor na tag HTML: <span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span>. Nunca escreva "R$" fora dessa tag.
3. Apresente os totais exatos que constam no balanço. NÃO tente inventar somas se o texto original do OCR já trouxe os totais. Se um valor realmente não constar no texto, escreva "Não informado no documento" ao invés de inventar.
4. NÃO escreva parágrafos, diagnóstico, análise ou recomendações. A resposta deve conter APENAS a lista de itens abaixo, nada além disso.
5. Calcule o Capital de Giro Líquido como: Ativo Circulante − Passivo Circulante. Mostre a conta feita entre parênteses.
6. Para "Resultado do Exercício", identifique se é Lucro ou Prejuízo do exercício (linha "LUCRO LÍQUIDO DO EXERCÍCIO" ou "PREJUÍZO DO EXERCÍCIO") e rotule corretamente. Existe abaixo uma seção "LEITURA DE ALTA PRECISÃO DA LINHA DE RESULTADO" (se presente) — ela é um recorte ampliado 3x feito especificamente nessa linha e é MAIS CONFIÁVEL que o texto geral para esse valor.
7. CONFERÊNCIA OBRIGATÓRIA DO RESULTADO: se o texto contiver as linhas "Resultado Antes do IR" (ou "Resultado Antes do IR e CSLL"), "Provisões" (do IR/CSLL, geralmente um valor negativo/entre parênteses logo após o Resultado Antes do IR) e "Participações e Contribuições" (também negativo/entre parênteses), CALCULE o Lucro/Prejuízo Líquido do Exercício como: Resultado Antes do IR − Provisões − Participações e Contribuições (tratando os valores entre parênteses como negativos/subtraindo-os). Se esse cálculo divergir da linha "LUCRO LÍQUIDO DO EXERCÍCIO" lida diretamente (por exemplo, diferença nos primeiros dígitos, sinal de possível erro de leitura/mancha no documento), CONFIE no valor calculado, pois ele é derivado de números que aparecem de forma mais nítida em outras linhas do documento.

--- TEXTO EXTRAÍDO DO PDF ---
{texto_pdf}
-----------------------------
{bloco_leitura_precisa}

Responda EXATAMENTE neste formato, preenchendo os valores em Markdown:

### 📊 RESULTADO DA ANÁLISE

* **Ativo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Total:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Imobilizado:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Passivo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Exigível Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Patrimônio Líquido:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Capital de Giro Líquido:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span> (Ativo Circulante − Passivo Circulante)
* **Resultado do Exercício (Lucro):** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Prejuízos Acumulados:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
"""

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="openai/gpt-oss-120b",
                    temperature=0.1,
                    max_tokens=4096,
                )

                conteudo = response.choices[0].message.content
                conteudo_seguro = conteudo.replace("$", "\\$")

                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(conteudo_seguro, unsafe_allow_html=True)

                with st.expander("🔍 Ver texto bruto extraído do PDF (debug)"):
                    st.text(texto_pdf)
                    if leitura_precisa_resultado:
                        st.markdown("**Leitura de alta precisão da linha de resultado:**")
                        st.text(leitura_precisa_resultado)

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
