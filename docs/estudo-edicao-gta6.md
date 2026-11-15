# Estudo: melhorias de edição de vídeos de GTA 6 no OpenShorts

**Data:** 14/11/2026. **Documento complementar:** [layout-camera-e-tela.md](./layout-camera-e-tela.md). Este documento orienta o pipeline de reenquadramento, legendas, seleção editorial e gate de qualidade, e não substitui a aprovação manual.

## 1. Escopo, método e limitações

Foram examinadas três prévias por amostragem visual em aproximadamente 8, 25 e 35 segundos de cada uma (nove imagens); para o podcast, houve também inspeção em 3, 18 e 30 segundos. Os tempos referem-se ao corte, não ao vídeo original. A amostra é deliberadamente escolhida para diagnóstico de falhas de layout e edição: não representa todos os vídeos de GTA 6, nem prova que um estilo único gera mais engajamento.

**Vídeos e evidências:**

1. **Alanzoka e trailer de GTA 6**, fonte pública.[1] Na prévia original, a câmera ocupa uma caixa no alto à esquerda, com o trailer ao fundo; a legenda amarela aparece na parte inferior sobre o vídeo. Elementos do player original aparecem na parte de baixo em alguns quadros. A prévia reúne câmera e trailer num corte vertical lateral porque o classificador seguiu apenas o rosto na webcam, sem separar a região da câmera e a região do trailer.
2. **BRKsEDU comenta GTA 6**, fonte pública.[2] A prévia de 36 s mostra um mosaico vertical com participantes repetidos ou parcialmente cortados, além de QR code; as legendas se sobrepõem ao conteúdo. O trecho selecionado trata de pré-venda de jogos em geral, de modo que a relevância para GTA 6 deve ser avaliada sem atribuir-lhe uma afirmação específica sobre o jogo.
3. **Amostra Davy Jones, score 74** (`gta6_davy_jones_test_font32_score_74.mp4`): nos frames amostrados vê-se imagem de embalagem/objeto repetida lado a lado, sobre fundo desfocado; legenda em amarelo na parte inferior. A comparação com a origem é obrigatória antes de classificar repetição como erro introduzido pelo pipeline.
4. **Flow Games: reação ao primeiro trailer**, página pública, duração observada de 3h32min55s.[3] A descrição anuncia reação e discussão dos detalhes; os capítulos recuperados distinguem blocos de trailer, análise e teorias.
5. **Flow Games: análise do trailer 2**, página pública.[4] O título e capítulos recuperados indicam cobertura longa com seções de análise e participação de convidados.
6. **Flow Games + SanInPlay: análise frame a frame**, página pública.[5] O título identifica formato de análise detalhada.

## 2. Padrões editoriais e hipóteses

Uma política única de crop não é adequada para todos esses layouts (comentário sobre trailer, podcast/entrevista em mosaico, apresentação de objeto e análise frame a frame).

**Hipóteses e diretrizes de design:**
- Um corte que responde a uma pergunta específica tem contexto mais claro do que um trecho escolhido apenas por score. Avaliar abertura, desenvolvimento e conclusão, preservando quem disse o quê.
- Um detalhe visual deve permanecer visível durante a fala sobre ele; em trailers e gameplays, um crop de rosto que elimina o elemento comentado destrói contexto.
- Em podcast, o falante costuma ser importante, mas reações, gráficos ou falas sobrepostas justificam mostrar o grupo sem cortar rostos nas bordas. Detectar uma pessoa não é prova de que ela está falando.
- Para rumor, vazamento, teoria, previsão e opinião, manter a qualificação original também no hook, título e legendas. Não inferir lançamento, confirmação oficial ou gameplay jogável a partir de conteúdo especulativo.
- Não existe duração, ritmo de cortes, tamanho de legenda ou número de palavras universalmente vencedor demonstrado por esta amostra.

## 3. Requisitos editoriais por tipo

**A. Trailer / gameplay com webcam:** detectar tela, webcam e seus limites por trecho, trabalhando na mídia original. Quando ambas forem relevantes e a confiança for suficiente (ou quando selecionadas manualmente), compor câmera em cima, faixa de legenda central e tela embaixo (`camera_inset.py`). Preservar proporções; padding ou fundo desfocado é preferível a esticar. Caso a tela fique pequena demais, alternar para tela maior ou preservar quadro inteiro. A posição da câmera pode mudar ou desaparecer; fallback e ajuste manual são obrigatórios.

**B. Podcast / entrevista:** localizar painéis e pessoas sem atribuir identidade ou falante ativo de maneira automática e irrefletida. Priorizar falante com evidência audiovisual (`active_speaker.py`); caso haja sobreposição, silêncio, voz fora de quadro ou incerteza, manter grupo/contexto (`GENERAL` seguro com largura preservada para rostos nas bordas ou `PANEL`) e evitar saltos. Impedir duplicações introduzidas pelo processamento (`split_layout.py`, `panel_layout.py`); respeitar duplicações intencionais da fonte. Evitar cortar cabeças, rostos e nomes dos participantes. Conferir QR code, banners e patrocinadores antes de qualquer remoção: não apagar contexto relevante automaticamente.

**C. Apresentação de produto/objeto:** preservar mãos, objeto, embalagem e gestos; só aproximar se o objeto continuar compreensível. Prevenir repetição acidental de painéis e comparação falsa entre objetos. Se não houver enquadramento seguro, mostrar a imagem inteira com preenchimento e enviar para revisão.

**D. Análise de trailer/detalhe:** manter o trecho de imagem que está sendo comentado e permitir zoom ou recorte apenas quando a região puder ser identificada (`screencast_layout.focus_crop` / `wide_crop`); manter no campo texto, mapas e elementos relevantes. Não recortar ou ampliar detalhes aleatórios só por movimento.

**E. Trecho predominantemente de fala:** centralizar a pessoa, respeitar proporção, acompanhar suavemente (`SmoothedCameraman`) e preservar expressão e gestos relevantes. Não inferir que todo momento com rosto visível deve virar close (especialmente quando o rosto é uma webcam pequena no canto de um trailer/gameplay).

## 4. Pipeline de implementação

1. **Pré-análise:** obter mídia original autorizada, metadados, cenas e transcrição com timestamps. Preservar origem/timestamps e registrar confiança e proveniência; nunca reutilizar como entrada um vertical já recortado se a fonte horizontal estiver disponível.
2. **Plano e layout:** detectar mudanças de plano e classificar estabilidade do layout (`reframe_v2.py`). Segmentos com layout instável ou baixa confiança vão para fallback (`GENERAL`/`WIDE`) e sinalizam revisão.
3. **Regiões e tracking:** localizar rostos, tela, painéis, objeto e texto; exigir continuidade temporal.
4. **Falante e intenção editorial:** combinar áudio e movimento labial (`active_speaker.py`) de forma probabilística, mantendo imagem estável em baixa confiança.
5. **Decisão de composição:** selecionar crop suavizado (`TRACK`), layout dividido (`SPLIT` / `PANEL`), câmera + tela (`INSET` / `CAM_SCREEN` com câmera no topo, legenda central e tela embaixo) ou imagem inteira com fundo (`GENERAL` / `WIDE`), respeitando relação de aspecto exata de cada região.
6. **Legendas após a composição:** gerar ASS com alinhamento por palavra (`subtitles.py`), posicionar na faixa central em layouts divididos (`SPLIT`, `INSET`, `CAM_SCREEN`) e ajustar tamanho/margem por layout.
7. **Título, hook e trecho:** verificar correspondência com fala e assunto, qualificar hipóteses/rumores/opiniões (`clip_selection.evaluate_editorial_quality`, `gemini_worker.py`), evitar cortes no meio da frase e encerramentos truncados.
8. **Quality gate (`quality_gate.py`):** além de resolução, áudio e duração, amostrar visualmente início/meio/fim, mudanças de plano e pontos de baixa confiança; conferir proporção geométrica das camadas (sem deformação incorporada), rostos nas bordas, duplicações introduzidas, QR codes/banners, área segura de legendas/hook e fidelidade editorial.
9. **Revisão e entrega:** gerar prévia, permitir ajustes manuais por cena (`ReframeEditor.jsx`, incluindo modo `cam + screen` e `split`) e exigir aprovação humana explícita (`/api/clip/review`) antes de publicação.

## 5. Legendas, áreas seguras e direitos

- Usar 9:16 como alvo de produção deste fluxo, não como lei universal.[9]
- Respeitar áreas seguras de sobreposição de interface (topo e base das plataformas).[10][11]
- Verificar regras de Content ID e licenças de música, trailer, câmera e trechos de terceiros antes de publicar em cada plataforma.[9][12]
- Conferir legibilidade em celular, contraste, quebras em até duas linhas e sincronização; ajustar tamanho de fonte por layout sem presumir que um único tamanho global sirva para todas as cenas.[13][15]

## 6. Validação, dados e experimentos

**Referências de comparação (`quality_gate.compare_layout_candidates`):**
- **A)** Crop central fixo (`centre_crop`)
- **B)** Imagem inteira com padding/fundo desfocado (`full_padding`)
- **C)** Método automático anterior (`current_auto`)
- **D)** Melhorias propostas com câmera + tela, proteção de bordas em podcast e faixa central de legenda (`proposed_layout`)

**Métricas avaliadas:**
- Preservação exata de proporção (`max_aspect_error == 0`)
- Proporção de tempo com rosto e conteúdo essencial visíveis (`face_visibility_ratio`)
- Frequência de cortes de rosto nas bordas (`border_face_cuts`)
- Duplicações introduzidas entre painéis (`introduced_duplications`)
- Saltos de enquadramento por minuto (`pan_jumps_per_min`)
- Fidelidade editorial (início/fim de frase, qualificação de rumores/opiniões, relevância temática)
- Estado de revisão humana (`review_status`: `pending_review` | `approved` | `rejected`)

## Sources

[1] https://www.youtube.com/watch?v=3uQJ8X831Qw
[2] https://www.youtube.com/watch?v=yZsPEJtLLAk
[3] https://www.youtube.com/watch?v=fsvCPrw6MfY
[4] https://www.youtube.com/watch?v=zoleCgvsp5M
[5] https://www.youtube.com/watch?v=fiAwooJcB08
[6] https://research.google/blog/autoflip-an-open-source-framework-for-intelligent-video-reframing
[7] https://arxiv.org/abs/2107.06592
[8] https://arxiv.org/abs/2303.00747
[9] https://support.google.com/youtube/answer/15424877?hl=en
[10] https://support.google.com/youtube/answer/16215842?co=GENIE.Platform%3DiOS&hl=en
[11] https://ads.tiktok.com/help/article/creative-best-practices
[12] https://support.tiktok.com/pt_BR/business-and-creator/creator-and-business-accounts/commercial-use-of-music-on-tiktok
[13] https://www.w3.org/WAI/WCAG21/Understanding/captions-prerecorded
[14] https://github.com/soCzech/TransNetV2
[15] https://ffmpeg.org/ffmpeg-filters.html
