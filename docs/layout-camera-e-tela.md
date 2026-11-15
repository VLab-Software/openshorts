# Melhoria: layout vertical para câmera e tela

**Estudo complementar:** consulte [estudo-edicao-gta6.md](./estudo-edicao-gta6.md) para pesquisa, exemplos, hipóteses, critérios de avaliação e plano priorizado de implementação.

**Escopo:** vídeos em que o apresentador/streamer aparece numa câmera pequena enquanto compartilha jogo, trailer ou outra tela, não somente a reacts.

## Resultado desejado

- Compor em 9:16: câmera do streamer em cima, legendas legíveis em uma faixa central e jogo/tela embaixo.
- Usar as regiões da fonte original, antes de um reenquadramento vertical que possa ter perdido informação. Preservar a proporção de cada região; usar crop apenas quando intencional e necessário, ou padding/fundo quando apropriado. Não alongar rostos, interface ou conteúdo.
- Reservar espaço para legenda e hook sem encobrir rosto, informação importante, HUD ou elementos essenciais; respeitar áreas seguras de interface nas plataformas. Evitar que uma faixa de legenda torne a tela pequena demais para entender.

## Melhorias prioritárias do estudo de GTA 6

- **P0 — Qualidade editorial e visual:** revisar origem e prévia em trechos distribuídos e mudanças de cena; conferir rosto, HUD, duplicações introduzidas, fidelidade do hook e relevância do assunto. Não usar score ou resolução como prova de qualidade (`quality_gate.py`, `clip_selection.py`).
- **P1 — Reenquadramento por cena:** classificar o layout com indicação de confiança e preservar rostos, tela, objetos e texto essenciais; usar imagem completa com preenchimento (`GENERAL`/`WIDE` seguro) ou revisão manual quando houver dúvida. Suavizar o movimento e transições entre cenas para evitar saltos.
- **P2 — Câmera e tela:** permitir seleção manual das regiões na fonte original (`ReframeEditor.jsx`, `reframe_v2.apply_crop_overrides`), câmera acima, legenda central e tela abaixo, sem distorcer (`camera_inset.py`).
- **P3 — Podcasts:** manter contexto em mosaicos, evitar duplicações introduzidas (`split_layout.py`, `panel_layout.py`), priorizar falante apenas com confiança e não remover automaticamente banners ou QR codes relevantes (`quality_gate.py`).
- **P4 — Automação avaliada:** detecção de cenas, regiões e falante ativo com validação humana; transcrição alinhada e qualificação cautelosa para rumores, teorias e opiniões (`gemini_worker.py`, `clip_selection.py`).
- **Comparação e aceite:** comparar saída atual, crop central, imagem completa com padding e novos layouts nos mesmos trechos (`quality_gate.compare_layout_candidates`); medir falhas visuais, estabilidade, legibilidade e tempo. Manter aprovação humana e verificar direitos para cada plataforma antes de publicar.

## Fluxo de implementação

1. Inspecionar amostras distribuídas da fonte e identificar se câmera e tela aparecem simultaneamente, incluindo posição e limites da câmera, mudanças de layout e trechos sem câmera (`camera_inset.detect_with_confidence`). Não inferir coordenadas universais a partir de um único vídeo.
2. Implementar detecção ou seleção das regiões com indicação de confiança; quando a detecção for incerta, a câmera desaparecer ou os limites mudarem, usar um fallback de enquadramento comum e permitir revisão/ajuste manual. Definir tratamento de transições sem saltos bruscos (`camera_inset.smooth_scene_insets`).
3. Compor duas regiões em um canvas vertical, preservando proporção (`camera_inset.inset_filtergraph` / `cam_screen_filtergraph`); posicionar e queimar legendas na faixa central após o layout (`layout_ranges.py`, `subtitles.py`), mantendo sincronização e áudio. Validar desempenho e comportamento em fontes e proporções diferentes.
4. Revisar vários momentos, especialmente mudanças de cena: rosto inteiro e sem deformação, conteúdo de tela compreensível, legenda e hook sem sobreposição indevida, áudio sincronizado (`quality_gate.evaluate_clip_render`). Comparar com a fonte original; teste de dimensões e SAR não detecta deformação já incorporada ao conteúdo.
5. Testar fontes só de câmera, câmera + tela, câmera que muda de posição ou desaparece e entradas com/sem legenda; validar que o fallback não prejudica vídeos sem split. Exigir revisão humana de uma prévia antes de ativar o layout em publicações (`/api/clip/review`).

## Caso adicional: podcast com várias pessoas — BRKsEDU

**Amostra avaliada:** prévia de 36 s, frames inspecionados em aproximadamente 3, 18 e 30 s.

### Problemas visuais observados

- O layout muda entre close e uma composição com várias pessoas. Em alguns momentos há pessoas parcialmente cortadas nas bordas; a montagem dificulta identificar o foco.
- Um participante aparece duplicado em uma composição quando um corte de 2 pessoas (`SPLIT`) é aplicado sobre um mosaico de 3+ pessoas ou com janelas sobrepostas.
- Um QR code aparece na composição e ocupa espaço que poderia servir ao conteúdo; verificar se é elemento essencial ou publicidade/contexto da fonte antes de ocultá-lo.
- A legenda parece grande em alguns momentos e compete com os rostos e demais elementos.
- Arquivo vertical 1080×1920, pontuação automática e metadados corretos não demonstram boa composição; o score 84 não impede rejeição visual pelo usuário.

### Melhorias para podcast e entrevistas

1. **Classificar layout por trecho:** identificar plano individual, chamada em mosaico e cena de múltiplas pessoas. Detectar mudanças de cena, limites dos painéis e apresentador ativo quando houver sinal confiável. Não aplicar o split de 2 pessoas a cenas com 3+ participantes separados (`split_layout._two_largest`) nem o split câmera + tela cegamente a toda entrevista.
2. **Priorizar falante e preservar contexto:** extrair regiões da fonte original, não da prévia vertical já recortada. Em plano individual, centralizar rosto inteiro e manter enquadramento estável. Com múltiplas pessoas, evitar que o recorte de uma metade invada o rosto vizinho (`split_layout.split_geometry` com `neighbour_gap`) ou usar quadro completo (`reframe_v2.safe_general_content_height`) sem cortar rostos nas bordas.
3. **Evitar duplicação e tratamento indevido:** conferir se dois painéis se sobrepõem na fonte ou exibem a mesma região (`quality_gate.check_panel_duplication`, `panel_layout.has_duplicate_panels`) e não reproduzir o mesmo painel duas vezes por erro de composição. Preservar duplicações intencionais da fonte quando não houver confiança; permitir correção manual.
4. **Elementos de tela:** identificar banners, QR codes e logos (`quality_gate.detect_screen_elements`) como regiões potencialmente removíveis, sem supor que sejam descartáveis. Só ocultar quando não forem essenciais e houver confiança; caso contrário preservar e sinalizar para revisão. Nunca tapar contexto relevante ou rosto com um recorte.
5. **Legendas e hook:** reservar área segura, sem cobrir boca, olhos, nomes ou conteúdo; ajustar tamanho de fonte e posição por layout (`subtitles.layout_caption_adjustments`, `hook_placement.py`), mantendo legibilidade no celular em até duas linhas.
6. **Transições e estabilidade:** usar confirmação temporal e margens para evitar alternância excessiva entre pessoas, zooms bruscos e câmera tremendo (`reframe_v2.smooth_layout_transitions`); preservar cortes naturais. Tratar momentos sem rosto ou falas sobrepostas com fallback para quadro geral/manual.
7. **Seleção editorial:** conferir se o trecho se sustenta sozinho, se começa e termina em frases completas, se o hook representa fielmente o que é dito e se o assunto interessa ao pedido (`clip_selection.evaluate_editorial_quality`). Evitar títulos que transformem opinião ou rumor em fato.

### Critérios de aceitação e testes

- Comparar origem e render em vários pontos, incluindo início/fim e mudanças de layout; verificar rostos completos, ausência de duplicação introduzida, contexto visível e dimensões sem deformação.
- Verificar legendas na reprodução real, áudio compreensível e sincronizado e cortes sem sílabas truncadas. Analisar também ganchos e legendas automáticas contra a fala, não apenas frames.
- Testar ao menos: um falante; dois ou mais participantes; mosaico com apresentador em destaque; falas sobrepostas; rosto parcialmente oculto; fonte com QR/banner; troca de câmera; e vídeo câmera + gameplay. Se confiança baixa, sinalizar para revisão em vez de aprovar automaticamente.
- A aprovação técnica e o score são insuficientes: exigir revisão visual/editorial e aprovação humana de uma prévia antes de publicação.
