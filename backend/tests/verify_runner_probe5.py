"""Probe 5: is update_network deny-all a RELIABLE egress lockdown?"""
import asyncio, os, sys
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from bounty.secrets import SecretVault

ONE = ("python3 -c \"import socket; socket.setdefaulttimeout(5)\n"
       "try:\n socket.create_connection(('{h}',443)); print('{h}','REACHABLE')\n"
       "except Exception as e:\n print('{h}','BLOCKED',type(e).__name__)\"")


async def rc(sb, cmd, t=25):
    from e2b import CommandExitException, TimeoutException
    try:
        r = await sb.commands.run(cmd, user='user', timeout=t)
        return (r.stdout or '') + (r.stderr or '')
    except CommandExitException as e:
        return (e.stdout or '') + (e.stderr or '')
    except TimeoutException:
        return 'CMD_TIMEOUT(likely blocked/hung)'


async def main():
    from e2b import AsyncSandbox
    key = SecretVault().get('e2b')
    sb = await AsyncSandbox.create(api_key=key, api_url=os.environ['E2B_API_URL'], template='openclaw',
        timeout=90, secure=True, metadata={'probe': 'egress5'})
    print('id', sb.sandbox_id)
    print('baseline github:', (await rc(sb, ONE.format(h='github.com'))).strip())
    await sb.update_network({'allow_out': [], 'deny_out': ['0.0.0.0/0']})
    await asyncio.sleep(4)
    for h in ['1.1.1.1', 'pypi.org', 'github.com']:
        print(f'lockdown {h}:', (await rc(sb, ONE.format(h=h))).strip())
    await sb.kill()
    print('done')


asyncio.run(main())
