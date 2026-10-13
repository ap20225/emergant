"""GitHub API transport. Public reads are separate from credentialed writes."""
import base64
import hashlib
import hmac
import json
import os
import re
import httpx
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError


class GitHub:
    def __init__(self, store):
        self.store = store

    async def request(self, method, path, payload=None, private=False):
        controls = await self.store.controls()
        if controls.get("integrations_revoked") or controls.get("global_stop"):
            raise HTTPException(409, "Integrations disabled by emergency policy")
        token = os.environ.get("GITHUB_TOKEN")
        from .secrets import SecretVault
        token = SecretVault().get('github') or token
        connection = await self.store.one('integration_connections', {'id': 'github'})
        if path.startswith('/repos/'):
            repository = '/'.join(path.split('/')[2:4])
            connected = bool(connection) and repository.lower() == connection['repository'].lower()
            if not connected:
                from .repos import RepoRegistry
                if not await RepoRegistry(self.store).can_read(repository):
                    raise HTTPException(403, 'Repository read blocked: REVOKED or BLOCKED in the repository authorization registry')
        if method != 'GET':
            raise HTTPException(409, 'Use the human-approved publication flow; direct GitHub writes are disabled')
        if (method != "GET" or private) and not token:
            raise HTTPException(503, "GitHub NOT CONNECTED. Configure a selected-repository fine-grained token server-side.")
        if method != "GET" and controls.get("submissions_disabled"):
            raise HTTPException(409, "External submissions disabled")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": os.environ["GITHUB_API_VERSION"]}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        await self.store.audit('researcher', 'GitHub read attempted', path)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                res = await client.request(method, os.environ["GITHUB_API_URL"] + path, headers=headers, json=payload)
        except httpx.RequestError:
            await self.store.audit('researcher', 'GitHub read failed', path, 'BLOCKED')
            raise HTTPException(502, "GitHub unavailable. No success was recorded.")
        if res.status_code not in (200, 201, 204):
            await self.store.audit('researcher', 'GitHub read rejected', f'{path}; HTTP {res.status_code}', 'BLOCKED')
            if res.status_code in (403, 429):
                raise HTTPException(429, "GitHub permission or rate limit reached. Stop and wait.")
            if res.status_code == 401:
                raise HTTPException(401, "GitHub credential expired or revoked")
            if res.status_code == 404:
                raise HTTPException(404, "GitHub repository or issue not found or not authorized")
            raise HTTPException(502, f"GitHub returned HTTP {res.status_code}; operation not recorded as successful")
        await self.store.audit('researcher', 'GitHub read confirmed', path)
        return res.json() if res.content else {}

    async def issue(self, repo, number):
        meta = await self.request("GET", f"/repos/{repo}")
        if meta.get("private"):
            raise HTTPException(403, "Private repositories are disabled in Phase 1")
        issue = await self.request("GET", f"/repos/{repo}/issues/{number}")
        if "pull_request" in issue:
            raise HTTPException(422, "This URL points to a pull request, not a bounty issue")
        return meta, issue

    async def branch(self, repo, base, branch):
        ref = await self.request("GET", f"/repos/{repo}/git/ref/heads/{base}", private=True)
        return await self.request("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]})

    async def commit_file(self, repo, branch, path, content, sha, message):
        return await self.request("PUT", f"/repos/{repo}/contents/{path}", {
            "message": message, "content": base64.b64encode(content.encode()).decode(), "sha": sha, "branch": branch})

    async def draft_pr(self, repo, title, body, head, base):
        return await self.request("POST", f"/repos/{repo}/pulls", {"title": title, "body": body, "head": head, "base": base, "draft": True})

    async def pr_details(self, repo, number):
        pr = await self.request("GET", f"/repos/{repo}/pulls/{number}")
        reviews = await self.request("GET", f"/repos/{repo}/pulls/{number}/reviews")
        comments = await self.request("GET", f"/repos/{repo}/issues/{number}/comments")
        events = await self.request("GET", f"/repos/{repo}/issues/{number}/events")
        return {"pr": pr, "reviews": reviews, "comments": comments, "events": events}

    async def webhook(self, request):
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
        if not secret or (await self.store.controls()).get("integrations_revoked"):
            raise HTTPException(503, "GitHub webhook NOT CONNECTED")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "Webhook too large")
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, request.headers.get("X-Hub-Signature-256", "")):
            await self.store.audit("security", "Webhook rejected", "Invalid GitHub signature", "BLOCKED")
            raise HTTPException(403, "Invalid webhook signature")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery):
            raise HTTPException(400, "Missing/invalid delivery id")
        try:
            data = json.loads(body)
        except ValueError:
            raise HTTPException(400, "Invalid JSON")
        event = request.headers.get("X-GitHub-Event", "unknown")
        repo = data.get("repository", {}).get("full_name")
        try:
            await self.store.insert("webhook_events", {"delivery_id": delivery, "event": event, "repository": repo, "action": data.get("action")})
        except DuplicateKeyError:
            return {"accepted": True, "duplicate": True}
        if event == "pull_request":
            pr_data = data.get("pull_request", {})
            pr = await self.store.one("pull_requests", {"repository": repo, "number": pr_data.get("number"), "external": True})
            if pr:
                status = "MERGED" if pr_data.get("merged") else ("REJECTED" if pr_data.get("state") == "closed" else "UNDER_REVIEW")
                await self.store.db.pull_requests.update_one({"id": pr["id"]}, {"$set": {"status": status}})
                await self.store.db.jobs.update_one({"id": pr["job_id"]}, {"$set": {"status": status}})
                await self.store.audit("outcome", "PR outcome received", f"{status}; no financial status changed", job_id=pr["job_id"])
        await self.store.audit("outcome", "Webhook recorded", f"{event}: {delivery}; no payment recognition")
        return {"accepted": True, "duplicate": False}