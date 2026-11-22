---
status: accepted
type: business-rule
---

# 8. A Gupy recebe o texto dos campos, não o PDF

## Context

Na Gupy a candidatura é feita no perfil, em campos digitados: Experiência (empresa, cargo, início, fim ou atual,
descrição) e Habilidades (até 30, visto no formulário). Não há campo de projetos. A IA da Gupy ordena só por
esses campos e não lê "informações fora dos campos estruturados"; o PDF passa por um parser que a própria Gupy
diz não ser 100%. Nas outras fontes (Workable, sistemas do Remotar), o arquivo de currículo é o que conta.

## Decision

Vaga da Gupy leva só o anexo `Gupy-<empresa>.txt`, sem PDF: um bloco por item de Experiência, pronto para digitar,
e as 30 habilidades mais ligadas à vaga. Todo projeto entra como item de Experiência, com rótulo honesto
(freelance com o cliente, projeto pessoal como "Projeto próprio: <nome>").

## Alternatives

- **PDF e texto juntos:** confundia qual dos dois usar, e a IA da Gupy não lê o PDF.
- **Projetos no "Apresente-se":** campo livre de até 1.500 caracteres, fora da ordenação.
- **Só os projetos que o PDF mantém:** menos prova no único campo que a ordenação lê.

## Trade-offs

- **We gain:** o texto vai direto para os campos que decidem a posição na Gupy.
- **We pay:** se o formulário pedir um arquivo, vale o currículo base; não dá para medir de fora se a Gupy soma
  projeto como anos de experiência (ela não mostra a posição na lista).
- **The rejected option would cost:** um PDF que a ordenação ignora, ou projetos num campo que ela não lê.

## Consequences

- Projeto sem `inicio` pede para preencher em vez de inventar a data; sem `fim`, é atual.
- A segunda nota de uma vaga da Gupy é dada contra esse texto (sem resumo, todos os projetos, 30 habilidades).
- Rever se a Gupy passar a ler o PDF ou ganhar um campo de projetos.
