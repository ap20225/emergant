import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from playwright.async_api import async_playwright

from session_utils import get_owner_session_token


async def main():
    env = dotenv_values("/app/frontend/.env")
    base_url = (env.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("REACT_APP_BACKEND_URL missing")

    out_dir = Path("/app/test_reports/iteration6_browser")
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_owner_session_token()
    host = urlparse(base_url).hostname
    results = {"responsive": [], "checks": [], "mocked": []}

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

        for width in [320, 768, 1024, 1440]:
            await page.set_viewport_size({"width": width, "height": 1100})
            await page.goto(f"{base_url}/security", wait_until="networkidle", timeout=90000)
            await page.wait_for_selector("[data-testid='page-title']", timeout=20000)
            overflow = await page.evaluate(
                """() => ({
                    body: document.body.scrollWidth > window.innerWidth,
                    doc: document.documentElement.scrollWidth > window.innerWidth
                })"""
            )
            await page.screenshot(path=str(out_dir / f"security_{width}.jpeg"), quality=40, full_page=False)
            results["responsive"].append({"width": width, "overflow": bool(overflow["body"] or overflow["doc"])})

        await page.set_viewport_size({"width": 1440, "height": 1200})
        await page.goto(f"{base_url}/security", wait_until="networkidle", timeout=90000)
        await page.wait_for_selector("[data-testid='integration-status-github']", timeout=20000)

        status_text = await page.text_content("[data-testid='real-execution-status']")
        start_disabled = await page.is_disabled("[data-testid='start-controlled-job']")
        verify_disabled = await page.is_disabled("[data-testid='verify-github-connection']")
        usage_empty = await page.text_content("[data-testid='model-usage-empty']")
        inference_text = await page.text_content("[data-testid='model-inference-status']")
        model_status_text = await page.text_content("[data-testid='integration-status-model']")

        results["checks"].append({
            "readiness": status_text,
            "start_disabled_without_gh": start_disabled,
            "verify_disabled_without_gh": verify_disabled,
            "usage_empty_text": usage_empty,
            "inference_text": inference_text,
            "catalog_status_text": model_status_text,
        })

        # Browser native required-field validation on empty submission
        await page.fill("[data-testid='github-repository']", "")
        await page.fill("[data-testid='github-issue-number']", "")
        await page.fill("[data-testid='github-token']", "")
        await page.click("[data-testid='connect-github']", force=True)
        empty_validity = await page.eval_on_selector(
            "[data-testid='github-repository']",
            "el => ({ valid: el.checkValidity(), msg: el.validationMessage })",
        )
        results["checks"].append({"empty_required_validation": empty_validity})

        # Real API 422 using invalid issue_number=0 (no real GitHub call)
        await page.fill("[data-testid='github-repository']", "owner/repo")
        await page.fill("[data-testid='github-issue-number']", "0")
        await page.fill("[data-testid='github-token']", "ghp_abcdefghijklmnopqrstuvwxyz")
        await page.eval_on_selector(
            "[data-testid='github-connect-form']",
            "form => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))",
        )
        await page.wait_for_timeout(1000)
        err_422 = None
        err_el = await page.query_selector("[data-testid='connection-error']")
        if err_el:
            err_422 = await err_el.text_content()
        token_after_422 = await page.input_value("[data-testid='github-token']")
        results["checks"].append({"invalid_issue_422_error": err_422, "token_cleared_after_422": token_after_422 == ""})

        # MOCKED browser failure for connect endpoint to verify password clearing behavior only
        async def mock_connect(route, request):
            if request.method == "POST":
                await route.fulfill(status=400, content_type="application/json", body=json.dumps({"detail": "MOCKED_GITHUB_CONNECT_FAILURE"}))
            else:
                await route.continue_()

        await page.route("**/api/bounty/connections/github", mock_connect)
        await page.fill("[data-testid='github-repository']", "owner/repo")
        await page.fill("[data-testid='github-issue-number']", "1")
        await page.fill("[data-testid='github-token']", "ghp_abcdefghijklmnopqrstuvwxyz")
        await page.click("[data-testid='connect-github']", force=True)
        await page.wait_for_timeout(800)
        mock_err = None
        mock_err_el = await page.query_selector("[data-testid='connection-error']")
        if mock_err_el:
            mock_err = await mock_err_el.text_content()
        token_after_mock = await page.input_value("[data-testid='github-token']")
        await page.unroute("**/api/bounty/connections/github", mock_connect)
        results["mocked"].append({
            "type": "BROWSER MOCK ONLY",
            "flow": "github_connect_failure_password_clear",
            "error": mock_err,
            "token_cleared": token_after_mock == "",
        })

        # MOCKED status + real click path to assert inline 409 rendering for diagnostic gate
        status_route_hit = {"value": False}
        diagnostic_route_hit = {"value": False}

        async def mock_status(route, request):
            status_route_hit["value"] = True
            payload = {
                "status": "NOT_READY",
                "github": {"status": "NOT CONNECTED"},
                "model": {
                    "catalog_status": "HTTP_302_OK",
                    "credential_configured": True,
                    "model_count": 1,
                    "model_ids": ["demo-model"],
                    "inference_verified_at": None,
                    "diagnostic_reason": "PROVIDER_CAP_UNVERIFIED",
                },
                "runner": {"status": "BLOCKED"},
                "usage": [],
                "diagnostic_attempted": False,
                "blockers": [{"code": "PROVIDER_CAP_UNVERIFIED", "action": "Blocked"}],
            }
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        async def mock_diag(route, request):
            diagnostic_route_hit["value"] = True
            await route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "PROVIDER_CAP_UNVERIFIED: diagnostic blocked"}))

        await page.route("**/api/bounty/execution/status", mock_status)
        await page.route("**/api/bounty/connections/model/diagnostic", mock_diag)
        await page.goto(f"{base_url}/security", wait_until="networkidle", timeout=90000)
        await page.wait_for_selector("[data-testid='model-diagnostic-check']", timeout=20000)
        await page.click("[data-testid='model-diagnostic-check']", force=True)
        await page.wait_for_timeout(800)
        diag_error = None
        diag_err_el = await page.query_selector("[data-testid='connection-error']")
        if diag_err_el:
            diag_error = await diag_err_el.text_content()
        await page.unroute("**/api/bounty/execution/status", mock_status)
        await page.unroute("**/api/bounty/connections/model/diagnostic", mock_diag)
        results["mocked"].append({
            "type": "BROWSER MOCK ONLY",
            "flow": "diagnostic_button_409_inline_error",
            "status_mock_hit": status_route_hit["value"],
            "diagnostic_mock_hit": diagnostic_route_hit["value"],
            "inline_error": diag_error,
        })

        await page.screenshot(path=str(out_dir / "security_owner_final.jpeg"), quality=40, full_page=False)
        await context.close()
        await browser.close()

    (out_dir / "owner_browser_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("OWNER_BROWSER_CHECKS_DONE")


if __name__ == "__main__":
    asyncio.run(main())
