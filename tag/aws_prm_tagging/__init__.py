"""Automação de tagging AWS PRM (AWS Partner Revenue Measurement).

Pacote estruturado em módulos reutilizáveis para permitir reaproveitamento
nos próximos estágios (tagueamento inicial, automação contínua, varredura
recorrente). Cobre a Etapa 1 (mapeamento, somente-leitura) e a Etapa 2a
(decisão de tagueamento, `decision.py`, pura) — nenhum dos dois faz nenhuma
chamada de escrita à AWS.
"""
