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


async def send_status(tg_page: Page) -> None:
    """Envia relatório de status periódico para todos os chats."""
    uptime = datetime.now() - inicio
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    msg = (
        f"<b>📊 Monitor ativo</b>\n"
        f"Checagens: <b>{checagens}</b>\n"
        f"Rodando há: <b>{h}h {m}m {s}s</b>\n"
        f"Iniciado: {inicio.strftime('%d/%m/%Y %H:%M:%S')}"
    )
    await send_telegram(tg_page, msg)
    log.info(f"Status periódico enviado ({checagens} checagens, {h}h{m}m{s}s)")


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

async def check_direct_event(browser: Browser, name: str, url: str) -> tuple[bool, list[dict]]:
    ctx = await browser.new_context(**CTX_ARGS)
    try:
        page = await ctx.new_page()
        await stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(5_000)
        except PWTimeout:
            log.error(f"  [{name}] Timeout")
            return False, []

        # Padrão 1: dropdown
        dropdown_btn = page.locator(".btn.dropdown-toggle.op-ini").first
        if await dropdown_btn.count():
            await dropdown_btn.click()
            await page.wait_for_timeout(800)
            first_item = page.locator(".dropdown-menu li").first
            if await first_item.count():
                cls = await first_item.get_attribute("class") or ""
                txt = (await first_item.inner_text()).strip().lower()
                is_soldout = "agotado" in cls or txt.startswith("esgotado")
                await page.keyboard.press("Escape")
                if is_soldout:
                    log.info(f"  [{name}] ESGOTADO (dropdown)")
                    return False, []
            else:
                await page.keyboard.press("Escape")

        # Padrão 2: card de status
        status_el = page.locator("[class*='event-status']").first
        if await status_el.count():
            if "status-soldout" in (await status_el.get_attribute("class") or ""):
                log.info(f"  [{name}] ESGOTADO (event-status)")
                return False, []

        body_text = (await page.inner_text("body")).lower()

        # Texto esgotado no topo da página
        if "esgotado" in body_text[:600]:
            log.info(f"  [{name}] ESGOTADO (texto)")
            return False, []

        # Confirmação positiva: só reporta disponível se houver botão de compra visível
        # ou dropdown sem esgotado. Sem confirmação → assume indisponível (evita falso alarme).
        has_buy_btn = await page.locator(".btn.btn-primary:visible").count() > 0
        has_dropdown = await page.locator(".btn.dropdown-toggle.op-ini").count() > 0
        if not has_buy_btn and not has_dropdown:
            log.info(f"  [{name}] Página sem elementos de compra — assumindo indisponível")
            return False, []

        sectors = await get_sectors(page)
        return True, sectors or [{"name": "Verificar setores no link", "price": ""}]
    finally:
        await ctx.close()


# ── Verificação de landing page ───────────────────────────────────────────────

async def check_landing_page(browser: Browser, landing_url: str) -> list[dict]:
    ctx = await browser.new_context(**CTX_ARGS)
    try:
        page = await ctx.new_page()
        await stealth_async(page)
        try:
            await page.goto(landing_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(4_000)
        except PWTimeout:
            log.error(f"Timeout landing {landing_url}")
            return []

        desktop = page.locator(".tmpe-desktop-view")
        container = desktop if await desktop.count() else page.locator("body")
        cards = await container.locator(".tmpe-ticket-item").all()

        if not cards:
            log.warning("Nenhum card encontrado na landing page.")
            return []

        results = []
        for card in cards:
            dot = card.locator(".tmpe-status-dot").first
            dot_cls = await dot.get_attribute("class") or "" if await dot.count() else ""
            date_txt = (await card.locator(".tmpe-date-text").inner_text()).strip() if await card.locator(".tmpe-date-text").count() else "?"
            title_txt = (await card.locator(".tmpe-ticket-title").inner_text()).strip() if await card.locator(".tmpe-ticket-title").count() else ""
            link = card.locator("a.tmpe-link-details").first
            event_url = await link.get_attribute("href") or landing_url if await link.count() else landing_url

            if "tmpe-dot-soldout" in dot_cls:
                log.info(f"  [{title_txt} {date_txt}] ESGOTADO")
                results.append({"event_url": event_url, "date": date_txt, "title": title_txt, "status": "esgotado", "sectors": []})
                continue

            if "tmpe-dot-soon" in dot_cls:
                log.info(f"  [{title_txt} {date_txt}] EM BREVE")
                results.append({"event_url": event_url, "date": date_txt, "title": title_txt, "status": "em_breve", "sectors": []})
                continue

            log.info(f"  [{title_txt} {date_txt}] DISPONÍVEL — verificando setores...")
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

            results.append({"event_url": event_url, "date": date_txt, "title": title_txt, "status": "available", "sectors": sectors})

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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        # Página dedicada ao Telegram — usa fetch() do browser para sendMessage
        tg_ctx = await browser.new_context(**CTX_ARGS)
        tg_page = await tg_ctx.new_page()
        await stealth_async(tg_page)
        # Navega para google.com — fetch() cross-origin ao Telegram funciona a partir daqui no WSL
        await tg_page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=15_000)

        log.info("Telegram pronto. Iniciando monitoramento...\n")

        # getUpdates não funciona neste ambiente WSL — usamos relatório periódico a cada 30min
        STATUS_INTERVAL = 30 * 60  # segundos

        async def status_loop():
            """Envia status a cada 30 minutos."""
            await asyncio.sleep(STATUS_INTERVAL)
            while True:
                await send_status(tg_page)
                await asyncio.sleep(STATUS_INTERVAL)

        poller = asyncio.create_task(status_loop())

        try:
            while True:
                checagens += 1
                log.info(f"── Ciclo #{checagens} {'─' * 40}")

                # Dispara todas as checagens em paralelo
                tasks = [check_landing_page(browser, lp["url"]) for lp in LANDING_PAGES]
                tasks += [check_direct_event(browser, ev["name"], ev["url"]) for ev in DIRECT_EVENTS]

                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Processa resultados das landing pages
                for i, lp in enumerate(LANDING_PAGES):
                    result = results[i]
                    if isinstance(result, Exception):
                        log.error(f"Erro na landing {lp['name']}: {result}")
                        continue
                    new_available: list[dict] = []
                    for card in result:
                        ev_url = card["event_url"]
                        prev = card_states.get(ev_url)
                        curr = card["status"]
                        if prev and prev != curr:
                            log.info(f"  MUDANÇA: {card['title']} {card['date']}: {prev.upper()} → {curr.upper()}")
                        card_states[ev_url] = curr
                        if curr != "available":
                            notified_landing.discard(ev_url)
                            continue
                        if ev_url not in notified_landing:
                            new_available.append(card)
                    if new_available:
                        log.info(f"  {len(new_available)} data(s) disponível(is)! Notificando...")
                        await notify_landing(tg_page, lp["name"], new_available)
                        for card in new_available:
                            notified_landing.add(card["event_url"])

                # Processa resultados dos eventos diretos
                for j, ev in enumerate(DIRECT_EVENTS):
                    result = results[len(LANDING_PAGES) + j]
                    if isinstance(result, Exception):
                        log.error(f"Erro no evento {ev['name']}: {result}")
                        continue
                    available, sectors = result
                    url = ev["url"]
                    name = ev["name"]
                    if not available:
                        if url in notified_direct:
                            log.info(f"  [{name}] Voltou a ficar indisponível.")
                        notified_direct.discard(url)
                    else:
                        log.info(f"  [{name}] DISPONÍVEL! Setores: {[s['name'] for s in sectors]}")
                        if url not in notified_direct:
                            await notify_direct(tg_page, name, url, sectors)
                            log.info(f"  -> Telegram enviado.")
                            notified_direct.add(url)
                        else:
                            log.info(f"  -> (sem mudança, Telegram já enviado)")

                log.info(f"Próxima verificação em {CHECK_INTERVAL}s...\n")
                await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.CancelledError:
            pass
        finally:
            poller.cancel()
            await tg_ctx.close()
            await browser.close()


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        log.error("Configuração incompleta no .env")
        exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrompido pelo usuário.")
