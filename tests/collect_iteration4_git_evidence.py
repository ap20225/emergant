import json
import subprocess
from pathlib import Path


def run(repo: Path, *args):
    p = subprocess.run(["/usr/bin/git", *args], cwd=repo, capture_output=True, text=True)
    return {"cmd": "git " + " ".join(args), "exit_code": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}


def main():
    jobs_root = Path("/app/jobs")
    out = {"workspaces": []}
    for job_dir in sorted([p for p in jobs_root.iterdir() if p.is_dir() and p.name.startswith("job_")]):
        repo = job_dir / "repository"
        if not repo.exists():
            continue
        entry = {
            "job_id": job_dir.name,
            "repository_path": str(repo),
            "checks": [
                run(repo, "rev-parse", "--is-inside-work-tree"),
                run(repo, "branch", "--show-current"),
                run(repo, "status", "--porcelain"),
                run(repo, "log", "--oneline", "-n", "5"),
                run(repo, "remote", "-v"),
                run(repo, "diff", "--stat"),
            ],
        }
        out["workspaces"].append(entry)

    Path("/app/test_reports/iteration4_git_evidence.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
