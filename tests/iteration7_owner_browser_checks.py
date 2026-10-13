import asyncio
import json
import re
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

    out_dir = Path("/app/test_reports/iteration7_browser")
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_owner_session_token()
    host = urlparse(base_url).hostname
    results = {"checks": [], "responsive": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            headless=True,
        )
        context = await browser.new_context()
        await context.add_cookies([
            {
                "name": "session_token",
                "value": token,
                "domain": host,
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        ])

        page = await context.new_page()
        page.on("console", lambda msg: print(f"CONSOLE: {msg.type}: {msg.text}"))

        # Optional responsive pass requested (mobile 320 and desktop 1440)
        for width in [320, 1440]:
            await page.set_viewport_size({"width": width, "height": 1100})
            await page.goto(f"{base_url}/security", wait_until="networkidle", timeout=90000)
            await page.wait_for_selector("[data-testid='real-execution-status']", timeout=30000)
            overflow = await page.evaluate(
                """() => ({
                    body: document.body.scrollWidth > window.innerWidth,
                    doc: document.documentElement.scrollWidth > window.innerWidth
                })"""
            )
            await page.screenshot(path=str(out_dir / f"security_{width}.jpeg"), quality=40, full_page=False)
            results["responsive"].append({"width": width, "overflow": bool(overflow["body"] or overflow["doc"])})

        await page.set_viewport_size({"width": 1440, "height": 1200})
        await page.goto(f"{base_url}/security", wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_selector("[data-testid='model-catalog-count']", timeout=30000)

        async with page.expect_response(
            lambda r: "/api/bounty/execution/status" in r.url and r.status == 200,
            timeout=60000,
        ) as status_info:
            await page.reload(wait_until="networkidle", timeout=90000)
        status_resp = await status_info.value
        results["checks"].append({"execution_status_http": status_resp.status})

        # Verify trusted owner browser invalid form hits 422 (not 403)
        submitted_token = "ghp_ITER7_SYNTHETIC_TOKEN_abcdefghijklmnopqrstuvwxyz"
        await page.fill("[data-testid='github-repository']", "owner/repo")
        await page.fill("[data-testid='github-issue-number']", "0")
        await page.fill("[data-testid='github-token']", submitted_token)

        async with page.expect_response(
            lambda r: "/api/bounty/connections/github" in r.url and r.request.method == "POST",
            timeout=30000,
        ) as gh_resp_info:
            await page.eval_on_selector(
                "[data-testid='github-connect-form']",
                "form => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))",
            )
        gh_resp = await gh_resp_info.value
        await page.wait_for_timeout(1200)

        gh_error = None
        gh_error_el = page.locator("[data-testid='connection-error']").first
        if await gh_error_el.count():
            gh_error = await gh_error_el.text_content()
        token_after = await page.input_value("[data-testid='github-token']")

        results["checks"].append(
            {
                "github_invalid_issue_http": gh_resp.status,
                "github_invalid_issue_error": gh_error,
                "token_cleared_after_invalid_submit": token_after == "",
                "secret_echoed": bool(gh_error and submitted_token in gh_error),
            }
        )

        # Wait for real catalog count to load before diagnostic click
        catalog_text = ""
        catalog_count = -1
        for _ in range(70):
            catalog_text = (await page.text_content("[data-testid='model-catalog-count']") or "").strip()
            match = re.search(r"(\d+)", catalog_text)
            catalog_count = int(match.group(1)) if match else -1
            if "302 catalog models" in catalog_text or catalog_count > 0:
                break
            await page.wait_for_timeout(1000)

        results["checks"].append({"catalog_text_before_diagnostic": catalog_text, "catalog_count": catalog_count})

        if not ("302 catalog models" in catalog_text or catalog_count > 0):
            raise RuntimeError(f"Catalog count not loaded before diagnostic click: {catalog_text}")

        async with page.expect_response(
            lambda r: "/api/bounty/connections/model/diagnostic" in r.url and r.request.method == "POST",
            timeout=30000,
        ) as diag_resp_info:
            await page.click("[data-testid='model-diagnostic-check']", force=True)
        diag_resp = await diag_resp_info.value
        await page.wait_for_timeout(1200)

        diag_error = None
        diag_error_el = page.locator("[data-testid='connection-error']").first
        if await diag_error_el.count():
            diag_error = await diag_error_el.text_content()

        usage_rows = await page.locator("tr[data-testid^='model-usage-']").count()
        usage_empty_visible = await page.is_visible("[data-testid='model-usage-empty']")

        # Get error messages using specific selectors
        error_text = await page.evaluate("""() => {
const errorElements = Array.from(document.querySelectorAll('.error, [class*="error"], [id*="error"]'));
return errorElements.map(el => el.textContent).join(", ");
}""")
        if error_text:
            print(f"Found error message: {error_text}")
        else:
            print("No error messages found on the page")

        results["checks"].append(
            {
                "diagnostic_http": diag_resp.status,
                "diagnostic_error": diag_error,
                "usage_rows_after_diagnostic": usage_rows,
                "usage_empty_visible": usage_empty_visible,
            }
        )

        await page.screenshot(path=str(out_dir / "security_owner_final.jpeg"), quality=40, full_page=False)
        await context.close()
        await browser.close()

    (out_dir / "owner_browser_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    # hard assertions after artifact capture
    check_422 = results["checks"][1]
    if check_422["github_invalid_issue_http"] != 422:
        raise AssertionError(f"Expected 422, got {check_422['github_invalid_issue_http']}")
    if "Untrusted request origin" in str(check_422["github_invalid_issue_error"]):
        raise AssertionError("Unexpected 403-style origin rejection in owner browser flow")
    if "Invalid configuration fields" not in str(check_422["github_invalid_issue_error"]):
        raise AssertionError(f"Unexpected inline error: {check_422['github_invalid_issue_error']}")
    if check_422["token_cleared_after_invalid_submit"] is not True:
        raise AssertionError("Token field did not clear after invalid submit")
    if check_422["secret_echoed"] is True:
        raise AssertionError("Submitted synthetic token was echoed in error message")

    check_diag = results["checks"][3]
    if check_diag["diagnostic_http"] != 409:
        raise AssertionError(f"Expected diagnostic 409, got {check_diag['diagnostic_http']}")
    if "PROVIDER_CAP_UNVERIFIED" not in str(check_diag["diagnostic_error"]):
        raise AssertionError(f"Unexpected diagnostic inline error: {check_diag['diagnostic_error']}")
    if check_diag["usage_rows_after_diagnostic"] != 0:
        raise AssertionError("Usage row count changed after blocked diagnostic")
    if check_diag["usage_empty_visible"] is not True:
        raise AssertionError("Expected empty model usage indicator after blocked diagnostic")

    print("ITERATION7_OWNER_BROWSER_CHECKS_PASSED")


if __name__ == "__main__":
    asyncio.run(main())
