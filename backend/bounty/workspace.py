import asyncio
import ctypes.util
import os
from pathlib import Path
import re
import shutil
import sysconfig
from . import fixture
from .store import redact


class Workspace:
    def __init__(self, job_id):
        if not re.fullmatch(r"job_[a-f0-9]{12}", job_id):
            raise ValueError("Invalid workspace identifier")
        self.root = Path(os.environ["BOUNTY_JOBS_DIR"]) / job_id
        self.repo = self.root / "repository"
        self.process = None

    @staticmethod
    def supported():
        return os.geteuid() == 0 and bool(ctypes.util.find_library("seccomp"))

    def environment(self):
        return {"PATH": os.environ["BOUNTY_EXECUTION_PATH"], "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": os.environ["BOUNTY_GIT_AUTHOR_NAME"], "GIT_AUTHOR_EMAIL": os.environ["BOUNTY_GIT_AUTHOR_EMAIL"],
            "GIT_COMMITTER_NAME": os.environ["BOUNTY_GIT_AUTHOR_NAME"], "GIT_COMMITTER_EMAIL": os.environ["BOUNTY_GIT_AUTHOR_EMAIL"]}

    async def command(self, args, env=None, timeout=25):
        self.process = await asyncio.create_subprocess_exec(*args, cwd=self.repo, env=env or self.environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            output, _ = await asyncio.wait_for(self.process.communicate(), timeout)
            return {"exit_code": self.process.returncode, "output": redact(output.decode(errors="replace")[:24000])}
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
            raise
        finally:
            self.process = None

    async def git(self, *args):
        result = await self.command([os.environ["BOUNTY_GIT"], "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", *args])
        if result["exit_code"]:
            raise RuntimeError(f"Git operation failed: {result['output']}")
        return result["output"].strip()

    async def create(self, attempt=0):
        if not self.supported():
            raise RuntimeError("Reference sandbox unavailable. Host execution refused.")
        if attempt and self.root.exists():
            archive = self.root / "attempts" / f"attempt_{attempt - 1}"
            archive.mkdir(parents=True, exist_ok=False)
            for child in list(self.root.iterdir()):
                if child.name != "attempts":
                    shutil.move(str(child), str(archive / child.name))
        for folder in ["repository", "artifacts", "logs", "tests", "reports"]:
            (self.root / folder).mkdir(parents=True, exist_ok=False)
        (self.repo / "slugify.py").write_text(fixture.ORIGINAL)
        (self.repo / "test_slugify.py").write_text(fixture.TESTS)
        (self.repo / "README.md").write_text("# Slugify reference repository\nA controlled, dependency-free Python acceptance fixture. No remote.\nRun: python test_slugify.py\n")
        (self.repo / "CONTRIBUTING.md").write_text("Keep changes minimal. Preserve the public function. Run all regression tests.\n")
        await self.git("init", "-b", "main")
        await self.git("add", "--", "slugify.py", "test_slugify.py", "README.md", "CONTRIBUTING.md")
        await self.git("commit", "-m", "Initialize controlled reference fixture")
        await asyncio.to_thread(self._runtime)

    def _runtime(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        shutil.copy2(os.environ["BOUNTY_PYTHON"], runtime / "python")
        stdlib = Path(sysconfig.get_path("stdlib"))
        shutil.copytree(stdlib, runtime / "lib" / stdlib.name,
            ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "tests", "idlelib", "tkinter", "ensurepip"))
        # Only trusted runtime libraries, never host configuration, secrets or user files.
        library = (Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY")).resolve(strict=True)
        # The interpreter links the versioned ELF SONAME, not the development symlink.
        shutil.copy2(library, runtime / "lib" / library.name)
        machine = os.uname().machine
        arch = "aarch64-linux-gnu" if machine == "aarch64" else "x86_64-linux-gnu"
        for name in ["libc.so.6", "libm.so.6", "libz.so.1", "libexpat.so.1", "libffi.so.8", "libssl.so.3", "libcrypto.so.3"]:
            src = Path("/lib") / arch / name
            if src.exists():
                dest = self.root / "lib" / arch / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        loader = Path("/lib/ld-linux-aarch64.so.1") if machine == "aarch64" else Path("/lib64/ld-linux-x86-64.so.2")
        dest = self.root / loader.relative_to("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(loader, dest)
        for path in [self.root, *self.root.rglob("*")]:
            if path.is_dir():
                path.chmod(0o755)
            elif path.name.startswith("ld-linux"):
                path.chmod(0o755)
            elif path.name != "python":
                path.chmod(0o644)
        # Test interpreter is unprivileged, with read-only project/runtime and no host mount.

    async def tests(self):
        env = {**self.environment(), "BOUNTY_JOBS_DIR": os.environ["BOUNTY_JOBS_DIR"]}
        return await self.command([os.environ["BOUNTY_PYTHON"], str(Path(__file__).with_name("sandbox_launcher.py")), str(self.root)], env=env)