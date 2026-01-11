import asyncio
import re
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


TARGET_URL = "https://m.a-bly.com/screens?screen_name=SUB_CATEGORY_DEPARTMENT&next_token=eyJsIjogIkRlcGFydG1lbnRDYXRlZ29yeVJlYWx0aW1lUmFua0dlbmVyYXRvciIsICJwIjogeyJkZXBhcnRtZW50X3R5cGUiOiAiQ0FURUdPUlkiLCAiY2F0ZWdvcnlfc25vIjogMjkzfSwgImQiOiAiQ0FURUdPUlkiLCAicHJldmlvdXNfc2NyZWVuX25hbWUiOiAiT1ZFUlZJRVciLCAiY2F0ZWdvcnlfc25vIjogMjkzfQ%3D%3D&category_list%5B%5D=293&sorting_type=POPULAR"
MIN_PURCHASE_COUNT = 2000
MAX_PRODUCTS = 10


def parse_purchase_count(text: str) -> int | None:
    match = re.search(r"([\d,]+)개 구매중", text)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


async def main():
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 412, "height": 892},
            user_agent="Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            locale="ko-KR",
        )
        page = await context.new_page()

        await page.goto(TARGET_URL)
        await page.locator("text=/\\d+개 구매중/").first.wait_for(timeout=30000)

        found_products: list[str] = []
        processed_indices: set[int] = set()
        scroll_position = 0

        while len(found_products) < MAX_PRODUCTS:
            purchase_elements = await page.locator("text=/\\d+개 구매중/").all()

            for idx, element in enumerate(purchase_elements):
                if idx in processed_indices:
                    continue

                text = await element.text_content()
                if not text:
                    continue

                count = parse_purchase_count(text)
                if not count or count < MIN_PURCHASE_COUNT:
                    processed_indices.add(idx)
                    continue

                try:
                    await element.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    await element.click()

                    await page.wait_for_url(lambda url: url != TARGET_URL, timeout=5000)
                    product_url = page.url

                    if product_url not in found_products:
                        found_products.append(product_url)
                        print(
                            f"[{len(found_products)}/{MAX_PRODUCTS}] {count:,}개 구매중 - {product_url}"
                        )

                    processed_indices.add(idx)

                    await page.goto(TARGET_URL)
                    await page.locator("text=/\\d+개 구매중/").first.wait_for(
                        timeout=30000
                    )
                    await page.evaluate(f"window.scrollTo(0, {scroll_position})")
                    await asyncio.sleep(0.3)

                    if len(found_products) >= MAX_PRODUCTS:
                        break

                except Exception as e:
                    print(f"Error: {e}")
                    processed_indices.add(idx)
                    continue

            if len(found_products) >= MAX_PRODUCTS:
                break

            scroll_position += 800
            await page.evaluate(f"window.scrollTo(0, {scroll_position})")
            await asyncio.sleep(0.5)

        print("\n=== 결과 ===")
        for i, url in enumerate(found_products, 1):
            print(f"{i}. {url}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
