# Histórico público dos imóveis da CAIXA

Coleta a lista nacional da opção **Todos**, com as mesmas validações e comparação exata do coletor Windows. O agendamento é reforçado perto do horário de atualização observado.

## Funcionamento

- GitHub Actions em **repositório público**, no runner padrão Ubuntu.
- Entre 22h02 e 23h57 de Brasília: disparos solicitados a cada cinco minutos.
- Fora dessa janela: um disparo solicitado por hora, no minuto 17.
- Execução manual por **Actions → Run workflow**. Os horários efetivos dependem do agendador do GitHub.
- Quando o CSV muda, uma **Release** recebe o CSV original e o JSON de metadados.
- O nome do CSV registra data, hora, microssegundos e fuso do download.
- Antes de cada consulta, restaura o CSV da última release e verifica seu SHA-256.
- Downloads idênticos à última versão são descartados. A → B → A mantém três versões.
- Todas as consultas e erros ficam em **status/execucoes/** e o resultado mais recente em **status/ultima_execucao.json**.
- O resumo também aparece na página do workflow.
- Publicação incompleta permanece como rascunho; nunca vira a versão válida para comparação.

## Configuração necessária

Repositório do projeto: https://github.com/surfmore/caixa-imoveis-historico

O workflow fica em .github/workflows/coletar-caixa.yml. Usa cron em UTC: 2-57/5 nas horas 1 e 2 (22h02–23h57 de Brasília), e minuto 17 nas demais horas. Em atualizações do código do coletor ou do workflow, os testes e uma coleta são executados para validar a publicação. Alterações somente em status/ não disparam outra execução.

Não precisa cadastrar segredo pessoal: usa apenas o GITHUB_TOKEN temporário do próprio repositório, com permissão contents:write. Nenhuma credencial da Cloudflare é necessária.

Se a organização/conta impuser aprovação dos workflows ou de permissões de escrita, autorize somente este repositório. Depois, execute manualmente uma coleta e confira o resultado antes de considerar a migração concluída.

## Custo e limites

Esta configuração não provisiona produtos da Cloudflare nem altera assinaturas ou faturamento. Em repositórios públicos, runners padrão são gratuitos segundo a política atual do GitHub. O workflow é ignorado se o repositório for privado e o script também valida a visibilidade.

As planilhas ficam em Releases. Nenhum arquivo é armazenado como artefato do Actions ou no Git LFS. Cada release contém somente dois arquivos e só é criada quando os bytes diferem da última planilha. As consultas ficam no Git, que usa compactação e deltas para o pequeno registro JSONL.

A gratuidade depende das políticas vigentes e dos limites de uso do GitHub; não constitui garantia de capacidade ilimitada ou de condições comerciais imutáveis. Se houver mudança de política, revise a configuração antes de continuar.

O GitHub pode atrasar ou perder disparos em períodos de alta carga. Em repositórios públicos sem atividade por 60 dias, o agendamento pode ser desativado. A coleta no runner foi validada em 01/11/2026. A CAIXA pode recusar consultas; nesse caso o processo registra erro, sem arquivar HTML.

As coletas Windows existentes permanecem preservadas. O arquivo inicial na nuvem terá seu próprio horário de download. A tarefa Windows e os arquivos locais foram preservados. Não há sincronização automática do histórico local com as Releases.

## Dados e modalidades

Os CSVs guardam o que a CAIXA disponibiliza, com preço, avaliação, identificador, estado e modalidade literal. “Leilão SFI - Edital Único” não permite concluir sozinho se a observação corresponde ao primeiro ou segundo leilão. Não se infere venda quando uma oferta desaparece.

Nenhuma UI foi publicada nesta etapa. Uma UI futura poderá consultar o índice JSON e os CSVs das Releases, ou migrar o armazenamento após revisão explícita de custos.

## Testes

    python -m unittest discover -s tests -v

## Referências

- https://venda-imoveis.caixa.gov.br/sistema/download-lista.asp
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
- https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

## Validação real

Em 01/11/2026, 28 testes passaram no runner Ubuntu do GitHub. Uma planilha com 23.667 imóveis foi publicada em Releases; uma execução posterior confirmou sem_alteracao e manteve somente um CSV.

Última execução validada: https://github.com/surfmore/caixa-imoveis-historico/actions/runs/41349707985

Último resultado: [status/ultima_execucao.json](status/ultima_execucao.json). Planilhas: https://github.com/surfmore/caixa-imoveis-historico/releases

## Janela de atualização observada

Na verificação de 09/11/2026, os históricos local e online continham as mesmas cinco versões, com geração em 30/10 e 03, 04, 05 e 06/11. Os cabeçalhos Last-Modified das quatro alterações indicavam aproximadamente 22h01 de Brasília. Isso é uma observação inicial, não um compromisso de horário da CAIXA.

O reforço de agendamento busca reduzir a demora para detectar a próxima alteração. O GitHub pode atrasar ou descartar disparos; a coleta local tem uma busca independente de minuto em minuto entre 22h e meia-noite. As sondagens locais usam HEAD, baixam o CSV ao detectar mudança e fazem uma confirmação integral a cada dez minutos enquanto aguardam. Depois de preservar uma nova versão, a busca rápida local para de consultar naquele dia. A coleta horária continua.
