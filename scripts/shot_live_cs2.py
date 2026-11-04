"""Проверяем лайв CS2: видно ли счёт, можно ли ставить."""

import asyncio
import json

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)

        await page.goto("http://127.0.0.1:8099/", wait_until="networkidle")
        await page.click('#matchSegments .seg[data-segment="live"]')
        await page.wait_for_timeout(900)
        await page.screenshot(path="/home/user/shots/93-live-cs2.png", full_page=True)

        card = page.locator("#matches .card").first
        print("карточка:", (await card.inner_text()).replace("\n", " | "))
        print("табло (scoreline):", await card.locator(".scoreline").count())
        print("строки карт:", await card.locator(".maprow").all_inner_texts())

        # пробуем поставить на исход матча
        btn = card.locator(".team button").first
        print("кнопка исхода disabled:", await btn.is_disabled())
        await btn.click()
        await page.wait_for_timeout(600)
        await page.screenshot(path="/home/user/shots/94-live-bet-sheet.png")

        stake = page.locator('#sheet input[type="number"]').first
        await stake.fill("50")
        await page.wait_for_timeout(200)
        texts = await page.locator("#sheet button").all_inner_texts()
        print("кнопки в шторке:", [t.replace("\n", " ") for t in texts][:6])
        confirm = page.locator("#sheet button", has_text="Поставить").first
        await confirm.click()
        await page.wait_for_timeout(1500)
        body = await page.locator("body").inner_text()
        marker = "не" if "Недостаточно" in body or "закрыт" in body else ""
        print("после ставки есть ошибка?" , marker or "нет")
        print("баланс на экране:", await page.locator("#balance").inner_text() if await page.locator("#balance").count() else "?")
        await page.screenshot(path="/home/user/shots/95-live-bet-result.png")
        print("ошибки в консоли:", [e for e in errors if "favicon" not in e][:5])
        await browser.close()


asyncio.run(main())
