#!/usr/bin/env python3
"""
Diagnóstico: carrega uma URL com o mesmo setup do monitor e mostra
o que o Playwright realmente vê — elementos relevantes + screenshot.
"""
import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

URL = "https://www.ticketmaster.com.br/event/venda-geral-bts-world-tour-arirang-28-10"

CTX_ARGS = dict(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    locale="pt-BR",
    timezone_id="America/Sao_Paulo",
)

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(**CTX_ARGS)
        page = await ctx.new_page()
        await stealth_async(page)

        print(f"\nCarregando: {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(5_000)

        # ── URL final após redirects ─────────────────────────────────────────
        print(f"\n[URL final]  {page.url}")

        # ── Título da página ─────────────────────────────────────────────────
        title = await page.title()
        print(f"[Título]     {title}")

        # ── Primeiros 800 chars do body ──────────────────────────────────────
        body = await page.inner_text("body")
        print(f"\n[Body (800 chars)]:\n{body[:800]}\n{'─'*60}")

        # ── Checagem dos seletores do monitor ────────────────────────────────
        checks = {
            "div#picker-bar":                          page.locator("div#picker-bar"),
            "div.event-status.status-soldout":         page.locator("div.event-status.status-soldout"),
            "div#picker-bar .status-soldout":          page.locator("div#picker-bar div.event-status.status-soldout"),
            "button#buyButton":                        page.locator("button#buyButton"),
            "button:has-text('Ingressos') (visível)":  page.locator("button:has-text('Ingressos')"),
            "[class*='event-status'] span":            page.locator("[class*='event-status'] span"),
        }

        print("[Seletores encontrados]")
        for label, loc in checks.items():
            count = await loc.count()
            if count:
                texts = []
                for i in range(min(count, 3)):
                    try:
                        t = (await loc.nth(i).inner_text()).strip()[:60]
                        texts.append(repr(t))
                    except Exception:
                        pass
                print(f"  ✓ {label:50s} → {count}x  {', '.join(texts)}")
            else:
                print(f"  ✗ {label}")

        # ── Screenshot ───────────────────────────────────────────────────────
        await page.screenshot(path="/mnt/c/debug_screenshot.png", full_page=False)
        print("\n[Screenshot salvo em C:\\debug_screenshot.png]")

        await browser.close()

asyncio.run(main())
