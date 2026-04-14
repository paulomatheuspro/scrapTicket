#!/usr/bin/env python3
"""
Monitor de ingressos da Ticketmaster Brasil com notificação via Telegram.
Usa Playwright async + asyncio.gather para checar todas as URLs em paralelo.
"""

import os
import re
import asyncio
import logging
import json
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Page, Browser
from playwright_stealth import stealth_async
from dotenv import load_dotenv
from config import CHECK_INTERVAL, LANDING_PAGES, DIRECT_EVENTS, IGNORE_TICKET_TYPES

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_IDS", os.getenv("TELEGRAM_CHAT_ID", "")).split(",") if cid.strip()]
TELEGRAM_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ID pessoal — recebe alertas de erro, página offline e elementos inesperados
PERSONAL_CHAT_ID = "1938486252"

checagens = 0
inicio = datetime.now()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CTX_ARGS = dict(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    locale="pt-BR",
    timezone_id="America/Sao_Paulo",
)


# ── Telegram via browser fetch() ──────────────────────────────────────────────
# APIRequestContext e requests não chegam ao Telegram no WSL.
# Usamos page.evaluate() com fetch() do Chromium, que usa o stack de rede completo.

_FETCH_JS = """
    async ([url, options, timeoutMs]) => {
        const ms = timeoutMs || 10000;
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), ms);
        try {
            const resp = await fetch(url, {...(options || {}), signal: ctrl.signal});
            clearTimeout(timer);
            return await resp.json();
        } catch(e) {
            clearTimeout(timer);
            return {ok: false, error: e.message};
        }
    }
"""

async def tg_post(tg_page: Page, endpoint: str, payload: dict) -> dict:
    url = f"{TELEGRAM_BASE}/{endpoint}"
    try:
        result = await tg_page.evaluate(_FETCH_JS, [url, {
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload),
        }])
        return result or {}
    except Exception as e:
        log.warning(f"tg_post {endpoint} falhou: {e}")
        return {}


async def tg_get(tg_page: Page, endpoint: str, params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{TELEGRAM_BASE}/{endpoint}?{query}"
    try:
        result = await tg_page.evaluate(_FETCH_JS, [url, None])
        return result or {}
    except Exception as e:
        log.warning(f"tg_get {endpoint} falhou: {e}")
        return {}


async def send_telegram(tg_page: Page, message: str, chat_id: str | None = None) -> bool:
    targets = [chat_id] if chat_id else TELEGRAM_CHAT_IDS
    success = True
    for cid in targets:
        result = await tg_post(tg_page, "sendMessage", {
            "chat_id": cid,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
        if not result.get("ok"):
            log.error(f"Telegram erro (chat {cid}): {result.get('description')}")
            success = False
    return success


async def notify_personal(tg_page: Page, message: str) -> None:
    """Envia alerta de erro/inesperado apenas para o ID pessoal."""
    await send_telegram(tg_page, message, chat_id=PERSONAL_CHAT_ID)


def _build_status_msg() -> str:
    uptime = datetime.now() - inicio
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return (
        f"<b>📊 Monitor ativo</b>\n"
        f"Ciclos: <b>{checagens}</b>\n"
        f"Rodando há: <b>{h}h {m}m {s}s</b>\n"
        f"Iniciado: {inicio.strftime('%d/%m/%Y %H:%M:%S')}"
    )


async def send_status(tg_page: Page) -> None:
    """Envia relatório de status periódico para todos os chats."""
    msg = _build_status_msg()
    await send_telegram(tg_page, msg)
    log.info(f"Status periódico enviado ({checagens} ciclos)")


async def commands_loop(tg_page: Page) -> None:
    """Polling de comandos Telegram a cada 3s. Responde /s com status do monitor."""
    offset = 0
    while True:
        try:
            data = await tg_get(tg_page, "getUpdates", {
                "offset": str(offset),
                "limit": "10",
                "timeout": "0",
            })
            for upd in (data.get("result") or []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not chat_id:
                    continue
                # aceita /s e /s@NomeDoBot
                if text == "/s" or text.lower().startswith("/s@"):
                    reply = _build_status_msg()
                    await send_telegram(tg_page, reply, chat_id=chat_id)
                    log.info(f"Comando /s respondido para chat {chat_id} ({checagens} ciclos)")
        except Exception as e:
            log.warning(f"commands_loop erro: {e}")
        await asyncio.sleep(3)


# ── Detecção de setores ───────────────────────────────────────────────────────

async def get_sectors(page: Page) -> list[dict]:
    btn = page.locator(".btn.btn-primary:visible").first
    if await btn.count():
        try:
            await btn.click()
            await page.wait_for_timeout(3_000)
        except Exception:
            pass

    page_text = await page.inner_text("body")
    pattern = re.compile(
        r"(pista|soundcheck\s+pacote\s+vip|cadeira\s+inferior|cadeira\s+superior|arquibancada)"
        r"(?:[\s\S]{0,120}?)(a partir de R\$\s*[\d.,]+(?:\s*\+\s*R\$\s*[\d.,]+)?)?",
        re.IGNORECASE,
    )
    seen: set[str] = set()
    sectors: list[dict] = []
    for m in pattern.finditer(page_text):
        name = m.group(1).strip().title()
        if IGNORE_TICKET_TYPES and any(ig.lower() in name.lower() for ig in IGNORE_TICKET_TYPES):
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        price_raw = m.group(2) or ""
        price_str = f" — {price_raw.strip()}" if price_raw.strip() else ""
        sectors.append({"name": name, "price": price_str})
    return sectors


# ── Verificação de evento direto ──────────────────────────────────────────────

async def check_direct_event(browser: Browser, name: str, url: str) -> tuple[str, list[dict]]:
    """
    Retorna (status, sectors) onde status é um de:
      'soldout'   — div#picker-bar div.event-status.status-soldout presente
      'available' — soldout ausente E botão 'Ingressos' / button#buyButton visível
      'unknown'   — soldout ausente MAS nenhum botão reconhecido encontrado
      'error'     — timeout ou falha ao carregar a página
    """
    ctx = await browser.new_context(**CTX_ARGS)
    try:
        page = await ctx.new_page()
        await stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(5_000)
        except PWTimeout:
            log.error(f"  [{name}] Timeout")
            return "error", []

        # ── Camada 1: picker-bar com status-soldout (elemento primário) ──────────
        soldout_el = page.locator("div#picker-bar div.event-status.status-soldout")
        if await soldout_el.count() > 0:
            log.info(f"  [{name}] ESGOTADO (picker-bar status-soldout)")
            return "soldout", []

        # ── Camada 2: qualquer elemento de status com texto "esgotado" ──────────
        status_spans = page.locator("[class*='event-status'] span, [class*='status-soldout'] span")
        for i in range(await status_spans.count()):
            txt = (await status_spans.nth(i).inner_text()).strip().lower()
            if "esgotado" in txt:
                log.info(f"  [{name}] ESGOTADO (status span fallback)")
                return "soldout", []

        # ── Camada 3: texto bruto do início da página ────────────────────────────
        body_text = (await page.inner_text("body")).lower()
        if "esgotado" in body_text[:1500]:
            log.info(f"  [{name}] ESGOTADO (texto body)")
            return "soldout", []

        # ── Camada 4: confirmação positiva — botão Ingressos visível ────────────
        ingressos_loc = page.locator(
            "button#buyButton, "
            "button:has-text('Ingressos'), "
            "a:has-text('Ingressos')"
        )
        has_ingressos = False
        for i in range(await ingressos_loc.count()):
            if await ingressos_loc.nth(i).is_visible():
                has_ingressos = True
                break

        if has_ingressos:
            log.info(f"  [{name}] DISPONÍVEL — botão 'Ingressos' encontrado")
            sectors = await get_sectors(page)
            return "available", sectors or [{"name": "Verificar setores no link", "price": ""}]

        # ── Camada 5: página com conteúdo real mas sem elementos de compra ───────
        # Indica pré-venda não iniciada ("soon") — sem alarme.
        # Só marca "unknown" se a página parece quebrada/bloqueada (conteúdo mínimo).
        REAL_CONTENT_SIGNALS = ["r$", "morumbi", "ingresso", "classificação", "portões"]
        page_seems_real = (
            len(body_text) > 400
            and any(sig in body_text[:3000] for sig in REAL_CONTENT_SIGNALS)
        )
        if page_seems_real:
            log.info(f"  [{name}] Pré-venda não iniciada (sem botão de compra) — aguardando")
            return "soon", []

        log.warning(f"  [{name}] Página suspeita/bloqueada — conteúdo não reconhecido")
        return "unknown", []
    finally:
        await ctx.close()


# ── Verificação de landing page ───────────────────────────────────────────────

_AVAILABLE_KEYWORDS = ["À VENDA", "A VENDA", "DISPONÍVEL", "DISPONIVEL", "INGRESSOS"]

def _link_is_soldout(link_text: str) -> bool:
    return "ESGOTADO" in link_text.upper()

def _link_is_available(link_text: str) -> bool:
    upper = link_text.upper()
    return any(kw in upper for kw in _AVAILABLE_KEYWORDS)


def _error_entry(event_url: str, reason: str) -> dict:
    return {"event_url": event_url, "date": "", "title": "", "status": "error", "reason": reason, "link_text": "", "sectors": []}


async def check_landing_page(browser: Browser, name: str, landing_url: str) -> list[dict]:
    """
    Para cada card .tmpe-ticket-item verifica dois indicadores de esgotado:
      1. a.tmpe-link-details com texto 'ESGOTADO'
      2. .tmpe-status-badge contendo .tmpe-dot-soldout

    Status por card:
      esgotado  — qualquer indicador acima presente
      available — nenhum indicador de esgotado E link exibe 'À VENDA' / 'DISPONÍVEL' / 'INGRESSOS'
      unknown   — nenhum indicador de esgotado mas texto do link não reconhecido
      error     — timeout ou sem cards (entrada única representando a landing inteira)
    """
    ctx = await browser.new_context(**CTX_ARGS)
    try:
        page = await ctx.new_page()
        await stealth_async(page)
        try:
            await page.goto(landing_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(4_000)
        except PWTimeout:
            log.error(f"Timeout landing {name}")
            return [_error_entry(landing_url, "timeout")]

        desktop = page.locator(".tmpe-desktop-view")
        container = desktop if await desktop.count() else page.locator("body")
        cards = await container.locator(".tmpe-ticket-item").all()

        if not cards:
            log.warning(f"Nenhum card encontrado na landing page: {name}")
            return [_error_entry(landing_url, "no_cards")]

        results = []
        for card in cards:
            date_txt = (await card.locator(".tmpe-date-text").inner_text()).strip() if await card.locator(".tmpe-date-text").count() else "?"
            title_txt = (await card.locator(".tmpe-ticket-title").inner_text()).strip() if await card.locator(".tmpe-ticket-title").count() else ""

            link = card.locator("a.tmpe-link-details").first
            link_count = await link.count()
            event_url = (await link.get_attribute("href") or landing_url) if link_count else landing_url
            link_text = (await link.inner_text()).strip() if link_count else ""

            # Indicador 1: texto do link
            soldout_by_link = _link_is_soldout(link_text)

            # Indicador 2: badge com dot esgotado
            soldout_by_badge = await card.locator(".tmpe-status-badge .tmpe-dot-soldout").count() > 0

            if soldout_by_link or soldout_by_badge:
                reason = "link" if soldout_by_link else "badge"
                log.info(f"  [{title_txt} {date_txt}] ESGOTADO ({reason})")
                results.append({
                    "event_url": event_url, "date": date_txt, "title": title_txt,
                    "status": "esgotado", "link_text": link_text, "sectors": [],
                })
                continue

            if not _link_is_available(link_text):
                log.warning(f"  [{title_txt} {date_txt}] Label inesperada: '{link_text}'")
                results.append({
                    "event_url": event_url, "date": date_txt, "title": title_txt,
                    "status": "unknown", "link_text": link_text, "sectors": [],
                })
                continue

            log.info(f"  [{title_txt} {date_txt}] DISPONÍVEL ('{link_text}') — verificando setores...")
            sectors: list[dict] = []
            try:
                await page.goto(event_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3_500)
                sectors = await get_sectors(page)
                log.info(f"     Setores: {[s['name'] for s in sectors] or 'nenhum'}")
                await page.goto(landing_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3_000)
            except PWTimeout:
                log.error(f"  Timeout ao entrar em {event_url}")

            results.append({
                "event_url": event_url, "date": date_txt, "title": title_txt,
                "status": "available", "link_text": link_text, "sectors": sectors,
            })

        return results
    finally:
        await ctx.close()


# ── Notificações ──────────────────────────────────────────────────────────────

async def notify_direct(tg_page: Page, name: str, url: str, sectors: list[dict]) -> None:
    lines = [
        "<b>🎟 INGRESSOS DISPONÍVEIS!</b>",
        f"<b>Evento:</b> {name}",
        f"<b>Link:</b> {url}",
        "",
        "<b>Setores disponíveis:</b>",
    ]
    for s in sectors:
        lines.append(f"  • {s['name']}{s['price']}")
    lines.append(f"\n<i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>")
    await send_telegram(tg_page, "\n".join(lines))


async def notify_landing(tg_page: Page, landing_name: str, events: list[dict]) -> None:
    lines = ["<b>🎟 INGRESSOS DISPONÍVEIS!</b>", f"<b>Show:</b> {landing_name}", ""]
    for ev in events:
        lines.append(f"<b>📅 {ev['date']} — {ev['title']}</b>")
        lines.append(f"<b>Link:</b> {ev['event_url']}")
        if ev["sectors"]:
            lines.append("<b>Setores:</b>")
            for s in ev["sectors"]:
                lines.append(f"  • {s['name']}{s['price']}")
        else:
            lines.append("  Setores: verificar no link")
        lines.append("")
    lines.append(f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>")
    await send_telegram(tg_page, "\n".join(lines))


# ── Loop principal ─────────────────────────────────────────────────────────────

async def main():
    global checagens

    log.info("Monitor de ingressos iniciado.")
    log.info(f"Landing pages: {len(LANDING_PAGES)} | Eventos diretos: {len(DIRECT_EVENTS)}")
    log.info(f"Intervalo: {CHECK_INTERVAL}s | Paralelismo: asyncio.gather\n")

    notified_direct: set[str] = set()
    card_states: dict[str, str] = {}
    notified_landing: set[str] = set()
    alerted_errors: set[str] = set()   # evita spam de alertas de erro repetidos

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        # Página dedicada ao Telegram — usa fetch() do browser para sendMessage
        tg_ctx = await browser.new_context(**CTX_ARGS)
        tg_page = await tg_ctx.new_page()
        await stealth_async(tg_page)
        # Navega para google.com — fetch() cross-origin ao Telegram funciona a partir daqui no WSL
        await tg_page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=15_000)

        log.info("Telegram pronto. Buscando chat IDs recentes via getUpdates...")
        updates = await tg_get(tg_page, "getUpdates", {"limit": "20", "timeout": "0"})
        seen_chats: set[str] = set()
        for upd in (updates.get("result") or []):
            chat = (upd.get("message") or upd.get("my_chat_member") or {}).get("chat", {})
            cid = str(chat.get("id", ""))
            ctype = chat.get("type", "")
            ctitle = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            if cid and cid not in seen_chats:
                seen_chats.add(cid)
                log.info(f"  Chat encontrado → id={cid}  tipo={ctype}  nome='{ctitle}'")
        if not seen_chats:
            log.info("  Nenhuma conversa recente. Adicione o bot a um grupo e envie uma mensagem, depois reinicie.")
        log.info("")

        # Registra /s no menu de comandos do Telegram
        await tg_post(tg_page, "setMyCommands", {
            "commands": [{"command": "s", "description": "Status do monitor (ciclos e uptime)"}]
        })
        log.info("Comando /s registrado no Telegram.\n")

        STATUS_INTERVAL = 30 * 60  # segundos

        async def status_loop():
            """Envia status a cada 30 minutos."""
            await asyncio.sleep(STATUS_INTERVAL)
            while True:
                await send_status(tg_page)
                await asyncio.sleep(STATUS_INTERVAL)

        poller = asyncio.create_task(status_loop())
        cmd_poller = asyncio.create_task(commands_loop(tg_page))

        try:
            while True:
                checagens += 1
                log.info(f"── Ciclo #{checagens} {'─' * 40}")

                # Dispara todas as checagens em paralelo
                tasks = [check_landing_page(browser, lp["name"], lp["url"]) for lp in LANDING_PAGES]
                tasks += [check_direct_event(browser, ev["name"], ev["url"]) for ev in DIRECT_EVENTS]

                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Processa resultados das landing pages
                for i, lp in enumerate(LANDING_PAGES):
                    result = results[i]
                    lp_url = lp["url"]
                    lp_name = lp["name"]

                    if isinstance(result, Exception):
                        log.error(f"Erro inesperado na landing {lp_name}: {result}")
                        if lp_url not in alerted_errors:
                            await notify_personal(tg_page, (
                                f"⚠️ <b>Erro inesperado — {lp_name}</b>\n"
                                f"{result}\n"
                                f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>"
                            ))
                            alerted_errors.add(lp_url)
                        continue

                    # Resultado bem-sucedido: limpa erro de nível de página
                    if result and result[0]["status"] != "error":
                        alerted_errors.discard(lp_url)

                    new_available: list[dict] = []
                    for card in result:
                        ev_url = card["event_url"]
                        curr = card["status"]
                        prev = card_states.get(ev_url)

                        if prev and prev != curr:
                            log.info(f"  MUDANÇA: {card['title']} {card['date']}: {prev.upper()} → {curr.upper()}")
                        card_states[ev_url] = curr

                        if curr == "available":
                            alerted_errors.discard(ev_url)
                            if ev_url not in notified_landing:
                                new_available.append(card)

                        elif curr in ("error", "unknown"):
                            notified_landing.discard(ev_url)
                            if ev_url not in alerted_errors:
                                if curr == "error":
                                    reason = card.get("reason", "")
                                    detail = "página offline ou timeout" if reason == "timeout" else "nenhum card encontrado na página"
                                    label = lp_name
                                else:
                                    label_txt = card.get("link_text", "") or "(vazio)"
                                    detail = f"label inesperada: <code>{label_txt}</code>"
                                    label = f"{card['title']} {card['date']}".strip() or lp_name
                                await notify_personal(tg_page, (
                                    f"⚠️ <b>{lp_name}</b> — {label}\n"
                                    f"{detail}\n"
                                    f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>"
                                ))
                                alerted_errors.add(ev_url)

                        else:  # esgotado
                            notified_landing.discard(ev_url)
                            alerted_errors.discard(ev_url)

                    if new_available:
                        log.info(f"  {len(new_available)} data(s) disponível(is)! Notificando...")
                        await notify_landing(tg_page, lp_name, new_available)
                        for card in new_available:
                            notified_landing.add(card["event_url"])

                # Processa resultados dos eventos diretos
                for j, ev in enumerate(DIRECT_EVENTS):
                    result = results[len(LANDING_PAGES) + j]
                    ev_url = ev["url"]
                    ev_name = ev["name"]

                    if isinstance(result, Exception):
                        log.error(f"Erro inesperado no evento {ev_name}: {result}")
                        if ev_url not in alerted_errors:
                            await notify_personal(tg_page, (
                                f"⚠️ <b>Erro inesperado — {ev_name}</b>\n"
                                f"{result}\n"
                                f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>"
                            ))
                            alerted_errors.add(ev_url)
                        continue

                    status, sectors = result

                    if status in ("soldout", "soon"):
                        if ev_url in notified_direct:
                            log.info(f"  [{ev_name}] Voltou a ficar indisponível.")
                        notified_direct.discard(ev_url)
                        alerted_errors.discard(ev_url)

                    elif status == "available":
                        alerted_errors.discard(ev_url)
                        log.info(f"  [{ev_name}] DISPONÍVEL! Setores: {[s['name'] for s in sectors]}")
                        if ev_url not in notified_direct:
                            await notify_direct(tg_page, ev_name, ev_url, sectors)
                            log.info(f"  -> Telegram enviado.")
                            notified_direct.add(ev_url)
                        else:
                            log.info(f"  -> (sem mudança, Telegram já enviado)")

                    elif status in ("error", "unknown"):
                        notified_direct.discard(ev_url)
                        if ev_url not in alerted_errors:
                            detail = (
                                "página offline ou timeout"
                                if status == "error"
                                else "nenhum elemento reconhecido (nem esgotado nem Ingressos)"
                            )
                            await notify_personal(tg_page, (
                                f"⚠️ <b>{ev_name}</b>\n"
                                f"{detail}\n"
                                f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>"
                            ))
                            alerted_errors.add(ev_url)
                            log.warning(f"  [{ev_name}] {status.upper()} → alerta pessoal enviado")

                log.info(f"Próxima verificação em {CHECK_INTERVAL}s...\n")
                await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.CancelledError:
            pass
        finally:
            poller.cancel()
            cmd_poller.cancel()
            await tg_ctx.close()
            await browser.close()


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN não configurado no .env")
        exit(1)
    if not TELEGRAM_CHAT_IDS:
        log.warning("TELEGRAM_CHAT_IDS vazio — notificações de disponibilidade não serão enviadas. Aguardando descoberta de IDs via getUpdates.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrompido pelo usuário.")
