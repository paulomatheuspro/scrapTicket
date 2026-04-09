"""
Configuração do monitor de ingressos.
"""

# Intervalo de verificação em segundos
CHECK_INTERVAL = 5

# Tipos de ingresso a ignorar nas notificações (deixe vazio para notificar todos)
IGNORE_TICKET_TYPES: list[str] = []

# ── Páginas de landing (listam múltiplas datas) ──────────────────────────────
# O bot varre todos os cards, entra nos disponíveis e verifica os setores.
LANDING_PAGES = [
    {
        "name": "BTS World Tour Arirang",
        "url": "https://www.ticketmaster.com.br/event/bts-world-tour-arirang",
    },
]

# ── Páginas de evento direto (pré-vendas, datas específicas) ─────────────────
# O bot verifica se o dropdown da data está como 'agotado' ou não.
DIRECT_EVENTS = [
    {
        "name": "Pré-venda ARMY - 28/10",
        "url": "https://www.ticketmaster.com.br/event/pre-venda-army-membership-bts-world-tour-arirang-28-10",
    },
    {
        "name": "Pré-venda ARMY - 30/10",
        "url": "https://www.ticketmaster.com.br/event/pre-venda-army-membership-bts-world-tour-arirang-30-10",
    },
    {
        "name": "Pré-venda ARMY - 31/10",
        "url": "https://www.ticketmaster.com.br/event/pre-venda-army-membership-bts-world-tour-arirang-31-10",
    },
]
