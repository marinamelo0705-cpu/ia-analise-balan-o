import sys
import io
import json
import re
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


def calcular_zscore_altman(dados):
    """
    Calcula o Z''-Score de Altman — a variante do modelo pensada para empresas
    privadas / mercados emergentes (Altman, Hartzell & Peck, 1995), que não
    depende do valor de mercado das ações nem da Receita de Vendas. É a
    variante mais adequada aqui, já que o app lê balanços de empresas
    brasileiras tipicamente não listadas em bolsa.

    O cálculo é feito em Python (não pela IA) para evitar erro de arredondamento
    ou de aritmética em uma fórmula com 4 divisões e pesos decimais.

    Fórmula: Z'' = 6,56*X1 + 3,26*X2 + 6,72*X3 + 1,05*X4, onde:
      X1 = Capital de Giro Líquido / Ativo Total
      X2 = Lucros/Prejuízos Acumulados / Ativo Total
      X3 = EBIT (proxy) / Ativo Total
      X4 = Patrimônio Líquido / Passivo Total

    `dados` é o dicionário extraído do bloco JSON retornado pela IA junto com
    a lista de indicadores. Retorna None se faltar algum valor obrigatório.
    """
    try:
        ativo_total = float(dados["ativo_total"])
        ativo_circulante = float(dados["ativo_circulante"])
        passivo_circulante = float(dados["passivo_circulante"])
        exigivel_nao_circulante = float(dados["exigivel_nao_circulante"])
        patrimonio_liquido = float(dados["patrimonio_liquido"])

        lucros_prejuizos_acumulados = dados.get("prejuizos_acumulados")
        lucros_prejuizos_acumulados = float(lucros_prejuizos_acumulados) if lucros_prejuizos_acumulados is not None else 0.0

        # EBIT não é um item extraído diretamente — usamos como proxy o
        # "Resultado Antes do IR e CSLL" (mais próximo do conceito de EBIT).
        # Se essa linha não existir no documento, caímos para o Resultado do
        # Exercício (menos preciso, pois já é líquido de IR/CSLL/provisões),
        # e sinalizamos isso para exibir um aviso ao usuário.
        usou_fallback_ebit = False
        ebit_proxy = dados.get("resultado_antes_ir_csll")
        if ebit_proxy is None:
            ebit_proxy = dados.get("resultado_exercicio")
            usou_fallback_ebit = True
        if ebit_proxy is None:
            return None
        ebit_proxy = float(ebit_proxy)

        passivo_total = passivo_circulante + exigivel_nao_circulante
        capital_de_giro = ativo_circulante - passivo_circulante

        if ativo_total == 0 or passivo_total == 0:
            return None

        x1 = capital_de_giro / ativo_total
        x2 = lucros_prejuizos_acumulados / ativo_total
        x3 = ebit_proxy / ativo_total
        x4 = patrimonio_liquido / passivo_total

        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4

        if z > 2.6:
            zona = "🟢 Zona Segura"
        elif z >= 1.1:
            zona = "🟡 Zona de Penumbra (Cinza)"
        else:
            zona = "🔴 Zona de Perigo"

        return {"z": z, "zona": zona, "usou_fallback_ebit": usou_fallback_ebit}
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
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
9. Após a lista de itens, inclua um bloco de código JSON (delimitado por ```json e ```) com os MESMOS valores acima, mas em formato NUMÉRICO BRUTO — sem "R$", sem separador de milhar, com PONTO como separador decimal (padrão JSON), e SEM nenhum texto antes ou depois do bloco. Esse JSON é usado internamente pelo sistema para cálculos adicionais e não é mostrado ao usuário. Se o Resultado do Exercício for Prejuízo, ou se os Prejuízos/Lucros Acumulados forem negativos, use sinal negativo no número. Se algum valor não existir no documento, use null (nunca invente). Preencha também "resultado_antes_ir_csll" com o valor de "Resultado Antes do IR" ou "Resultado Antes do IR e CSLL" ANTES de descontar Provisões/Participações (use null se essa linha não existir no texto). Formato exato das chaves:
```json
{{
  "ativo_circulante": 0.0,
  "ativo_nao_circulante": 0.0,
  "ativo_total": 0.0,
  "passivo_circulante": 0.0,
  "exigivel_nao_circulante": 0.0,
  "patrimonio_liquido": 0.0,
  "capital_de_giro_liquido": 0.0,
  "resultado_exercicio": 0.0,
  "prejuizos_acumulados": 0.0,
  "resultado_antes_ir_csll": null
}}
```

--- TEXTO EXTRAÍDO DO PDF ---
{texto_pdf}
-----------------------------

Responda EXATAMENTE neste formato, preenchendo os valores em Markdown (e o bloco JSON da regra 9 logo depois da lista):

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

```json
{{ ... }}
```
"""

                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="openai/gpt-oss-120b",
                    temperature=0.1,
                    max_tokens=4096,
                )

                conteudo = response.choices[0].message.content

                # 3. Extrai o bloco JSON com os valores numéricos brutos (regra 9 do
                #    prompt), usado só internamente para calcular o Z-Score de Altman
                #    em Python — mais confiável do que pedir pra IA fazer a divisão na
                #    mão. O bloco é removido do texto antes de exibir ao usuário.
                dados_numericos = None
                match_json = re.search(r"```json\s*(\{.*?\})\s*```", conteudo, re.DOTALL)
                if match_json:
                    try:
                        dados_numericos = json.loads(match_json.group(1))
                    except json.JSONDecodeError:
                        dados_numericos = None
                    conteudo = (conteudo[:match_json.start()] + conteudo[match_json.end():]).rstrip()

                resultado_zscore = calcular_zscore_altman(dados_numericos) if dados_numericos else None

                if resultado_zscore:
                    linha_zscore = (
                        f'\n* **Z-Score de Altman:** '
                        f'<span style="color: #F1C40F; font-weight: bold;">{resultado_zscore["z"]:.2f}</span>'
                        f' — {resultado_zscore["zona"]}'
                    )
                    if resultado_zscore["usou_fallback_ebit"]:
                        linha_zscore += (
                            " *(calculado usando o Resultado do Exercício como aproximação de EBIT, "
                            "pois o documento não trazia 'Resultado Antes do IR e CSLL')*"
                        )
                    conteudo += linha_zscore
                else:
                    conteudo += (
                        "\n* **Z-Score de Altman:** Não foi possível calcular "
                        "(faltam dados suficientes no documento)."
                    )

                # 4. Exibição do relatório final.
                # Escapa "$" soltos para o Streamlit não confundir com LaTeX (\$...\$),
                # o que causava a renderização quebrada ("R`" no lugar de "R$").
                conteudo_seguro = conteudo.replace("$", "\\$")

                st.success("Análise concluída com sucesso!")
                st.markdown("---")
                st.markdown(conteudo_seguro, unsafe_allow_html=True)

                if resultado_zscore:
                    with st.expander("ℹ️ O que é o Z-Score de Altman?"):
                        st.markdown(
                            """
O **Z-Score de Altman** é um indicador que estima a probabilidade de uma empresa
enfrentar dificuldades financeiras graves (insolvência/falência) em um horizonte
de até dois anos, combinando indicadores de liquidez, rentabilidade e
endividamento extraídos do balanço.

Quanto **mais baixa** a nota, mais a empresa se aproxima da chamada **"Zona de
Penumbra"** ou **"Zona de Perigo"**, indicando um estado financeiro crítico.

- 🟢 **Zona Segura** (Z > 2,6): baixo risco de insolvência no curto/médio prazo.
- 🟡 **Zona de Penumbra/Cinza** (1,1 ≤ Z ≤ 2,6): risco moderado, requer atenção.
- 🔴 **Zona de Perigo** (Z < 1,1): alto risco de dificuldades financeiras graves.

Este app usa a variante do modelo (Z'') voltada a empresas privadas e mercados
emergentes, que não depende do valor de mercado das ações nem da Receita de
Vendas — mais adequada a balanços de empresas brasileiras não listadas em bolsa.

Fonte: [Investing.com Academy — "Altman Z-Score"](https://br.investing.com/academy/analysis/altman-z-score/)

*O Z-Score é um indicador estatístico e não substitui a avaliação de um
contador ou consultor financeiro habilitado.*
"""
                        )

                with st.expander("🔍 Ver texto bruto extraído do PDF (debug)"):
                    st.text(texto_pdf)
                    if leitura_precisa_resultado:
                        st.markdown("**Leitura de alta precisão da linha de resultado:**")
                        st.text(leitura_precisa_resultado)

                # 5. Análise descritiva: um
