# Analisador Contábil de Balanços e DRE

App em Streamlit que lê PDFs de Balanço Patrimonial (e opcionalmente DRE), extrai os
valores de Ativo, Passivo, Patrimônio Líquido e Resultado do Exercício usando a API da
Groq (Llama 3.3), confere se as contas fecham e gera um diagnóstico financeiro.

## Arquivos deste projeto

- `app.py` — aplicação Streamlit.
- `requirements.txt` — dependências Python.
- `packages.txt` — dependências de sistema (Tesseract OCR e Poppler), necessárias no
  Streamlit Community Cloud para o fallback de OCR funcionar com PDFs escaneados.

## Rodando localmente

```bash
pip install -r requirements.txt
```

No Linux/Mac também é preciso instalar os pacotes de sistema (no Cloud isso é feito
automaticamente pelo `packages.txt`):

```bash
# Ubuntu/Debian
sudo apt-get install tesseract-ocr tesseract-ocr-por poppler-utils
```

```bash
streamlit run app.py
```

## Configurando a chave da Groq

**Local:** crie o arquivo `.streamlit/secrets.toml` (não versione este arquivo):

```toml
GROQ_API_KEY = "sua_chave_aqui"
```

**No Streamlit Community Cloud:** no painel do app, vá em *Settings → Secrets* e cole
o mesmo conteúdo acima. Sem isso, o app pede a chave manualmente na barra lateral
(funciona, mas cada usuário precisa da própria chave grátis em
[console.groq.com](https://console.groq.com/)).

## Por que existe o `packages.txt`

O app usa `pytesseract` e `pdf2image` como fallback para PDFs escaneados/fotografados
(quando o texto não pode ser extraído diretamente). Essas bibliotecas Python dependem
de binários de sistema — `tesseract-ocr` e `poppler-utils` — que **não vêm instalados
por padrão no Streamlit Community Cloud**. Sem o `packages.txt`, o app quebra com erro
de "tesseract não encontrado" assim que tenta processar um PDF escaneado. Basta este
arquivo estar na raiz do repositório para o Cloud instalar tudo automaticamente no
próximo deploy.

## Como os 10 números do balanço são extraídos

O balanço é uma tabela (Descrição | Saldo Atual). O app faz OCR preservando a posição
de cada palavra (`pytesseract.image_to_data`), reagrupa as palavras em **linhas** na
ordem em que aparecem no papel e só então separa cada linha em `(rótulo da conta,
valor no final da linha)`. Isso evita o problema clássico de OCR em tabelas: o rótulo
de uma conta colar no valor de outra.

Os 10 campos pedidos (Ativo Circulante, Ativo Não Circulante, Ativo Total, Passivo
Circulante, Exigível Não Circulante, Patrimônio Líquido, Prejuízo Acumulado, Capital
Social, Lucro Líquido e Imobilizado) são casados por **palavra-chave contábil em
código Python** — não pedimos pro modelo de IA "adivinhar" esses números. O LLM
(Groq) só é usado para escrever o texto de diagnóstico/recomendações; os valores
que aparecem no relatório vêm sempre do parser determinístico.

## O que o app confere automaticamente

Depois de extrair os valores, o app valida (prova real do balanço):

- `Ativo Circulante + Ativo Não Circulante = Ativo Total`
- `Passivo Circulante + Exigível Não Circulante + Patrimônio Líquido = Passivo Total`
  (equação contábil básica: Ativo = Passivo + PL)
- `Ativo Total = Passivo Total`

Se algo não bater (diferença maior que R$ 1,00) ou se um campo não for localizado no
texto, aparece um aviso explícito (⚠️) em vez de apresentar um número possivelmente
errado como se fosse certo. Use o expansor "Ver linhas extraídas por página" no final
do relatório para auditar exatamente o que o OCR leu, linha por linha.
