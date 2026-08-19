# 📊 Analisador Inteligente de Balanços e DRE

Aplicação em Streamlit que lê um PDF contábil (Balanço Patrimonial / DRE), extrai o texto (nativo ou via OCR) e usa IA (Groq) para retornar apenas os indicadores financeiros principais, já calculados e destacados.

## 🚀 Demo

Hospedado em: [Streamlit Community Cloud](https://share.streamlit.io/)

## ✨ O que o app retorna

A resposta é sempre uma lista objetiva, sem parágrafos ou recomendações, com estes 10 itens:

1. **Ativo Circulante**
2. **Ativo Não Circulante**
3. **Ativo Total**
4. **Imobilizado**
5. **Passivo Circulante**
6. **Exigível Não Circulante**
7. **Patrimônio Líquido**
8. **Capital de Giro Líquido** (calculado como Ativo Circulante − Passivo Circulante)
9. **Resultado do Exercício** (Lucro ou Prejuízo do período, identificado automaticamente)
10. **Prejuízos Acumulados**
11. **Z-Score de Altman** — indicador de risco de insolvência (veja seção abaixo)

Todos os valores monetários aparecem destacados em amarelo no relatório.

## 🔐 Login

O app fica protegido por usuário e senha (via [streamlit-authenticator](https://github.com/mkhorasani/Streamlit-Authenticator)),
para que só as pessoas da empresa consigam acessar — o link do app sozinho
não é suficiente, e ninguém de fora fica usando a cota da chave da Groq.

Depois de logar, a pessoa fica conectada por até 30 dias (cookie no
navegador), sem precisar digitar usuário/senha toda vez que abrir o app.

Os usuários são cadastrados em **Settings → Secrets** (não ficam no
código nem no repositório). Exemplo de configuração:

```toml
GROQ_API_KEY = "a-chave-da-groq-da-empresa"

[auth]
cookie_name = "auth_analisador_balanco"
cookie_key = "troque-por-uma-string-aleatoria-bem-grande-e-secreta"
cookie_expiry_days = 30

[auth.usuarios.maria]
nome = "Maria Silva"
senha = "escolha-uma-senha-forte-aqui"

[auth.usuarios.joao]
nome = "João Souza"
senha = "outra-senha-forte-aqui"
```

- `cookie_key` pode ser qualquer string aleatória grande (só precisa ser
  secreta e não mudar depois, senão os logins ativos expiram).
- Para adicionar/remover um funcionário, basta editar essa lista em Secrets
  — não precisa mexer no código nem fazer novo deploy.
- As senhas ficam nos Secrets do Streamlit (criptografados e privados, o
  mesmo lugar onde já fica a `GROQ_API_KEY`), nunca em texto público no
  GitHub.

## ⚠️ Z-Score de Altman

O app calcula automaticamente o **Z-Score de Altman**, um indicador que estima a
probabilidade de a empresa enfrentar dificuldades financeiras graves (insolvência/
falência) em um horizonte de até dois anos. Quanto mais baixa a nota, mais a empresa
se aproxima da chamada "Zona de Penumbra" ou "Zona de Perigo" — um estado financeiro
crítico ([fonte](https://br.investing.com/academy/analysis/altman-z-score/)).

- 🟢 **Zona Segura** (Z > 2,6)
- 🟡 **Zona de Penumbra/Cinza** (1,1 ≤ Z ≤ 2,6)
- 🔴 **Zona de Perigo** (Z < 1,1)

Diferente dos outros indicadores, o Z-Score **não é calculado pela IA**: a IA só
extrai os valores brutos do balanço (em um bloco JSON interno, não exibido), e o
Python calcula a fórmula com precisão — evita erro de arredondamento numa conta com
4 divisões. O app usa a variante **Z''** do modelo (Altman, Hartzell & Peck, 1995),
pensada para empresas privadas/mercados emergentes: não depende do valor de mercado
das ações nem da Receita de Vendas, o que a torna adequada a balanços de empresas
brasileiras não listadas em bolsa. Se o documento não trouxer dados suficientes
(ex: nenhuma linha de resultado), o app avisa que não foi possível calcular em vez
de arriscar um número incorreto.

## 💬 Análise descritiva e sugestões (opcional)

Além da lista de indicadores, o app pode gerar um segundo bloco de texto, em prosa,
com três seções:

1. **💸 Principais Gastos e Despesas** — resume as contas de despesa/custo identificadas
   no documento (quando o DRE detalha esses valores).
2. **🔮 Estimativa de Gastos Futuros** — uma estimativa cautelosa de tendência para os
   próximos períodos, sempre deixando claro que é uma aproximação (uma projeção robusta
   exigiria série histórica de vários períodos).
3. **✅ Sugestões de Gestão Financeira** — de 3 a 5 recomendações práticas baseadas nos
   indicadores calculados (capital de giro, endividamento, resultado do exercício etc.).

Essa análise é gerada em uma segunda chamada à IA, separada da extração numérica, para não
misturar texto corrido com a lista objetiva de valores. Pode ser ativada/desativada pelo
checkbox **"💬 Incluir análise descritiva e sugestões"** na barra lateral (ativado por padrão).
Ao final do texto, o app sempre inclui um aviso de que a análise é gerada por IA e não
substitui a avaliação de um contador ou consultor financeiro habilitado.

## ⚙️ Como funciona a extração

1. O PDF é lido página por página com `pdfplumber`.
2. Se uma página não tiver texto digital suficiente (documento escaneado/foto), ela é:
   - renderizada em 400 DPI (`pdf2image`);
   - pré-processada com **OpenCV** (desfoque de mediana + binarização Otsu) para remover marcas d'água e ruído de fundo que atrapalham a leitura dos números;
   - lida via **Tesseract OCR** (idioma português).
3. O texto final (nativo + OCR) é enviado para o modelo de IA na Groq, com instruções estritas para não inventar valores e não arredondar dígitos.
4. O relatório final é exibido com os valores destacados em amarelo.

## 🛠️ Tecnologias

- [Streamlit](https://streamlit.io/) — interface web
- [streamlit-authenticator](https://github.com/mkhorasani/Streamlit-Authenticator) — login por usuário/senha
- [Groq](https://console.groq.com/) — inferência de IA (modelo `openai/gpt-oss-120b`)
- [pdfplumber](https://github.com/jsvine/pdfplumber) — extração de texto nativo de PDF
- [pytesseract](https://github.com/madmaze/pytesseract) + [pdf2image](https://github.com/Belval/pdf2image) — OCR para páginas escaneadas
- [OpenCV](https://opencv.org/) (`opencv-python-headless`) — pré-processamento de imagem (remoção de ruído/marca d'água, binarização Otsu)
- [Pillow (PIL)](https://python-pillow.org/) — manipulação de imagem
- [NumPy](https://numpy.org/) — suporte ao processamento de imagem com OpenCV

## 📦 Instalação local

### Pré-requisitos de sistema

- Python 3.9+
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) com o pacote de idioma português (`por`)
- [Poppler](https://poppler.freedesktop.org/) (necessário para o `pdf2image`)

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

3. Configure sua chave da Groq e os usuários de login. Crie o arquivo `.streamlit/secrets.toml`:
```toml
GROQ_API_KEY = "sua_chave_aqui"

[auth]
cookie_name = "auth_analisador_balanco"
cookie_key = "troque-por-uma-string-aleatoria-bem-grande-e-secreta"
cookie_expiry_days = 30

[auth.usuarios.maria]
nome = "Maria Silva"
senha = "escolha-uma-senha-forte-aqui"
```

4. Rode a aplicação:
```bash
streamlit run app.py
```

## ☁️ Deploy no Streamlit Community Cloud

1. Faça push deste repositório para o GitHub.
2. Acesse [share.streamlit.io](https://share.streamlit.io/) e conecte o repositório.
3. Em **Settings → Secrets**, adicione a `GROQ_API_KEY` e o bloco `[auth]` (veja a seção "🔐 Login" acima).
4. Adicione um arquivo `packages.txt` na raiz do repositório com as dependências de sistema (o Streamlit Cloud roda em Linux):
