# 📊 Analisador Inteligente de Balanços e DRE

Aplicação em Streamlit que lê um PDF contábil (Balanço Patrimonial / DRE), extrai os dados via texto nativo ou OCR, e usa um modelo de IA (via Groq) para gerar um diagnóstico financeiro estruturado.

## 🚀 Demo

Hospedado em: [Streamlit Community Cloud](https://share.streamlit.io/)

## ✨ Funcionalidades

- Upload de PDF de Balanço/DRE
- Extração de texto nativo (via `pdfplumber`) com fallback automático para **OCR** (via `pytesseract`) quando o PDF é digitalizado/escaneado
- Pré-processamento de imagem (contraste, nitidez, binarização) para melhorar a precisão do OCR em documentos escaneados
- Análise via IA (Groq API) com relatório estruturado em 4 seções:
  1. Estrutura do Ativo
  2. Estrutura do Passivo e Patrimônio Líquido
  3. Resultado e Prejuízos
  4. Diagnóstico Financeiro e Recomendações
- Destaque visual (amarelo) para todos os valores monetários
- Expander com o texto bruto extraído, para conferência manual dos números

## 🛠️ Tecnologias

- [Streamlit](https://streamlit.io/) — interface web
- [Groq](https://console.groq.com/) — inferência de IA (modelo `openai/gpt-oss-120b`)
- [pdfplumber](https://github.com/jsvine/pdfplumber) — extração de texto nativo de PDF
- [pytesseract](https://github.com/madmaze/pytesseract) + [pdf2image](https://github.com/Belval/pdf2image) — OCR para PDFs escaneados
- [Pillow (PIL)](https://python-pillow.org/) — pré-processamento de imagem

## 📦 Instalação local

### Pré-requisitos

- Python 3.9+
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) instalado no sistema (com o pacote de idioma português `por`)
- [Poppler](https://poppler.freedesktop.org/) instalado no sistema (necessário para o `pdf2image`)

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install tesseract-ocr tesseract-ocr-por poppler-utils
```

**macOS (Homebrew):**
```bash
brew install tesseract tesseract-lang poppler
```

### Passos

1. Clone o repositório:
```bash
git clone https://github.com/marinamelo0705-cpu/ia-analise-balan-o.git
cd ia-analise-balan-o
```

2. Instale as dependências Python:
```bash
pip install -r requirements.txt
```

3. Configure sua chave da Groq. Crie o arquivo `.streamlit/secrets.toml`:
```toml
GROQ_API_KEY = "sua_chave_aqui"
```
Ou insira a chave diretamente na barra lateral ao rodar o app (a chave gratuita pode ser obtida em [console.groq.com](https://console.groq.com/)).

4. Rode a aplicação:
```bash
streamlit run app.py
```

## ☁️ Deploy no Streamlit Community Cloud

1. Faça fork/push deste repositório para o GitHub.
2. Acesse [share.streamlit.io](https://share.streamlit.io/) e conecte o repositório.
3. Em **Settings → Secrets**, adicione:
```toml
GROQ_API_KEY = "sua_chave_aqui"
```
4. Adicione um arquivo `packages.txt` na raiz do repositório com as dependências de sistema (Tesseract e Poppler), já que o Streamlit Cloud roda em ambiente Linux:
```
tesseract-ocr
tesseract-ocr-por
poppler-utils
```
5. O deploy é automático a cada `git push` na branch `main`.

## 📄 requirements.txt (sugerido)

```
streamlit
groq
pdfplumber
pytesseract
pdf2image
pillow
```

## ⚠️ Notas importantes

- A precisão da leitura depende diretamente da qualidade do scan/foto do PDF enviado. Documentos nítidos, sem inclinação e com boa resolução geram resultados muito mais confiáveis.
- O app usa o modelo `openai/gpt-oss-120b` via Groq. Caso a Groq descontinue esse modelo no futuro, atualize a variável `model` na chamada `client.chat.completions.create()` em `app.py` (consulte [console.groq.com/docs/models](https://console.groq.com/docs/models) para a lista atualizada).
- Use sempre o expander "🔍 Ver texto bruto extraído do PDF" para conferir se um valor divergente veio de um erro de OCR ou de leitura da IA.

## 📝 Licença

Defina a licença do projeto aqui (ex: MIT).
