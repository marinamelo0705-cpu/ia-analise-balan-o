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

incluir_analise_descritiva = st.sidebar.checkbox(
    "💬 Incluir análise descritiva e sugestões",
    value=True,
    help="Além dos indicadores calculados, gera um texto explicando os principais gastos, "
         "uma estimativa de gastos futuros e sugestões de gestão financeira.",
)


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


def reler_linha_resultado(imagem_pil):
    """
    Localiza a linha "LUCRO/PREJUÍZO DO EXERCÍCIO" na imagem original,
    recorta SÓ essa linha, amplia a resolução em 3x e refaz o OCR isolado
    nela. Essa linha costuma ter fonte diferente/negrito nos balanços, o
    que confunde o Tesseract quando lida junto com o resto da página —
    isolar e ampliar aumenta muito a precisão só nesse valor crítico, sem
    duplicar o OCR da página inteira (evita estourar o limite de tokens).
    """
    try:
        arr_cinza = np.array(imagem_pil.convert('L'))
        dados = pytesseract.image_to_data(
            arr_cinza, lang="por", config='--oem 3 --psm 6', output_type=Output.DICT
        )

        palavras_resultado = ["LUCRO", "PREJU"]
        linha_alvo = None
        n = len(dados['text'])

        # Agrupa palavras por linha (block/par/line) para poder checar o
        # conteúdo completo de cada linha, não só palavra por palavra
        linhas = {}
        for idx in range(n):
            palavra = dados['text'][idx].strip()
            if not palavra:
                continue
            chave = (dados['block_num'][idx], dados['par_num'][idx], dados['line_num'][idx])
            linhas.setdefault(chave, []).append(idx)

        # Só considera a linha certa: precisa ter "LUCRO"/"PREJU" E "EXERC"
        # juntos (isso elimina falsos positivos como "Lucro Bruto", que não
        # tem "Exercício" na mesma linha)
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
        for idx in range(n):
            chave = (dados['block_num'][idx], dados['par_num'][idx], dados['line_num'][idx])
            if chave == linha_alvo and dados['text'][idx].strip():
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
        x1 = min(largura_img, max(xs) + 400)  # margem extra à direita para garantir o valor numérico completo

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
                            # Renderiza em alta resolução (400 dpi) e pré-processa
                            # antes do OCR — resolve o problema de marcas d'água /
                            # texturas de fundo que atrapalham a leitura dos números.
                            st.info(f"ℹ️ Página {i} parece escaneada/fotografada. Aplicando OCR com tratamento de imagem...")
                            imagens_pagina = convert_from_bytes(bytes_data, dpi=400, first_page=i, last_page=i)
                            for img in imagens_pagina:
                                img_tratada = preparar_imagem_para_ocr(img)
                                texto_ocr = pytesseract.image_to_string(img_tratada, lang="por", config='--oem 3 --psm 6')
                                texto_pdf += texto_ocr + "\n"

                                # Releitura isolada e ampliada da linha de resultado (Lucro/Prejuízo)
                                # Roda em todas as páginas escaneadas, pois a linha do resultado
                                # pode estar em qualquer uma delas (ex: página 4 de 4)
                                leitura_desta_pagina = reler_linha_resultado(img)
                                if leitura_desta_pagina:
                                    leitura_precisa_resultado = leitura_desta_pagina

                # Trava de segurança final
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

                # 2. Envio para a Groq (GPT-OSS 120B) usando tags HTML para a cor amarela
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
6. Para "Resultado do Exercício", identifique se é Lucro ou Prejuízo do exercício e rotule corretamente como "Lucro do Exercício" ou "Prejuízo do Exercício". A linha "LUCRO LÍQUIDO DO EXERCÍCIO" costuma vir borrada/manchada no scan — se ela tiver caracteres estranhos, letras misturadas com números, ou não bater com o cálculo da regra 7 abaixo, IGNORE a leitura direta e use exclusivamente o valor calculado pela regra 7.
7. CÁLCULO OBRIGATÓRIO E PRIORITÁRIO DO RESULTADO: se o texto contiver "Resultado Antes do IR" (ou "Resultado Antes do IR e CSLL"), "Provisões" (valor entre parênteses logo após) e "Participações e Contribuições" (também entre parênteses), CALCULE: Resultado Antes do IR − Provisões − Participações e Contribuições (valores entre parênteses são negativos). Use esse valor calculado como o "Resultado do Exercício" da resposta, SEMPRE que essas três linhas estiverem disponíveis — não use a leitura direta da linha "LUCRO LÍQUIDO DO EXERCÍCIO" nesse caso, pois ela é a mais sujeita a erro de OCR no documento.
8. CÁLCULO DO ATIVO NÃO CIRCULANTE: se não houver uma linha explícita "ATIVO NÃO CIRCULANTE" com um valor total no texto, mas houver "Ativo Total" e "Ativo Circulante", CALCULE: Ativo Total − Ativo Circulante. Use esse valor calculado em vez de escrever "Não informado no documento".

--- TEXTO EXTRAÍDO DO PDF ---
{texto_pdf}
-----------------------------

Responda EXATAMENTE neste formato, preenchendo os valores em Markdown:

### 📊 RESULTADO DA ANÁLISE

* **Ativo Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Não Circulante:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
* **Ativo Total:** <span style="color: #F1C40F; font-weight: bold;">R$ ...</span>
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
                    if leitura_precisa_resultado:
                        st.markdown("**Leitura de alta precisão da linha de resultado:**")
                        st.text(leitura_precisa_resultado)

                # 4. Análise descritiva: um segundo chamado à IA, separado do primeiro,
                #    para não contaminar a extração estritamente numérica (regra 4 do
                #    prompt acima) com texto corrido. Recebe os indicadores já calculados
                #    (mais confiáveis que o texto bruto) e também o texto extraído do PDF,
                #    que costuma trazer o detalhamento de despesas do DRE.
                if incluir_analise_descritiva:
                    with st.spinner("Gerando análise descritiva e sugestões..."):
                        prompt_analise = f"""
Você é um consultor financeiro e contábil experiente. Com base nos indicadores já calculados
abaixo e no texto original extraído do documento (que pode conter o detalhamento de despesas/
custos do DRE), escreva uma análise em português, em prosa corrida (sem tabelas), organizada
em três partes com exatamente estes subtítulos em Markdown:

### 💸 Principais Gastos e Despesas
Descreva quais são as principais contas de despesa/custo identificadas no texto (ex: despesas
administrativas, despesas financeiras, custo das mercadorias/serviços vendidos, despesas com
pessoal etc.), citando os valores explícitos encontrados no texto quando estiverem disponíveis.
Se o texto não detalhar despesas por conta, diga isso claramente e comente o nível geral de
comprometimento financeiro com base apenas no Resultado do Exercício e no Passivo.

### 🔮 Estimativa de Gastos Futuros
A partir SOMENTE dos dados deste período disponível, apresente uma estimativa cautelosa da
tendência de gastos para os próximos períodos. Deixe explícito que é uma estimativa aproximada
e não uma previsão garantida, já que uma projeção confiável exigiria uma série histórica de
vários períodos. Quando fizer sentido, aponte uma faixa aproximada ou percentual de variação
plausível, sempre destacando a incerteza envolvida.

### ✅ Sugestões de Gestão Financeira
Dê de 3 a 5 sugestões práticas e específicas para a empresa, baseadas nos indicadores
calculados (ex: capital de giro, endividamento, resultado do exercício, prejuízos acumulados).
Seja objetivo e evite recomendações genéricas que sirvam para qualquer empresa.

Regras obrigatórias:
- NÃO invente valores que não constem no texto extraído ou nos indicadores já calculados.
- NÃO repita a lista de indicadores já apresentada anteriormente ao usuário.
- Ao final da resposta, inclua em itálico a frase: "Esta análise foi gerada por inteligência
  artificial e tem caráter informativo. Não substitui a avaliação de um contador ou consultor
  financeiro habilitado."

--- INDICADORES JÁ CALCULADOS ---
{conteudo}
-----------------------------

--- TEXTO EXTRAÍDO DO PDF (para detalhamento de despesas, se houver) ---
{texto_pdf}
-----------------------------
"""
                        try:
                            response_analise = client.chat.completions.create(
                                messages=[{"role": "user", "content": prompt_analise}],
                                model="openai/gpt-oss-120b",
                                temperature=0.3,
                                max_tokens=2048,
                            )
                            analise_texto = response_analise.choices[0].message.content
                            analise_segura = analise_texto.replace("$", "\\$")

                            st.markdown("---")
                            st.markdown(analise_segura, unsafe_allow_html=True)
                        except Exception as e:
                            st.warning(f"Não foi possível gerar a análise descritiva: {e}")

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
