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

## O que o app confere automaticamente

Depois de extrair os valores, o app valida:

- `Ativo Circulante + Ativo Não Circulante = Ativo Total`
- `Passivo Circulante + Exigível Não Circulante + Patrimônio Líquido = Ativo Total`
  (equação contábil básica: Ativo = Passivo + PL)
- Consistência da DRE (`Receita Líquida − Custos = Lucro Bruto`), quando houver DRE.

Se algo não bater, aparece um aviso na tela pedindo para conferir manualmente — em vez
de apresentar um número possivelmente errado como se fosse certo.
