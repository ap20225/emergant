"""E2B only. Never starts processes or repository code on the application host."""
import os
import shlex
from fastapi import HTTPException
from .secrets import SecretVault
from .store import now, redact


TEST_COMMANDS = {'pytest': 'python -m pytest -q', 'unittest': 'python -m unittest discover -v',
                 'jest': 'npm test -- --runInBand', 'vitest': 'npm test -- --run', 'go': 'go test ./...'}


def clone_spec(job, connection):
    """The clone/checkout target is the AUTHORIZED EXECUTION repository (a fork for third-party work),
    falling back to the connected repository for the legacy owner-repo controlled flow."""
    repo = job.get('execution_repo') or (connection or {}).get('repository') or job.get('repository')
    branch = job.get('execution_default_branch') or (connection or {}).get('default_branch') or job.get('base_branch')
    return repo, branch


class RemoteRunner:
    def __init__(self, store):
        self.store = store
        self.sandbox = None
        self.job_id = None
        self.workdir = os.environ['BOUNTY_REMOTE_WORKDIR']

    async def preflight(self):
        c = await self.store.controls()
        if any(c.get(k) for k in ['global_stop', 'agents_stopped', 'spending_stopped', 'integrations_revoked']):
            raise HTTPException(409, 'Runner stopped by emergency policy')
        settings = await self.store.settings()
        if settings['mode'] not in ('ASSISTED', 'AUTONOMOUS'):
            raise HTTPException(409, 'ASSISTED or AUTONOMOUS mode is required for real execution')
        config = await self.store.one('integration_connections', {'id': 'runner'})
        if not config or not config.get('template_id') or not SecretVault().has('e2b'):
            raise HTTPException(409, 'RUNNER_REQUIRED: configure an E2B isolated sandbox and a dependency-ready template in Security')
        attestation = await self.store.one('execution_attestations', {'id': 'runner-verification'})
        if not attestation or not attestation.get('verified'):
            raise HTTPException(409, 'RUNNER_UNVERIFIED: run the E2B runtime verification before any real execution')

    async def verify(self, actor):
        """One short isolated sandbox launch that proves capabilities. No repository code is run."""
        c = await self.store.controls()
        if any(c.get(k) for k in ['global_stop', 'agents_stopped', 'integrations_revoked']):
            raise HTTPException(409, 'Runner verification blocked by emergency policy')
        settings = await self.store.settings()
        if settings['mode'] not in ('ASSISTED', 'AUTONOMOUS'):
            raise HTTPException(409, 'ASSISTED or AUTONOMOUS mode is required')
        config = await self.store.one('integration_connections', {'id': 'runner'})
        if not config or not config.get('template_id') or not SecretVault().has('e2b'):
            raise HTTPException(409, 'RUNNER_REQUIRED: configure an E2B key and template first')
        from e2b import AsyncSandbox, CommandExitException
        checks, sandbox = {}, None
        try:
            sandbox = await AsyncSandbox.create(api_key=SecretVault().get('e2b'), api_url=os.environ['E2B_API_URL'],
                template=config['template_id'], timeout=int(os.environ['RUNNER_VERIFY_TIMEOUT_SECONDS']),
                secure=True, envs={}, metadata={'penniless_verify': 'runtime'})

            async def run(cmd, t=45):
                try:
                    r = await sandbox.commands.run(cmd, user='user', timeout=t)
                    return r.exit_code, ((r.stdout or '') + (r.stderr or '')).strip()
                except CommandExitException as exc:
                    return exc.exit_code, ((exc.stdout or '') + (exc.stderr or '')).strip()

            for label, cmd in [('user', 'whoami'), ('uid', 'id -u'), ('git', 'git --version'),
                               ('python', 'python3 --version'), ('pip', 'python3 -m pip --version'), ('node', 'node --version')]:
                code, out = await run(cmd)
                checks[label] = {'exit_code': code, 'output': redact(out)[:200]}
            # Prove file write + read-back: real execution writes patches via sandbox.files.write.
            try:
                marker = 'penniless-verify-' + os.urandom(8).hex()
                target = os.environ['BOUNTY_REMOTE_HOME'] + '/penniless_verify.txt'
                await sandbox.files.write(target, marker, user='user')
                code, out = await run('cat ' + shlex.quote(target))
                checks['file_write'] = {'exit_code': code, 'output': redact(out)[:200], 'match': out.strip() == marker}
            except Exception as exc:
                checks['file_write'] = {'exit_code': 1, 'output': type(exc).__name__, 'match': False}
            await sandbox.update_network({'allow_out': [], 'deny_out': ['0.0.0.0/0']})
            probe = ("python3 -c \"import socket; socket.setdefaulttimeout(6)\n"
                     "def t(h):\n try:\n  socket.create_connection((h,443)); return 'REACHABLE'\n except Exception as e:\n  return 'BLOCKED:'+type(e).__name__\n"
                     "print('host', t('pypi.org')); print('ip', t('1.1.1.1'))\"")
            code, out = await run(probe, 40)
            checks['egress_after_lockdown'] = {'exit_code': code, 'output': out[:200]}
        finally:
            if sandbox is not None:
                try:
                    await sandbox.kill()
                except Exception:
                    pass
        non_root = checks.get('user', {}).get('output') not in ('root', '') and checks.get('uid', {}).get('output') not in ('0', '')
        has_git = checks.get('git', {}).get('exit_code') == 0
        has_python = checks.get('python', {}).get('exit_code') == 0
        file_write_ok = checks.get('file_write', {}).get('match') is True
        egress = checks.get('egress_after_lockdown', {}).get('output') or ''
        dns_blocked = 'host BLOCKED' in egress
        raw_ip_open = 'ip REACHABLE' in egress
        verified = bool(non_root and has_git and has_python and file_write_ok)
        attestation = {'id': 'runner-verification', 'verified': verified, 'template_id': config['template_id'],
            'non_root': non_root, 'git': has_git, 'python': has_python, 'file_write': file_write_ok,
            'pip': checks.get('pip', {}).get('exit_code') == 0, 'node': checks.get('node', {}).get('exit_code') == 0,
            'dns_egress_blocked': dns_blocked, 'raw_ip_egress_open': raw_ip_open, 'checks': checks,
            'network_note': 'After lockdown DNS/hostname egress is blocked; raw-IP egress remains possible (partial control).',
            'verified_at': now()}
        await self.store.db.execution_attestations.update_one({'id': 'runner-verification'}, {'$set': attestation}, upsert=True)
        await self.store.db.integration_connections.update_one({'id': 'runner'}, {'$set': {'status': 'RUNTIME_VERIFIED' if verified else 'VERIFICATION_FAILED', 'verified_at': now()}})
        await self.store.audit('security', 'E2B runtime verification',
            f'verified={verified}; non_root={non_root}; git={has_git}; python={has_python}; file_write={file_write_ok}; dns_egress_blocked={dns_blocked}; raw_ip_open={raw_ip_open}',
            'PASS' if verified else 'BLOCKED', actor=actor)
        return attestation

    async def open(self, job, connection):
        await self.preflight()
        await self._create(job, connection)

    async def _create(self, job, connection):
        from e2b import AsyncSandbox
        self.job_id = job['id']
        config = await self.store.one('integration_connections', {'id': 'runner'})
        self.sandbox = await AsyncSandbox.create(api_key=SecretVault().get('e2b'), api_url=os.environ['E2B_API_URL'], template=config['template_id'],
            timeout=int(os.environ['RUNNER_HARD_TIMEOUT_SECONDS']), secure=True, envs={},
            metadata={'penniless_job': job['id']})
        await self.store.db.jobs.update_one({'id': job['id']}, {'$set': {'sandbox_id': self.sandbox.sandbox_id}})
        try:
            repo, default_branch = clone_spec(job, connection)
            url = os.environ['GITHUB_WEB_URL'] + '/' + repo + '.git'
            await self.command('mkdir -p ' + shlex.quote(self.workdir), cwd=os.environ['BOUNTY_REMOTE_HOME'])
            await self.git('clone', '--no-checkout', '--depth', '1', '--branch', default_branch, '--', url, 'repository', cwd=self.workdir)
            # No arbitrary repository file has run before hostname egress is removed.
            await self.sandbox.update_network({'allow_out': [], 'deny_out': ['0.0.0.0/0']})
            await self.store.audit('security', 'Runner egress locked down', 'Public clone completed; hostname/DNS egress removed before repository execution (raw-IP egress may remain per E2B partial control)', job_id=self.job_id)
            await self.git('checkout', '--detach', job['base_sha'])
            await self.git('checkout', '-b', job['branch'])
        except BaseException:
            await self.close()
            raise

    async def command(self, command, cwd=None, check=True):
        if self.sandbox is None:
            raise HTTPException(409, 'RUNNER_REQUIRED: no isolated workspace')
        c = await self.store.controls()
        if any(c.get(k) for k in ['global_stop', 'agents_stopped', 'integrations_revoked']):
            raise HTTPException(409, 'Runner command stopped by emergency policy')
        from e2b import CommandExitException
        await self.store.audit('coder', 'Isolated command started', redact(command)[:1000], job_id=self.job_id)
        try:
            result = await self.sandbox.commands.run(command, cwd=cwd or self.workdir + '/repository',
                user='user', timeout=int(os.environ['RUNNER_COMMAND_TIMEOUT_SECONDS']), envs={'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'})
        except CommandExitException as exc:
            result = exc
        output = redact((result.stdout + '\n' + result.stderr)[-24000:])
        value = {'exit_code': result.exit_code, 'output': output}
        await self.store.audit('tester', 'Isolated command finished', f'Exit {result.exit_code}', 'PASS' if result.exit_code == 0 else 'FAIL', self.job_id)
        if check and result.exit_code != 0:
            raise HTTPException(409, 'Isolated command failed; inspect preserved test evidence')
        return value

    async def git(self, *args, cwd=None):
        fixed = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'protocol.file.allow=never',
                 '-c', 'protocol.ext.allow=never', '-c', 'core.fsmonitor=false', '-c', 'diff.external=',
                 '-c', 'user.name=' + os.environ['BOUNTY_GIT_AUTHOR_NAME'],
                 '-c', 'user.email=' + os.environ['BOUNTY_GIT_AUTHOR_EMAIL']]
        return (await self.command(shlex.join([*fixed, *args]), cwd=cwd))['output'].strip()

    async def write(self, path, content):
        from .patches import safe_path
        safe_path(path)
        await self.safe_target(path)
        await self.sandbox.files.write(self.workdir + '/repository/' + path, content, user='user')

    async def safe_target(self, path):
        from .patches import safe_path
        safe_path(path)
        # Trusted validation script executes only inside the isolated remote VM.
        script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); parts=[p,*p.parents]; assert not any(x.is_symlink() for x in parts); assert not p.exists() or p.is_file()"
        await self.command('python -I -c ' + shlex.quote(script) + ' ' + shlex.quote(path))

    async def tests(self, suite):
        if suite not in TEST_COMMANDS:
            raise HTTPException(422, 'Unsupported test suite')
        return await self.command(TEST_COMMANDS[suite], check=False)

    async def close(self):
        if self.sandbox is not None:
            try:
                await self.sandbox.kill()
            finally:
                self.sandbox = None