"""Probe 4: decisive egress-enforcement check (hostname + IP + curl, with settle)."""
import asyncio, os, sys
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from bounty.secrets import SecretVault

CHECK = ("python3 -c \"import socket; socket.setdefaulttimeout(6)\n"
         "for tgt in [('example.com',443),('1.1.1.1',443),('github.com',443),('pypi.org',443)]:\n"
         " try:\n  socket.create_connection(tgt); print(tgt[0],'REACHABLE')\n"
         " except Exception as e:\n  print(tgt[0],'BLOCKED',type(e).__name__)\"")


async def rc(sb, cmd):
    from e2b import CommandExitException
    try:
        r = await sb.commands.run(cmd, user='user', timeout=45)
    except CommandExitException as e:
        r = e
    return (r.stdout or '') + (r.stderr or '')


async def main():
    from e2b import AsyncSandbox
    key = SecretVault().get('e2b')
    sb = await AsyncSandbox.create(api_key=key, api_url=os.environ['E2B_API_URL'], template='openclaw',
        timeout=90, secure=True,
        network={'allow_out': ['github.com'], 'deny_out': ['0.0.0.0/0']}, metadata={'probe': 'egress4'})
    print('id', sb.sandbox_id, '| allow=[github.com] deny=[0.0.0.0/0]')
    await asyncio.sleep(3)
    print('--- create-time rules ---')
    print((await rc(sb, CHECK)).strip())
    await sb.update_network({'allow_out': [], 'deny_out': ['0.0.0.0/0']})
    await asyncio.sleep(3)
    print('--- after full lockdown (allow=[]) ---')
    print((await rc(sb, CHECK)).strip())
    await sb.kill()
    print('done')


asyncio.run(main())
