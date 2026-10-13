"""Probe 3: verify the correct ALL_TRAFFIC egress control (IP + hostname)."""
import asyncio, os, sys
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from bounty.secrets import SecretVault

IP = ("python3 -c \"import socket; socket.setdefaulttimeout(6)\n"
      "try:\n socket.create_connection(('1.1.1.1',443)); print('IP_REACHABLE')\n"
      "except Exception as e:\n print('IP_BLOCKED', type(e).__name__)\"")
HOST = ("python3 -c \"import socket; socket.setdefaulttimeout(6)\n"
        "try:\n socket.create_connection(('example.com',443)); print('HOST_REACHABLE')\n"
        "except Exception as e:\n print('HOST_BLOCKED', type(e).__name__)\"")
GH = ("python3 -c \"import socket; socket.setdefaulttimeout(8)\n"
      "try:\n socket.create_connection(('github.com',443)); print('GH_REACHABLE')\n"
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
    sb = await AsyncSandbox.create(api_key=key, api_url=os.environ['E2B_API_URL'], template='openclaw',
        timeout=90, secure=True,
        network={'allow_out': ['github.com'], 'deny_out': ['ALL_TRAFFIC']}, metadata={'probe': 'egress-correct'})
    print('id', sb.sandbox_id)
    print('allow=[github] ip 1.1.1.1 :', (await rc(sb, IP)).strip())
    print('allow=[github] host example:', (await rc(sb, HOST)).strip())
    print('allow=[github] github     :', (await rc(sb, GH)).strip())
    await sb.update_network({'deny_out': ['ALL_TRAFFIC']})
    print('lockdown github           :', (await rc(sb, GH)).strip())
    print('lockdown ip 1.1.1.1       :', (await rc(sb, IP)).strip())
    await sb.kill()
    print('done')


asyncio.run(main())
