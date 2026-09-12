import asyncio, json
from patchright.async_api import async_playwright

PROFILE = "data/browser_profiles/prom-56e46ee3-0f19-4e6f-8c47-b8a81dbf0d40"
CAPTURED = []

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE, headless=True, channel="chrome", locale="uk-UA",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_response(resp):
            u = resp.url
            if "graphql" in u or "/cabinet/" in u or "/api/" in u or "order" in u.lower():
                try:
                    body = await resp.text()
                except Exception:
                    body = "<binary>"
                CAPTURED.append({"url": u, "status": resp.status, "method": resp.request.method,
                                 "post": resp.request.post_data, "body": body[:6000]})
        page.on("response", on_response)

        await page.goto("https://my.prom.ua/cabinet/user/orders", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        print("FINAL URL:", page.url)
        print("BODY TEXT:", (await page.inner_text("body"))[:500])
        await ctx.close()

    for c in CAPTURED:
        print("\n=== ", c["method"], c["status"], c["url"])
        if c["post"]:
            print("POST:", c["post"][:2000])
        print("RESP:", c["body"][:3000])

asyncio.run(main())
