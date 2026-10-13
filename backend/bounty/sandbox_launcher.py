"""Trusted launcher. Only used for the bundled reference fixture, not arbitrary repos."""
import ctypes
import ctypes.util
import errno
import os
from pathlib import Path
import resource
import sys


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    if root.parent != Path(os.environ["BOUNTY_JOBS_DIR"]).resolve() or not root.name.startswith("job_"):
        raise RuntimeError("Invalid sandbox root")
    lib = ctypes.CDLL(ctypes.util.find_library("seccomp"), use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)
    if not ctx:
        raise RuntimeError("Sandbox filter unavailable")
    blocked = "socket socketpair connect bind listen accept accept4 sendto sendmsg sendmmsg recvfrom recvmsg recvmmsg ptrace process_vm_readv process_vm_writev mount umount2 pivot_root unshare setns bpf keyctl add_key request_key open_by_handle_at perf_event_open reboot kexec_load init_module finit_module delete_module clone clone3 fork vfork setuid setgid setresuid setresgid setgroups chroot".split()
    for name in blocked:
        nr = lib.seccomp_syscall_resolve_name(name.encode())
        if nr >= 0 and lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, nr, 0) != 0:
            raise RuntimeError("Cannot install sandbox rule")
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.chroot(root)
    os.chdir("/repository")
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or lib.seccomp_load(ctx) != 0:
        raise RuntimeError("No-new-privileges/seccomp failed; execution refused")
    lib.seccomp_release(ctx)
    os.execve("/runtime/python", ["python", "-B", "/repository/test_slugify.py"], {
        "PYTHONHOME": "/runtime", "LD_LIBRARY_PATH": "/runtime/lib", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})


if __name__ == "__main__":
    main()