"""Automação de tagging AWS PRM (AWS Partner Revenue Measurement).

Pacote estruturado em módulos reutilizáveis para permitir reaproveitamento
nos próximos estágios (automação contínua, varredura recorrente). Cobre a
Etapa 1 (mapeamento, somente-leitura), a Etapa 2a (decisão de tagueamento,
`decision.py`, pura, sem nenhuma chamada de API) e as Etapas 2b/2c
(`tag_execution.py`, dry-run e execução real do tagueamento) — a primeira
chamada de escrita à AWS de todo o pacote só existe na Etapa 2c
(`run_tagging_execution(..., dry_run=False)`).
"""
