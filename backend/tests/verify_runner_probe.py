"""Standalone REAL E2B runner probe. Not wired into app; de-risks the network API."""
import asyncio, os, sys
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from bounty.secrets import SecretVault


async def run_cmd(sb, cmd):
    from e2b import CommandExitException
    try:
        r = await sb.commands.run(cmd, user='user', timeout=45)
    except CommandExitException as e:
        r = e
    return r.exit_code, (r.stdout or '') + (r.stderr or '')


async def main():
    from e2b import AsyncSandbox
    key = SecretVault().get('e2b')
    print('e2b key present:', bool(key))
    sb = await AsyncSandbox.create(
        api_key=key, api_url=os.environ['E2B_API_URL'], template='openclaw',
        timeout=90, secure=True,
        network={'allow_out': ['github.com'], 'deny_out': ['0.0.0.0/0']},
        metadata={'probe': 'runner-verify'})
    print('sandbox_id:', sb.sandbox_id)
    try:
        for label, cmd in [
            ('whoami', 'whoami'),
            ('uid', 'id -u'),
            ('git', 'git --version'),
            ('python', 'python3 --version'),
            ('pip', 'python3 -m pip --version'),
            ('node', 'node --version'),
            ('pytest', 'python3 -m pytest --version'),
            ('blocked_host', "python3 -c \"import socket; socket.setdefaulttimeout(6)\nimport sys\ntry:\n socket.create_connection(('1.1.1.1',443)); print('REACHABLE')\nexcept Exception as e:\n print('BLOCKED')\""),
        ]:
            code, out = await run_cmd(sb, cmd)
            print(f'[{label}] exit={code} :: {out.strip()[:200]}')
        # lock down fully
        await sb.update_network({'allow_out': [], 'deny_out': ['0.0.0.0/0']})
        code, out = await run_cmd(sb, "python3 -c \"import socket; socket.setdefaulttimeout(6)\ntry:\n socket.create_connection(('140.82.112.3',443)); print('GH_REACHABLE')\nexcept Exception as e:\n print('GH_BLOCKED')\"")
        print(f'[github_after_lockdown] exit={code} :: {out.strip()[:200]}')
    finally:
        await sb.kill()
        print('killed')


asyncio.run(main())
