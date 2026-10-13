"""Bounded text-only patches. No model-generated shell commands."""
import json
import re
from pathlib import PurePosixPath
from fastapi import HTTPException
from .store import redact, contains_secret


def safe_path(path):
    p = PurePosixPath(path)
    if (not isinstance(path, str) or not path or len(path) > 220 or p.is_absolute()
            or any(x in {'..', '.', '.git', '.github', 'node_modules', '.venv'} for x in path.split('/'))
            or '\\' in path or any(ord(c) < 32 for c in path) or path.startswith('-')
            or any(x.startswith('.env') for x in p.parts)
            or p.suffix.lower() in {'.pem', '.key', '.p12'}):
        raise HTTPException(422, 'Unsafe or protected patch path')
    return path


def loads_lenient(text):
    """Models often wrap JSON in markdown fences or prose. Extract the JSON object safely."""
    t = str(text).strip()
    if t.startswith('```'):
        t = re.sub(r'^```[a-zA-Z0-9]*\n?', '', t)
        t = re.sub(r'\n?```\s*$', '', t.strip())
    try:
        return json.loads(t)
    except ValueError:
        match = re.search(r'\{.*\}', t, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def parse_patch(text):
    try:
        data = loads_lenient(text)
        files = data['files']
        if not isinstance(files, dict) or not 1 <= len(files) <= 8:
            raise ValueError()
        for path, content in files.items():
            safe_path(path)
            if not isinstance(content, str) or len(content.encode()) > 60000 or contains_secret(content):
                raise ValueError()
        if sum(len(v.encode()) for v in files.values()) > 120000:
            raise ValueError()
        return {'summary': redact(str(data.get('summary', 'Scoped issue fix'))[:200]), 'files': files}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, 'Model patch is not valid bounded JSON or contains possible secrets')


def review_result(text, security=False):
    try:
        data = loads_lenient(text)
        expected = 'PASS' if security else 'APPROVE'
        status = str(data.get('status', '')).strip().upper()
        raw = data.get('findings', [])
        if isinstance(raw, dict):
            raw = [raw]
        elif not isinstance(raw, list):
            raw = [raw] if raw else []
        findings = []
        for x in raw:
            if isinstance(x, str):
                s = x.strip()
            elif isinstance(x, dict):
                s = '; '.join(f'{k}: {v}' for k, v in x.items() if isinstance(v, (str, int, float)))
            else:
                s = str(x)
            if s:
                findings.append(s)
        positive = {'PASS', 'APPROVE', 'APPROVED', 'OK', 'LGTM', 'NO_ISSUES'}
        negative = {'FAIL', 'REQUEST_CHANGES', 'CHANGES_REQUESTED', 'REJECT', 'REJECTED'}
        if status not in positive | negative:
            raise ValueError()
        status = expected if (status in positive and not findings) else ('FAIL' if security else 'REQUEST_CHANGES')
        return {'status': status, 'findings': [redact(x[:1000]) for x in findings[:20]],
                'scope': 'Independent model review of actual repository context, patch and test output; not a comprehensive security certification'}
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, 'Review output invalid; publication remains blocked')


def tests_passed(suite, result):
    if result['exit_code'] != 0:
        return False
    patterns = {'unittest': r'Ran [1-9]\d* tests?', 'pytest': r'\b[1-9]\d* passed',
                'jest': r'Tests:\s+.*[1-9]\d* passed', 'vitest': r'Tests\s+.*[1-9]\d* passed',
                'go': r'(?m)^ok\s+'}
    return bool(re.search(patterns[suite], result['output']))