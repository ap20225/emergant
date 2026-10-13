"""Probe 2: determine which E2B switch actually enforces egress denial."""
import asyncio, os, sys
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from bounty.secrets import SecretVault

NETTEST = ("python3 -c \"import socket; socket.setdefaulttimeout(6)\n"
           "import sys\ntry:\n socket.create_connection(('1.1.1.1',443)); print('REACHABLE')\n"
           "except Exception as e:\n print('BLOCKED', type(e).__name__)\"")
GHTEST = ("python3 -c \"import socket; socket.setdefaulttimeout(6)\n"
          "import sys\ntry:\n socket.create_connection(('140.82.112.3',443)); print('GH_REACHABLE')\n"
          "except Exception as e:\n print('GH_BLOCKED', type(e).__name__)\"")


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
    # Case A: allow_internet_access=False, no network
    sb = await AsyncSandbox.create(api_key=key, api_url=os.environ['E2B_API_URL'], template='openclaw',
        timeout=80, secure=True, allow_internet_access=False, metadata={'probe': 'egress-A'})
    print('A id', sb.sandbox_id)
    print('A internet-off 1.1.1.1:', (await rc(sb, NETTEST)).strip())
    print('A internet-off github :', (await rc(sb, GHTEST)).strip())
    await sb.kill()
    # Case B: default internet on, then update_network deny all
    sb = await AsyncSandbox.create(api_key=key, api_url=os.environ['E2B_API_URL'], template='openclaw',
        timeout=80, secure=True, allow_internet_access=True,
        network={'allow_out': ['github.com']}, metadata={'probe': 'egress-B'})
    print('B id', sb.sandbox_id)
    print('B allow=[github] 1.1.1.1:', (await rc(sb, NETTEST)).strip())
    await sb.update_network({'deny_out': ['ALL_TRAFFIC']})
    print('B after deny_out github :', (await rc(sb, GHTEST)).strip())
    print('B after deny_out 1.1.1.1:', (await rc(sb, NETTEST)).strip())
    await sb.kill()
    print('done')


asyncio.run(main())
