import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from playwright.async_api import async_playwright

sys.path.append("/app/tests")
from session_utils import get_owner_session_token


async def main():
    env = dotenv_values("/app/frontend/.env")
    base_url = (env.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("REACT_APP_BACKEND_URL missing")

    out_dir = Path("/app/test_reports/iteration9_browser")
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_owner_session_token()
    host = urlparse(base_url).hostname
    results = {"checks": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            headless=True,
        )

        # Public /login copy check (no auth actions)
        public_context = await browser.new_context()
        public_page = await public_context.new_page()
        await public_page.set_viewport_size({"width": 1440, "height": 1080})
        await public_page.goto(f"{base_url}/login", wait_until="networkidle", timeout=90000)
        login_heading = await public_page.text_content("[data-testid='login-brand-name']")
        results["checks"].append({
            "login_heading": (login_heading or "").strip(),
            "login_heading_ok": (login_heading or "").strip() == "Bounty Hunter workspace",
        })
        await public_context.close()

        # Owner context for / and /security verification
        context = await browser.new_context()
        await context.add_cookies(
            [
                {
                    "name": "session_token",
                    "value": token,
                    "domain": host,
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ]
        )
        page = await context.new_page()

        # Delay bootstrap once to assert initial-loading brand copy
        intercepted = {"seen": False}

        async def delay_bootstrap(route, request):
            if not intercepted["seen"] and "/api/bounty/bootstrap" in request.url:
                intercepted["seen"] = True
                await page.wait_for_timeout(900)
            await route.continue_()

        await context.route("**/api/bounty/bootstrap", delay_bootstrap)
        await page.set_viewport_size({"width": 1440, "height": 1080})
        await page.goto(f"{base_url}/", wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_selector("[data-testid='loading-brand-name']", timeout=10000)
        loading_text = await page.text_content("[data-testid='loading-brand-name']")
        await page.wait_for_selector("[data-testid='app-brand-name']", timeout=20000)
        app_brand = await page.text_content("[data-testid='app-brand-name']")

        title = await page.title()
        meta_description = await page.evaluate(
            """() => document.querySelector('meta[name="description"]')?.getAttribute('content') || ''"""
        )

        results["checks"].append(
            {
                "loading_brand": (loading_text or "").strip(),
                "sidebar_brand": (app_brand or "").strip(),
                "title": title,
                "meta_description": meta_description,
                "title_ok": title == "Bounty Hunter",
                "meta_ok": meta_description.startswith("Bounty Hunter"),
            }
        )

        # Page heading fallback check on non-nav route
        await page.goto(f"{base_url}/jobs/non-existent", wait_until="networkidle", timeout=90000)
        body_text = (await page.text_content("body")) or ""
        results["checks"].append({"fallback_heading_ok": "Job workspace" in body_text})

        # Security details and read-only invariant labels
        await page.goto(f"{base_url}/security", wait_until="networkidle", timeout=90000)
        await page.wait_for_selector("[data-testid='github-verified-details']", timeout=30000)
        await page.wait_for_selector("[data-testid='runner-saved-details']", timeout=30000)
        github_details = (await page.text_content("[data-testid='github-verified-details']")) or ""
        runner_details = (await page.text_content("[data-testid='runner-saved-details']")) or ""
        runner_template = (await page.text_content("[data-testid='runner-saved-template']")) or ""
        runner_key_field = await page.input_value("[data-testid='runner-api-key']")

        blockers = await page.locator("[data-testid='execution-blockers'] li").all_text_contents()
        blocker_text = " | ".join(blockers)

        results["checks"].append(
            {
                "github_details": github_details,
                "runner_details": runner_details,
                "runner_template": runner_template.strip(),
                "runner_key_blank": runner_key_field == "",
                "github_connected_repo_ok": "ap20225/bounty-hunter" in github_details,
                "github_issue_ok": "issue #1" in github_details,
                "github_read_verified_ok": "read-verified" in github_details,
                "runner_template_ok": runner_template.strip() == "openclaw",
                "runner_status_phrase_ok": "runtime not yet verified" in runner_details.lower(),
                "blocker_no_github_required": "GITHUB_REQUIRED" not in blocker_text,
                "blocker_no_runner_required": "RUNNER_REQUIRED" not in blocker_text,
                "execution_not_ready_visible": "NOT READY" in body_text or "NOT READY" in ((await page.text_content("body")) or ""),
                "catalog_302_visible": "302 catalog models" in ((await page.text_content("body")) or ""),
                "inference_unverified_visible": "Inference: NOT VERIFIED" in ((await page.text_content("body")) or ""),
            }
        )

        # Width checks for clipped brand: desktop 1440 + mobile 320
        async def brand_clipped_check(width, height):
            await page.set_viewport_size({"width": width, "height": height})
            await page.goto(f"{base_url}/", wait_until="networkidle", timeout=90000)
            if width <= 900:
                await page.click("[data-testid='mobile-menu-toggle']", force=True)
                await page.wait_for_timeout(300)
            box = await page.locator("[data-testid='app-brand-name']").bounding_box()
            if not box:
                return {"width": width, "height": height, "brand_visible": False, "clipped": True}
            clipped = box["x"] < 0 or (box["x"] + box["width"]) > width
            if width <= 900:
                await page.screenshot(path=str(out_dir / "security_320.jpeg"), quality=40, full_page=False)
            else:
                await page.screenshot(path=str(out_dir / "security_1440.jpeg"), quality=40, full_page=False)
            return {
                "width": width,
                "height": height,
                "brand_visible": True,
                "brand_width": box["width"],
                "brand_right": box["x"] + box["width"],
                "clipped": clipped,
            }

        results["checks"].append(await brand_clipped_check(1440, 1080))
        results["checks"].append(await brand_clipped_check(320, 844))

        error_text = await page.evaluate("""() => {
const errorElements = Array.from(document.querySelectorAll('.error, [class*="error"], [id*="error"]'));
return errorElements.map(el => el.textContent).join(", ");
}""")
        if error_text:
            print(f"Found error message: {error_text}")
        else:
            print("No error messages found on the page")

        await context.close()
        await browser.close()

    (out_dir / "owner_browser_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    if not results["checks"][0]["login_heading_ok"]:
        raise AssertionError(f"Unexpected login heading: {results['checks'][0]['login_heading']}")

    core = results["checks"][1]
    if core["loading_brand"] != "Bounty Hunter":
        raise AssertionError(f"Unexpected loading brand: {core['loading_brand']}")
    if core["sidebar_brand"] != "Bounty Hunter":
        raise AssertionError(f"Unexpected sidebar brand: {core['sidebar_brand']}")
    if not core["title_ok"]:
        raise AssertionError(f"Unexpected title: {core['title']}")
    if not core["meta_ok"]:
        raise AssertionError(f"Unexpected meta description: {core['meta_description']}")

    if not results["checks"][2]["fallback_heading_ok"]:
        raise AssertionError("Job workspace fallback heading text not found on /jobs/:id route")

    security = results["checks"][3]
    for key in [
        "github_connected_repo_ok",
        "github_issue_ok",
        "github_read_verified_ok",
        "runner_template_ok",
        "runner_status_phrase_ok",
        "runner_key_blank",
        "blocker_no_github_required",
        "blocker_no_runner_required",
        "catalog_302_visible",
        "inference_unverified_visible",
    ]:
        if not security[key]:
            raise AssertionError(f"Security invariant failed: {key}")

    for width_check in results["checks"][4:6]:
        if width_check["clipped"]:
            raise AssertionError(f"Brand appears clipped at {width_check['width']}px")

    print("ITERATION9_OWNER_BROWSER_CHECKS_PASSED")


if __name__ == "__main__":
    asyncio.run(main())
