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

    out_dir = Path("/app/test_reports/iteration8_browser")
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_owner_session_token()
    host = urlparse(base_url).hostname
    results = {"checks": [], "overflow": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            headless=True,
        )
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
        page.on("console", lambda msg: print(f"CONSOLE: {msg.type}: {msg.text}"))

        # Step 1: Owner models tab load + required data-testid coverage
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await page.goto(f"{base_url}/settings?tab=models", wait_until="networkidle", timeout=90000)
        await page.wait_for_selector("[data-testid='free-first-policy']", timeout=30000)

        required_testids = [
            "[data-testid='routing-routine']",
            "[data-testid='routing-coding']",
            "[data-testid='routing-review']",
            "[data-testid='routing-paid-escalation']",
            "[data-testid='routing-trigger-invalid-patch']",
            "[data-testid='routing-trigger-tests-failed']",
            "[data-testid='routing-trigger-review-rejected']",
            "[data-testid='routing-paid-lock']",
        ]
        missing = []
        for selector in required_testids:
            if await page.locator(selector).count() == 0:
                missing.append(selector)
        results["checks"].append({"missing_testids": missing})

        # Capture original policy values for restore
        original = {
            "routine": await page.input_value("[data-testid='routing-routine']"),
            "coding": await page.input_value("[data-testid='routing-coding']"),
            "review": await page.input_value("[data-testid='routing-review']"),
            "paid_escalation": await page.input_value("[data-testid='routing-paid-escalation']"),
            "max_free_attempts": await page.input_value("#routing-max-attempts"),
            "invalid_patch": await page.is_checked("[data-testid='routing-trigger-invalid-patch']"),
            "tests_failed": await page.is_checked("[data-testid='routing-trigger-tests-failed']"),
            "review_rejected": await page.is_checked("[data-testid='routing-trigger-review-rejected']"),
        }

        # Policy save + reload persistence verification
        toggled_attempts = "1" if original["max_free_attempts"] != "1" else "2"
        await page.fill("#routing-max-attempts", toggled_attempts)
        await page.click("[data-testid='routing-trigger-tests-failed']", force=True)

        async with page.expect_response(
            lambda r: "/api/bounty/routing-policy" in r.url and r.request.method == "PUT",
            timeout=40000,
        ) as save_info:
            await page.click("[data-testid='save-routing-policy']", force=True)
        save_response = await save_info.value
        await page.wait_for_timeout(1000)

        saved_flash = await page.locator("[data-testid='routing-policy-saved']").count() > 0
        after_reload = {
            "max_free_attempts": "",
            "tests_failed": False,
        }
        await page.click("[data-testid='reload-routing-policy']", force=True)
        await page.wait_for_timeout(1000)
        after_reload["max_free_attempts"] = await page.input_value("#routing-max-attempts")
        after_reload["tests_failed"] = await page.is_checked("[data-testid='routing-trigger-tests-failed']")

        results["checks"].append(
            {
                "save_status": save_response.status,
                "save_status_ok": save_response.status == 200,
                "saved_flash_visible": saved_flash,
                "max_attempts_persisted": after_reload["max_free_attempts"] == toggled_attempts,
                "tests_failed_persisted": after_reload["tests_failed"] != original["tests_failed"],
            }
        )

        # Restore original policy to avoid side effects
        await page.fill("[data-testid='routing-routine']", original["routine"])
        await page.fill("[data-testid='routing-coding']", original["coding"])
        await page.fill("[data-testid='routing-review']", original["review"])
        await page.fill("[data-testid='routing-paid-escalation']", original["paid_escalation"])
        await page.fill("#routing-max-attempts", original["max_free_attempts"])

        for key, selector in [
            ("invalid_patch", "[data-testid='routing-trigger-invalid-patch']"),
            ("tests_failed", "[data-testid='routing-trigger-tests-failed']"),
            ("review_rejected", "[data-testid='routing-trigger-review-rejected']"),
        ]:
            current = await page.is_checked(selector)
            if current != original[key]:
                await page.click(selector, force=True)

        async with page.expect_response(
            lambda r: "/api/bounty/routing-policy" in r.url and r.request.method == "PUT",
            timeout=40000,
        ) as restore_info:
            await page.click("[data-testid='save-routing-policy']", force=True)
        restore_response = await restore_info.value
        results["checks"].append({"restore_status": restore_response.status})

        # No legacy primary model controls
        body_text = (await page.text_content("body")) or ""
        results["checks"].append(
            {
                "legacy_primary_model_control_absent": "Primary model" not in body_text and "primary_model" not in body_text,
                "paid_lock_visible": "$0 ongoing authorization" in body_text,
            }
        )

        # Representative screenshot (single)
        await page.screenshot(path=str(out_dir / "owner_models_representative.jpeg"), quality=40, full_page=False)

        # Step 2: Security link navigation and unknown-label checks
        await page.goto(f"{base_url}/security", wait_until="networkidle", timeout=90000)
        await page.wait_for_selector("[data-testid='edit-free-first-routing']", timeout=30000)

        await page.click("[data-testid='edit-free-first-routing']", force=True)
        await page.wait_for_url("**/settings?tab=models**", timeout=30000)
        results["checks"].append({"edit_link_targets_models_tab": "tab=models" in page.url})

        await page.goto(f"{base_url}/security", wait_until="networkidle", timeout=90000)
        usage_empty_visible = await page.locator("[data-testid='model-usage-empty']").count() > 0
        security_text = (await page.text_content("body")) or ""
        results["checks"].append(
            {
                "unknown_not_fake_zero": (
                    ("Tokens, latency and actual inference cost are not available." in security_text)
                    if usage_empty_visible
                    else ("Unknown" in security_text or "Pending / unknown" in security_text)
                )
            }
        )

        # Step 3: Desktop/mobile overflow check
        for viewport in [{"width": 1920, "height": 1080}, {"width": 390, "height": 844}]:
            await page.set_viewport_size(viewport)
            await page.goto(f"{base_url}/settings?tab=models", wait_until="networkidle", timeout=90000)
            overflow = await page.evaluate(
                """() => ({
                    body: document.body.scrollWidth > window.innerWidth,
                    doc: document.documentElement.scrollWidth > window.innerWidth
                })"""
            )
            results["overflow"].append(
                {
                    "viewport": viewport,
                    "has_overflow": bool(overflow["body"] or overflow["doc"]),
                }
            )

        # Required error-selector check
        error_text = await page.evaluate("""() => {
const errorElements = Array.from(document.querySelectorAll('.error, [class*=\"error\"], [id*=\"error\"]'));
return errorElements.map(el => el.textContent).join(", ");
}""")
        if error_text:
            print(f"Found error message: {error_text}")
        else:
            print("No error messages found on the page")

        await context.close()
        await browser.close()

    (out_dir / "owner_browser_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    # Hard assertions
    if results["checks"][0]["missing_testids"]:
        raise AssertionError(f"Missing data-testid selectors: {results['checks'][0]['missing_testids']}")

    save_check = results["checks"][1]
    if save_check["save_status"] != 200:
        raise AssertionError(f"Expected save 200, got {save_check['save_status']}")
    if not save_check["saved_flash_visible"]:
        raise AssertionError("Policy saved indicator not visible")
    if not save_check["max_attempts_persisted"]:
        raise AssertionError("max_free_attempts did not persist after reload")
    if not save_check["tests_failed_persisted"]:
        raise AssertionError("Escalation trigger toggle did not persist after reload")

    if results["checks"][2]["restore_status"] != 200:
        raise AssertionError(f"Expected restore save 200, got {results['checks'][2]['restore_status']}")

    if not results["checks"][3]["legacy_primary_model_control_absent"]:
        raise AssertionError("Legacy primary model control text unexpectedly present")
    if not results["checks"][3]["paid_lock_visible"]:
        raise AssertionError("Paid lock $0 text is not visible")

    if not results["checks"][4]["edit_link_targets_models_tab"]:
        raise AssertionError("Security edit-free-first-routing link did not route to models tab")

    if not results["checks"][5]["unknown_not_fake_zero"]:
        raise AssertionError("Unknown token/cost labeling check failed")

    for item in results["overflow"]:
        if item["has_overflow"]:
            raise AssertionError(f"Horizontal overflow detected at viewport {item['viewport']}")

    print("ITERATION8_OWNER_BROWSER_CHECKS_PASSED")


if __name__ == "__main__":
    asyncio.run(main())
