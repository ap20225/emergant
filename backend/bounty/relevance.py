"""Issue-relevant file selection for bounded execution context.

Instead of dumping an ENTIRE repository into the coder's context (which only ever fits tiny
repos and breaks on real codebases), analyze the issue, rank the repository tree by relevance
to the issue, and send only the bounded RELEVANT subset — issue-referenced paths, strongest
name/token matches, and nearby files — to the coder.

This makes large real repositories executable when the requested change is localized, while
preserving the engine's bounded window (<=MAX_FILES / <=MAX_BYTES to the model), the E2B-only
execution model, model routing, retries/corrections, the per-job budget, the review gates and
Draft-PR-only publication (all unchanged). Deterministic (regex + scoring); optional model hints
(relevant paths/terms returned by the existing triage step) only re-order selection — they never
bypass the secret gate or the caps. GitHub reads only: no execution, no spend.
"""
import asyncio
import base64
import re

from fastapi import HTTPException

from .patches import safe_path
from .store import redact

# Same inspectable extension set the engine can actually read/edit (kept identical on purpose).
INSPECT_EXT = re.compile(r'\.(py|js|ts|tsx|jsx|json|md|txt|toml|yaml|yml|go)$', re.I)
CODE_EXT = re.compile(r'\.(py|js|ts|tsx|jsx|go)$', re.I)

MAX_FILE_BYTES = 20000
MAX_FILES = 30
MAX_BYTES = 100000

# Generated / vendored / example trees are almost never the issue-relevant code -> heavy penalty.
VENDORED = re.compile(
    r'(^|/)(node_modules|vendor|third_party|dist|build|out|coverage|__pycache__|site-packages|'
    r'bower_components|\.next|\.nuxt|\.min\.|target/|examples?/|fixtures?/|testdata/|_build/)', re.I)

_STOP = {
    'the', 'and', 'for', 'with', 'this', 'that', 'from', 'have', 'when', 'then', 'else', 'into',
    'issue', 'bounty', 'bug', 'fix', 'error', 'should', 'would', 'could', 'https', 'http', 'com',
    'github', 'repository', 'repo', 'code', 'file', 'files', 'function', 'method', 'class', 'test',
    'tests', 'expected', 'actual', 'steps', 'reproduce', 'please', 'like', 'want', 'need', 'make',
    'using', 'used', 'does', 'not', 'but', 'are', 'was', 'were', 'will', 'can', 'add', 'added',
}

# file path (with or without a directory), e.g. `src/foo/bar.py` or `bar.py`
_PATH_RE = re.compile(r'([A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,6})')
# identifiers: snake_case / CamelCase names >=4 chars
_IDENT_RE = re.compile(r'\b([a-z][a-z0-9_]{3,}|[A-Z][A-Za-z0-9]{3,}(?:[A-Z][A-Za-z0-9]+)*)\b')
_WORD_RE = re.compile(r'[a-z0-9]{4,}')


def _basename(path):
    return path.rsplit('/', 1)[-1]


def _stem(name):
    return name.rsplit('.', 1)[0].lower()


def extract_signals(title, body, labels=None):
    """Deterministic issue -> relevance signals (paths, filenames, dirs, identifiers, tokens)."""
    labels = labels or []
    text = f"{title or ''}\n{body or ''}\n{' '.join(str(l) for l in labels)}"
    lower = text.lower()
    paths, dirs = set(), set()
    for m in _PATH_RE.findall(text):
        p = m.strip('.,;:)(`\'"').lower()
        if '.' not in p or p.startswith('http') or p.endswith('.') or len(p) > 200:
            continue
        # ignore version-like / domain-like tokens (e.g. 3.11, node.js in prose without a dir)
        paths.add(p)
        if '/' in p:
            dirs.add(p.rsplit('/', 1)[0])
    basenames = {_basename(p) for p in paths}
    idents = {i for i in _IDENT_RE.findall(text) if len(i) >= 4 and i.lower() not in _STOP}
    tokens = {w for w in _WORD_RE.findall(lower) if w not in _STOP}
    return {'paths': paths, 'basenames': basenames, 'dirs': dirs, 'idents': idents, 'tokens': tokens}


def merge_hints(signals, hints):
    """Fold optional model-provided path/term hints into the signals (boost only)."""
    if not hints:
        return signals
    s = {k: set(v) for k, v in signals.items()}
    for h in hints:
        h = str(h).strip().lower()
        if not h:
            continue
        if '.' in h and ('/' in h or re.search(r'\.[a-z0-9]{1,6}$', h)):
            s['paths'].add(h)
            s['basenames'].add(_basename(h))
            if '/' in h:
                s['dirs'].add(h.rsplit('/', 1)[0])
        else:
            for w in _WORD_RE.findall(h):
                if w not in _STOP:
                    s['tokens'].add(w)
    return s


def score_path(path, size, signals):
    """Higher = more likely the issue-relevant file. Not gated on repo size."""
    pl = path.lower()
    base = _basename(pl)
    stem = _stem(base)
    depth = pl.count('/')
    score = 0.0
    if VENDORED.search(pl):
        score -= 50
    # Explicit references from the issue win decisively.
    if pl in signals['paths'] or any(pl.endswith('/' + p) or pl == p for p in signals['paths']):
        score += 100
    if base in signals['basenames']:
        score += 40
    for d in signals['dirs']:
        if d and ('/' + d + '/') in ('/' + pl + '/'):
            score += 8
    # identifier / token overlap with the filename stem
    for ident in signals['idents']:
        il = ident.lower()
        if il == stem:
            score += 14
        elif il in stem or stem in il:
            score += 6
    for tok in signals['tokens']:
        if tok == stem:
            score += 8
        elif tok in pl:
            score += 2
    # a test file that shares a token with a matched module is useful supporting context
    if re.search(r'(^|/)(test_|tests?/|.*[._]test\.|.*\.spec\.)', pl):
        if any(t in pl for t in signals['tokens']) or any(_basename(p).split('.')[0] in pl for p in signals['paths']):
            score += 5
        else:
            score -= 1
    if CODE_EXT.search(pl):
        score += 2
    else:
        score += 0.5  # json/yaml/toml/md config still useful, slightly lower
    score += max(0, 3 - depth) * 0.5  # shallow, top-level files matter a bit more
    if base in ('readme.md', 'readme', 'readme.txt'):
        score += 0.6  # keep as a weak fallback so we never return nothing
    return score


def _candidates(tree_entries):
    out = []
    for e in tree_entries:
        if e.get('type') != 'blob' or e.get('mode') not in ('100644', '100755'):
            continue
        path = e.get('path', '')
        try:
            safe_path(path)
        except HTTPException:
            continue
        size = e.get('size', 0) or 0
        if size > MAX_FILE_BYTES or not INSPECT_EXT.search(path):
            continue
        out.append({'path': path, 'sha': e.get('sha'), 'size': size})
    return out


def select(candidates, signals, max_files=MAX_FILES, max_bytes=MAX_BYTES):
    """Greedy bounded selection ordered by relevance; guarantees >=1 code file when any exists."""
    scored = sorted(((score_path(c['path'], c['size'], signals), c) for c in candidates),
                    key=lambda x: (-x[0], x[1]['size']))
    selected, total = [], 0
    for sc, c in scored:
        if len(selected) >= max_files or total + c['size'] > max_bytes:
            continue
        selected.append({**c, 'score': round(sc, 2)})
        total += c['size']
    # ensure at least one CODE file is present (swap the lowest-scored non-code out if needed)
    if selected and not any(CODE_EXT.search(s['path']) for s in selected):
        code = next((c for _, c in scored if CODE_EXT.search(c['path']) and c['size'] <= max_bytes), None)
        if code:
            selected = [s for s in selected if not CODE_EXT.search(s['path'])]
            # make room
            while selected and (sum(s['size'] for s in selected) + code['size'] > max_bytes or len(selected) >= max_files):
                selected.pop()
            selected.insert(0, {**code, 'score': round(score_path(code['path'], code['size'], signals), 2)})
    matched = any(s['score'] >= 40 for s in selected)
    return selected, matched


async def _expand_truncated(github, repo, tree_entries, signals, limit=3):
    """Big-repo trees truncate. Recurse ONLY into issue-relevant top-level dirs to recover their files."""
    subtrees = {e['path']: e['sha'] for e in tree_entries if e.get('type') == 'tree'}
    wanted_dirs = set(signals['dirs']) | {p.rsplit('/', 1)[0] for p in signals['paths'] if '/' in p}
    picks = []
    for full, sha in subtrees.items():
        fl = full.lower()
        if any(fl == d or fl.endswith('/' + d) for d in wanted_dirs if d):
            picks.append((full, sha))
    extra = []
    for full, sha in picks[:limit]:
        try:
            sub = await github.request('GET', f'/repos/{repo}/git/trees/{sha}?recursive=1')
        except HTTPException:
            continue
        for e in sub.get('tree', []):
            if e.get('type') == 'blob':
                extra.append({**e, 'path': f"{full}/{e.get('path', '')}"})
    return extra


async def gather_context(github, repo, base_sha, signals, model_hints=None,
                         max_files=MAX_FILES, max_bytes=MAX_BYTES):
    """Fetch tree, select the issue-relevant bounded subset, fetch + secret-check ONLY the selected
    files. Returns a dict with `files`, `selected`, `code_files`, `detail`, `truncated`,
    `matched_explicit`, `candidate_count`. Raises HTTPException on hard failure (tree error, no
    inspectable code, or a secret detected in a selected file)."""
    signals = merge_hints(signals, model_hints)
    tree = await github.request('GET', f'/repos/{repo}/git/trees/{base_sha}?recursive=1')
    entries = list(tree.get('tree', []))
    truncated = bool(tree.get('truncated'))
    if truncated:
        entries += await _expand_truncated(github, repo, entries, signals)
    candidates = _candidates(entries)
    if not candidates:
        raise HTTPException(409, 'No supported inspectable text files')
    if not any(CODE_EXT.search(c['path']) for c in candidates):
        raise HTTPException(409, 'No inspectable code files (only docs/config visible)')
    selected, matched = select(candidates, signals, max_files, max_bytes)
    if not selected:
        raise HTTPException(409, 'No relevant files fit the bounded inspection window')

    sem = asyncio.Semaphore(10)

    async def _blob(entry):
        async with sem:
            try:
                b = await github.request('GET', f"/repos/{repo}/git/blobs/{entry['sha']}")
                return entry['path'], base64.b64decode(b['content']).decode('utf-8')
            except (HTTPException, ValueError, UnicodeDecodeError, KeyError):
                return entry['path'], None

    files, code_files = {}, 0
    for path, content in await asyncio.gather(*[_blob(s) for s in selected]):
        if content is None:
            continue
        if redact(content) != content:
            raise HTTPException(409, 'Possible repository secret in a selected file; no material sent to model')
        files[path] = content
        if CODE_EXT.search(path):
            code_files += 1
    if not files:
        raise HTTPException(409, 'Selected files were not decodable text')
    detail = (f'{len(files)} relevant files / {sum(len(v.encode()) for v in files.values())}B / '
              f'{code_files} code, of {len(candidates)} candidates'
              f'{"; explicit issue-path match" if matched else "; ranked by relevance"}'
              f'{"; truncated-tree partial" if truncated else ""}')
    return {'files': files, 'selected': [{'path': s['path'], 'score': s['score']} for s in selected if s['path'] in files],
            'code_files': code_files, 'candidate_count': len(candidates), 'truncated': truncated,
            'matched_explicit': matched, 'detail': detail}
