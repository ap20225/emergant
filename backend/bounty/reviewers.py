import ast
import re
from . import fixture


def research(workspace, opportunity):
    files = sorted(p.name for p in workspace.repo.iterdir() if p.is_file())
    source = (workspace.repo / "slugify.py").read_text()
    return {"problem_interpretation": opportunity["title"],
        "likely_root_cause": "Literal space replacement does not normalize repeated whitespace, tabs or newlines." if '.replace(" ", "-")' in source else "Reference source differs; stop for review.",
        "files_inspected": files, "relevant_files": ["slugify.py"], "relevant_tests": ["test_slugify.py"],
        "contribution_rules": (workspace.repo / "CONTRIBUTING.md").read_text(),
        "strategy": "Reproduce the failures; normalize whitespace with split/join; preserve API and unrelated files.",
        "estimated_hours": opportunity["estimated_hours"], "risks": ["Whitespace normalization changes edge-case behavior"],
        "test_strategy": "Run seven unittest cases before and after; independently inspect AST and diff.",
        "dependencies": "Python standard library only", "ci": "No CI configuration in reference repository",
        "issue_history": "Local fixture issue only; no remote PR history", "implementation": "Deterministic reference research, not LLM analysis"}


def code(workspace):
    path = workspace.repo / "slugify.py"
    if path.read_text() != fixture.ORIGINAL:
        raise RuntimeError("Reference worker refuses unexpected source; general coding provider NOT CONNECTED")
    path.write_text(fixture.FIXED)
    return {"strategy": "Minimal one-line whitespace normalization", "files": ["slugify.py"],
        "model": "deterministic-reference-worker", "notes": "Bundled acceptance fixture only; no general AI coding or paid model call."}


def security_review(source, diff, files):
    findings = []
    if re.search(r"(?i)sk-[\w-]{10,}|ghp_\w{10,}|-----BEGIN.*PRIVATE KEY|password\s*=", source + diff):
        findings.append("Possible embedded secret")
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                findings.append("Unexpected dependency/import in reference change")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "open", "compile", "__import__"}:
                findings.append("Unsafe dynamic execution or file access")
    except SyntaxError:
        findings.append("Source cannot be parsed")
    if set(files) != {"slugify.py"}:
        findings.append("Change exceeds permitted file scope")
    return {"status": "FAIL" if findings else "PASS", "findings": findings,
        "checks": ["Secret patterns", "Dependency imports", "Dynamic execution", "File scope", "AST syntax"],
        "scope": "Static review of the controlled reference change only; not a comprehensive security audit"}


def code_review(source, files, test_result, diff):
    findings = []
    try:
        tree = ast.parse(source)
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        if len(functions) != 1 or functions[0].name != "slugify" or len(functions[0].args.args) != 1:
            findings.append("Public function signature changed")
    except SyntaxError:
        findings.append("Invalid Python syntax")
    if test_result["status"] != "PASS":
        findings.append("Regression tests have not passed")
    if files != ["slugify.py"] or len(diff.splitlines()) > 30:
        findings.append("Unexpected scope or complexity")
    if not diff:
        findings.append("No implementation diff")
    return {"status": "REQUEST_CHANGES" if findings else "APPROVE", "findings": findings,
        "summary": "Reference change is scoped, keeps the API, and has passing regression evidence." if not findings else "Changes require review.",
        "scope": "Independent deterministic reference reviewer; general maintainer review provider NOT CONNECTED"}