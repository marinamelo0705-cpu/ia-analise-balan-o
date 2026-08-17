import io
import re
import unicodedata

import streamlit as st
from groq import Groq
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np
import cv2

# Configuração da página
st.set_page_config(page_title="Analisador Contábil Completo", page_icon="📊", layout="wide")
st.title("📊 Analisador Inteligente de Balanços e DRE")
st.markdown(
    "Suba o arquivo PDF contábil da empresa para gerar o diagnóstico estruturado de "
    "Ativos, Passivos, Resultado e Recomendações."
)

# Busca a chave salva nos Secrets do Streamlit ou pede na barra lateral
if "GROQ_API_KEY" in st.secrets:
    groq_api_key = st.secrets["GROQ_API_KEY"]
else:
    groq_api_key = st.sidebar.text_input("Insira sua API Key da Groq (Grátis):", type="password")
    st.sidebar.info("Pegue sua chave gratuita em: https://console.groq.com/")

# Upload do PDF
pdf_file = st.file_uploader("Arraste e solte o PDF do Balanço/DRE aqui", type=["pdf"])


# =====================================================================================
# 1. PRÉ-PROCESSAMENTO DE IMAGEM (para páginas escaneadas)
# =====================================================================================

def preparar_imagem_para_ocr(imagem_pil):
    """
    Pré-processa a imagem escaneada antes do OCR:
    - converte pra tons de cinza
    - aplica median blur pra atenuar marcas d'água / textura de fundo (hachurados finos)
    - binariza com threshold automático (Otsu), deixando o texto preto puro
      sobre fundo branco puro.
    """
    arr = np.array(imagem_pil.convert('L'))
    arr = cv2.medianBlur(arr, 3)
    _, arr_bin = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(arr_bin)


# =====================================================================================
# 2. OCR "COM ESTRUTURA DE LINHA" (o coração do fix)
# =====================================================================================
#
# O balanço é uma tabela de duas colunas (Descrição | Saldo Atual). O app antigo usava
# pytesseract.image_to_string, que devolve um texto corrido: a ordem de leitura de uma
# tabela escaneada com essa densidade de linhas frequentemente escorrega, e o rótulo de
# uma conta acaba grudado no valor de outra conta. Isso é o motivo mais provável dos
# números não baterem com o PDF: o texto ia inteiro pro modelo e ele tinha que
# "adivinhar" qual valor pertencia a qual conta.
#
# A correção: usar image_to_data (que devolve a posição x/y de cada palavra), reagrupar
# as palavras em LINHAS (preservando a ordem em que aparecem no papel) e, só então,
# separar cada linha em (rótulo da conta) + (valor no final da linha). Cada linha do
# balanço tem exatamente um rótulo e um valor — extrair por linha garante que rótulo e
# valor nunca se misturem entre contas diferentes.

VALOR_RE = re.compile(r'(\(?-?\d{1,3}(?:\.\d{3})*,\d{2}\)?)\s*([DC])?\s*$', re.IGNORECASE)


def normalizar(texto):
    """Maiúsculas, sem acento, espaços colapsados — para comparar rótulos com tolerância
    a pequenas variações de OCR."""
    texto = texto.upper().strip()
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    texto = re.sub(r'[^A-Z0-9,.\-/ ]', ' ', texto)
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto


def parse_valor(token):
    """Converte um token de valor ('1.945.258,34D', '(179.462,15)', '793.376,08') em
    (magnitude_com_sinal, sufixo 'D'/'C'/None)."""
    token = token.strip()
    negativo = token.startswith('(') and token.endswith(')')
    token = token.strip('()')
    sufixo = None
    m = re.match(r'^(.*?)([DC])$', token, re.IGNORECASE)
    if m:
        token, sufixo = m.group(1), m.group(2).upper()
    token = token.replace('.', '').replace(',', '.')
    try:
        valor = float(token)
    except ValueError:
        return None, None
    if negativo:
        valor = -abs(valor)
    return valor, sufixo


def dividir_label_valor(linha_texto):
    """Separa uma linha em (rótulo, valor_bruto). valor_bruto é None se a linha não
    terminar em algo reconhecível como valor monetário (ex.: cabeçalhos)."""
    m = VALOR_RE.search(linha_texto)
    if not m:
        return linha_texto.strip(), None
    valor_bruto = m.group(1) + (m.group(2) or '')
    rotulo = linha_texto[:m.start()].strip(' -_.:|')
    return rotulo, valor_bruto


def ocr_linhas(imagem_pil, lang="por"):
    """OCR com bounding boxes, reagrupado em linhas na ordem em que aparecem no papel.
    Retorna lista de strings (uma por linha detectada)."""
    proc = preparar_imagem_para_ocr(imagem_pil)
    try:
        data = pytesseract.image_to_data(
            proc, lang=lang, config="--oem 3 --psm 6", output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractError:
        # fallback se o idioma "por" não estiver instalado no ambiente
        data = pytesseract.image_to_data(
            proc, lang="eng", config="--oem 3 --psm 6", output_type=pytesseract.Output.DICT
        )

    linhas = {}
    tops = {}
    n = len(data["text"])
    for i in range(n):
        palavra = data["text"][i].strip()
        if not palavra:
            continue
        chave = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        linhas.setdefault(chave, []).append((data["left"][i], palavra))
        tops[chave] = min(tops.get(chave, 10 ** 9), data["top"][i])

    resultado = []
    for chave in sorted(linhas.keys(), key=lambda k: tops[k]):
        palavras = sorted(linhas[chave])
        resultado.append(" ".join(p[1] for p in palavras))
    return resultado


def linhas_de_texto_digital(texto):
    """Mesma ideia, mas para páginas com texto digital (não escaneadas): já vem uma
    linha por linha, só precisamos preservar a ordem."""
    return [l for l in texto.splitlines() if l.strip()]


# =====================================================================================
# 3. EXTRAÇÃO DETERMINÍSTICA DOS CAMPOS (sem depender do LLM para os números)
# =====================================================================================
#
# Em vez de pedir pro modelo "leia o texto e me diga o Ativo Circulante", o app agora
# varre as linhas já separadas em (rótulo, valor) e casa por palavra-chave contábil.
# Isso é 100% reprodutível: o mesmo PDF sempre gera o mesmo número, e dá pra validar
# a "prova real" do balanço (Ativo = Passivo) em código, não confiando na álgebra do LLM.

CAMPOS_LABELS = {
    "ativo_total": "Ativo Total",
    "ativo_circulante": "Ativo Circulante",
    "ativo_nao_circulante": "Ativo Não Circulante",
    "imobilizado": "Imobilizado",
    "passivo_total": "Passivo Total",
    "passivo_circulante": "Passivo Circulante",
    "exigivel_nao_circulante": "Exigível Não Circulante (Passivo Não Circulante)",
    "patrimonio_liquido": "Patrimônio Líquido",
    "capital_social": "Capital Social (Capital Líquido)",
    "prejuizo_acumulado": "Prejuízo Acumulado",
}


def extrair_campos_balanco(linhas_brutas):
    """linhas_brutas: lista de strings (uma por linha, na ordem do papel).
    Retorna (campos: dict rótulo->valor float|None, auditoria: lista de (rótulo, valor) lidos)."""
    campos = {chave: None for chave in CAMPOS_LABELS}
    auditoria = []
    lado = None
    circulante_lido_no_lado_atual = False

    for linha in linhas_brutas:
        rotulo_bruto, valor_bruto = dividir_label_valor(linha)
        if not valor_bruto:
            continue
        valor, _sufixo = parse_valor(valor_bruto)
        if valor is None:
            continue
        norm = normalizar(rotulo_bruto)
        if not norm:
            continue
        auditoria.append((rotulo_bruto.strip(), valor, valor_bruto))

        if norm == "ATIVO":
            lado = "ATIVO"
            circulante_lido_no_lado_atual = False
            campos["ativo_total"] = valor
            continue
        if norm == "PASSIVO":
            lado = "PASSIVO"
            circulante_lido_no_lado_atual = False
            campos["passivo_total"] = valor
            continue

        if norm == "CIRCULANTE" and not circulante_lido_no_lado_atual:
            circulante_lido_no_lado_atual = True
            if lado == "ATIVO":
                campos["ativo_circulante"] = valor
            elif lado == "PASSIVO":
                campos["passivo_circulante"] = valor
            continue

        if lado == "ATIVO" and "NAO CIRCULANTE" in norm and campos["ativo_nao_circulante"] is None:
            campos["ativo_nao_circulante"] = valor
            continue

        if lado == "PASSIVO" and "EXIGIVEL" in norm and "NAO CIRCULANTE" in norm and campos["exigivel_nao_circulante"] is None:
            campos["exigivel_nao_circulante"] = valor
            continue

        if norm == "IMOBILIZADO" and campos["imobilizado"] is None:
            campos["imobilizado"] = valor
            continue

        if norm == "PATRIMONIO LIQUIDO" and campos["patrimonio_liquido"] is None:
            campos["patrimonio_liquido"] = valor
            continue

        if norm == "CAPITAL SOCIAL" and campos["capital_social"] is None:
            campos["capital_social"] = valor
            continue

        if "PREJUIZO ACUMULADO" in norm and campos["prejuizo_acumulado"] is None:
            campos["prejuizo_acumulado"] = valor
            continue

    return campos, auditoria


def extrair_lucro_liquido(linhas_brutas):
    """Procura a linha 'LUCRO LÍQUIDO DO EXERCÍCIO' / 'PREJUÍZO LÍQUIDO DO EXERCÍCIO'
    na DRE. O sinal vem direto do parêntese (negativo) ou da ausência dele (positivo)."""
    for linha in linhas_brutas:
        rotulo_bruto, valor_bruto = dividir_label_valor(linha)
        if not valor_bruto:
            continue
        norm = normalizar(rotulo_bruto)
        if "LUCRO LIQUIDO DO EXERCICIO" in norm or "PREJUIZO LIQUIDO DO EXERCICIO" in norm:
            valor, _ = parse_valor(valor_bruto)
            if valor is not None and "PREJUIZO" in norm:
                valor = -abs(valor)
            return valor, rotulo_bruto.strip()
    return None, None


def eh_pagina_balanco(linhas_brutas):
    texto = normalizar(" ".join(linhas_brutas))
    return "BALANCO PATRIMONIAL" in texto or (" ATIVO " in f" {texto} " and " PASSIVO " in f" {texto} ")


def eh_pagina_dre(linhas_brutas):
    texto = normalizar(" ".join(linhas_brutas))
    return "DEMONSTRACAO DO RESULTADO" in texto or "RESULTADO DO EXERCICIO" in texto


# =====================================================================================
# 4. VALIDAÇÃO (prova real do balanço)
# =====================================================================================

def validar(campos):
    checks = []
    tolerancia = 1.00  # R$ 1,00 de folga para arredondamento/ruído de OCR

    ac, anc, at = campos["ativo_circulante"], campos["ativo_nao_circulante"], campos["ativo_total"]
    if None not in (ac, anc, at):
        diff = (ac + anc) - at
        checks.append((
            abs(diff) <= tolerancia,
            f"Ativo Circulante + Ativo Não Circulante = Ativo Total "
            f"({ac:,.2f} + {anc:,.2f} = {ac+anc:,.2f}; PDF mostra {at:,.2f})"
        ))

    pc, enc, pl, pt = (
        campos["passivo_circulante"], campos["exigivel_nao_circulante"],
        campos["patrimonio_liquido"], campos["passivo_total"],
    )
    if None not in (pc, enc, pl, pt):
        diff = (pc + enc + pl) - pt
        checks.append((
            abs(diff) <= tolerancia,
            f"Passivo Circulante + Exigível Não Circulante + Patrimônio Líquido = Passivo Total "
            f"({pc:,.2f} + {enc:,.2f} + {pl:,.2f} = {pc+enc+pl:,.2f}; PDF mostra {pt:,.2f})"
        ))

    if at is not None and pt is not None:
        diff = at - pt
        checks.append((
            abs(diff) <= tolerancia,
            f"Ativo Total = Passivo Total ({at:,.2f} vs {pt:,.2f})"
        ))

    return checks


def fmt_brl(valor):
    if valor is None:
        return "⚠️ Não localizado no documento — confira manualmente"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# =====================================================================================
# 5. MONTAGEM DO RELATÓRIO
# =====================================================================================

def montar_resumo_markdown(campos, lucro_liquido, checks):
    linhas = ["### 1. 🏢 ESTRUTURA DO ATIVO\n"]
    for chave in ["ativo_circulante", "ativo_nao_circulante", "imobilizado", "ativo_total"]:
        linhas.append(f"* **{CAMPOS_LABELS[chave]}:** <span style=\"color: #F1C40F; font-weight: bold;\">{fmt_brl(campos[chave])}</span>")

    linhas.append("\n### 2. 💳 ESTRUTURA DO PASSIVO E PATRIMÔNIO LÍQUIDO\n")
    for chave in ["passivo_circulante", "exigivel_nao_circulante", "patrimonio_liquido", "capital_social"]:
        linhas.append(f"* **{CAMPOS_LABELS[chave]}:** <span style=\"color: #F1C40F; font-weight: bold;\">{fmt_brl(campos[chave])}</span>")

    linhas.append("\n### 3. 📈 RESULTADO E PREJUÍZOS\n")
    if lucro_liquido is None:
        resultado_fmt = "⚠️ Não localizado no documento — confira manualmente"
    else:
        rotulo = "Lucro" if lucro_liquido >= 0 else "Prejuízo"
        resultado_fmt = f"{rotulo} de <span style=\"color: #F1C40F; font-weight: bold;\">{fmt_brl(abs(lucro_liquido))}</span>"
    linhas.append(f"* **Resultado do Exercício (Ano):** {resultado_fmt}")
    linhas.append(f"* **Prejuízos Acumulados:** <span style=\"color: #F1C40F; font-weight: bold;\">{fmt_brl(campos['prejuizo_acumulado'])}</span>")

    linhas.append("\n### ✅ Conferência automática (prova real do balanço)\n")
    if not checks:
        linhas.append("* Não foi possível rodar a conferência — faltam campos para comparar.")
    for ok, msg in checks:
        icone = "✅" if ok else "⚠️"
        linhas.append(f"* {icone} {msg}")

    return "\n".join(linhas)


def montar_prompt_diagnostico(campos, lucro_liquido, checks_ok):
    valores_validados = "\n".join(
        f"- {CAMPOS_LABELS[k]}: {fmt_brl(v)}" for k, v in campos.items()
    )
    resultado_txt = fmt_brl(abs(lucro_liquido)) if lucro_liquido is not None else "não informado"
    tipo_resultado = "Lucro" if (lucro_liquido or 0) >= 0 else "Prejuízo"

    aviso_inconsistencia = ""
    if not checks_ok:
        aviso_inconsistencia = (
            "\nATENÇÃO: a conferência automática (Ativo = Passivo) NÃO fechou — isso indica que "
            "algum valor pode ter sido lido incorretamente do PDF escaneado. Mencione essa ressalva "
            "explicitamente na análise, recomendando conferência manual.\n"
        )

    return f"""
Você é um auditor contábil sênior. Os valores abaixo já foram extraídos e VALIDADOS
diretamente do balanço (não são uma estimativa sua) — use-os exatamente como estão,
não recalcule nem arredonde diferente.

--- VALORES VALIDADOS DO BALANÇO ---
{valores_validados}
- {tipo_resultado} Líquido do Exercício: {resultado_txt}
{aviso_inconsistencia}
-----------------------------

Com base SOMENTE nesses valores, escreva em Markdown, usando a tag
<span style="color: #F1C40F; font-weight: bold;">R$ VALOR</span> sempre que citar um valor em reais:

### 4. 💡 DIAGNÓSTICO FINANCEIRO E IDEIAS DE AÇÃO
* **Análise da Saúde Financeira:** [resumo em 2 parágrafos]
* **Ideias e Recomendações Práticas:** [3 a 5 sugestões práticas para a diretoria]
"""


# =====================================================================================
# 6. FLUXO PRINCIPAL
# =====================================================================================

if pdf_file and groq_api_key:
    if st.button("🚀 Processar e Analisar Balanço"):
        with st.spinner("Lendo documento e extraindo dados (pode levar alguns segundos se for escaneado)..."):
            try:
                bytes_data = pdf_file.read()

                linhas_balanco = []  # todas as linhas de páginas de Balanço Patrimonial, em ordem
                linhas_dre = []      # todas as linhas de páginas de DRE, em ordem
                linhas_brutas_debug = []

                with pdfplumber.open(io.BytesIO(bytes_data)) as pdf:
                    total_paginas = len(pdf.pages)
                    for i, page in enumerate(pdf.pages, start=1):
                        t = page.extract_text()
                        if t and len(t.strip()) >= 20:
                            linhas_pagina = linhas_de_texto_digital(t)
                        else:
                            st.info(f"ℹ️ Página {i} parece escaneada/fotografada. Aplicando OCR com tratamento de imagem e leitura por linha...")
                            imagens_pagina = convert_from_bytes(bytes_data, dpi=400, first_page=i, last_page=i)
                            linhas_pagina = []
                            for img in imagens_pagina:
                                linhas_pagina.extend(ocr_linhas(img, lang="por"))

                        linhas_brutas_debug.append((i, linhas_pagina))

                        if eh_pagina_balanco(linhas_pagina):
                            linhas_balanco.extend(linhas_pagina)
                        elif eh_pagina_dre(linhas_pagina):
                            linhas_dre.extend(linhas_pagina)
                        else:
                            # página não identificada: entra nas duas buscas por segurança
                            linhas_balanco.extend(linhas_pagina)
                            linhas_dre.extend(linhas_pagina)

                texto_total = "\n".join(l for _, ls in linhas_brutas_debug for l in ls)
                if len(texto_total.strip()) < 30:
                    st.error("⚠️ Não foi possível reconhecer o texto do documento. Certifique-se de que a imagem esteja legível.")
                    st.stop()

                # --- Extração determinística (sem LLM) ---
                campos, auditoria = extrair_campos_balanco(linhas_balanco)
                lucro_liquido, rotulo_resultado = extrair_lucro_liquido(linhas_dre)
                checks = validar(campos)
                checks_ok = all(ok for ok, _ in checks) if checks else False

                resumo_md = montar_resumo_markdown(campos, lucro_liquido, checks)

                # --- LLM só para a parte qualitativa (diagnóstico e recomendações) ---
                client = Groq(api_key=groq_api_key)
                prompt_diagnostico = montar_prompt_diagnostico(campos, lucro_liquido, checks_ok)
                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt_diagnostico}],
                    model="openai/gpt-oss-120b",
                    temperature=0.1,
                    max_tokens=2048,
                )
                diagnostico_md = response.choices[0].message.content

                conteudo_final = resumo_md + "\n\n" + diagnostico_md
                conteudo_seguro = conteudo_final.replace("$", "\\$")

                st.success("Análise concluída com sucesso!")
                if not checks_ok and checks:
                    st.warning(
                        "⚠️ A conferência automática (Ativo = Passivo) não fechou perfeitamente. "
                        "Isso normalmente indica erro de leitura de OCR em algum valor — confira "
                        "os números abaixo contra o PDF original antes de usar o relatório."
                    )

                st.markdown("---")
                st.markdown(conteudo_seguro, unsafe_allow_html=True)

                with st.expander("🔍 Ver linhas extraídas por página (debug / auditoria)"):
                    for i, ls in linhas_brutas_debug:
                        st.markdown(f"**Página {i}**")
                        st.text("\n".join(ls))

                with st.expander("🔍 Ver todas as contas casadas na extração determinística"):
                    for rotulo, valor, valor_bruto in auditoria:
                        st.text(f"{rotulo:<55} {valor_bruto}")

            except Exception as e:
                st.error(f"Erro ao processar o arquivo: {e}")

elif pdf_file and not groq_api_key:
    st.warning("⚠️ Insira a sua API Key da Groq para continuar.")
