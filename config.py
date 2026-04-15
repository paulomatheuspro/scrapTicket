"""
Configuração do monitor de ingressos.
"""

# Intervalo de verificação em segundos
CHECK_INTERVAL = 5

# Tipos de ingresso a ignorar nas notificações (deixe vazio para notificar todos)
IGNORE_TICKET_TYPES: list[str] = []

# ── Páginas de landing (listam múltiplas datas) ──────────────────────────────
# Detecção: link a.tmpe-link-details com texto "ESGOTADO" OU badge tmpe-dot-soldout.
# Notifica quando qualquer card muda para 'À VENDA', 'DISPONÍVEL' ou 'INGRESSOS'.
LANDING_PAGES = [
    {
        "name": "BTS World Tour Arirang",
        "url": "https://www.ticketmaster.com.br/event/bts-world-tour-arirang",
    },
]

# ── Páginas de evento direto (datas específicas da venda geral) ───────────────
# Detecção: div.event-picker div.event-status.status-soldout desaparece
#           E surge button/link com texto "Ingressos".
DIRECT_EVENTS = [
    {
        "name": "Venda Geral - 28/10",
        "url": "https://www.ticketmaster.com.br/event/venda-geral-bts-world-tour-arirang-28-10",
    },
    {
        "name": "Venda Geral - 30/10",
        "url": "https://www.ticketmaster.com.br/event/venda-geral-bts-world-tour-arirang-30-10",
    },
    {
        "name": "Venda Geral - 31/10",
        "url": "https://www.ticketmaster.com.br/event/venda-geral-bts-world-tour-arirang-31-10",
    },
]
