"""
gpustat.web


MIT License

Copyright (c) 2018-2020 Jongwook Choi (@wookayin)
"""

from typing import List, Tuple, Optional
import os
import sys
import traceback
import urllib
import ssl

import asyncio
import asyncssh
import aiohttp

from datetime import datetime
from collections import OrderedDict, Counter

from termcolor import cprint, colored
from aiohttp import web
import aiohttp_jinja2 as aiojinja2
import queue




__PATH__ = os.path.abspath(os.path.dirname(__file__))

DEFAULT_GPUSTAT_COMMAND = "conda run -n gpustat gpustat --color --gpuname-width 25"


###############################################################################
# Background workers to collect information from nodes
###############################################################################

class Context(object):
    '''The global context object.'''
    def __init__(self):
        self.host_status = OrderedDict()
        self.host_gpu = OrderedDict()
        self.interval = 5.0
        self.queue = queue.Queue()
        # for _ in range(3):
        #     self.queue.put('uname -a')
            # self.queue.put('whoami')

    def host_set_message(self, hostname: str, msg: str):
        self.host_status[hostname] = colored(f"({hostname}) ", 'white') + msg + '\n'

context = Context()

async def add_jobs(poll_delay=None):
    if poll_delay is None:
        poll_delay = context.interval
    async def _loop_body():
        while True:
            if os.path.exists('jobs_in.txt'):
                with open('jobs_out.txt', 'a') as f_out, open('jobs_in.txt') as f_in:
                    for line in f_in:
                        line = line.strip()
                        if len(line) == 0:
                            continue
                        context.queue.put_nowait(line)
                        f_out.write(line+'\n')
                        print(f'added a job: {line}')
                os.remove('jobs_in.txt')
            await asyncio.sleep(poll_delay)

    while True:
        try:
            await _loop_body()

        except asyncio.CancelledError:
            print("local worker closed as being cancelled.")
            break
        except Exception as e:
            print(f"an error occured in local worker, {type(e).__name__}: {e}")
            raise

        # retry upon timeout/disconnected, etc.
        cprint(f"[{hostname:<{L}}] Disconnected, retrying in {poll_delay} sec...", color='yellow')
        await asyncio.sleep(poll_delay)

async def run_client(hostname: str, exec_cmd: str, *, port=22,
                     poll_delay=None, timeout=30.0,
                     name_length=None, verbose=False):
    '''An async handler to collect gpustat through a SSH channel.'''
    L = name_length or 0
    if poll_delay is None:
        poll_delay = context.interval

    async def _loop_body():
        # establish a SSH connection.
        async with asyncssh.connect(hostname, port=port) as conn:
            cprint(f"[{hostname:<{L}}] SSH connection established!", attrs=['bold'])

            while True:
                if False: #verbose: XXX DEBUG
                    print(f"[{hostname:<{L}}] querying... ")

                # query for web
                result = await asyncio.wait_for(conn.run(exec_cmd), timeout=timeout)

                now = datetime.now().strftime('%Y/%m/%d-%H:%M:%S.%f')
                if result.exit_status != 0:
                    cprint(f"[{now} [{hostname:<{L}}] Error, exitcode={result.exit_status}", color='red')
                    cprint(result.stderr or '', color='red')
                    stderr_summary = (result.stderr or '').split('\n')[0]
                    context.host_set_message(hostname, colored(f'[exitcode {result.exit_status}] {stderr_summary}', 'red'))
                else:
                    if verbose:
                        cprint(f"[{now} [{hostname:<{L}}] OK from gpustat ({len(result.stdout)} bytes)", color='cyan')
                    # update data
                    context.host_status[hostname] = result.stdout

                # query for running
                result = await asyncio.wait_for(conn.run("nvidia-smi | grep '%\|CUDA'"), timeout=timeout)
                if result.exit_status == 0:
                    cuda_ver = result.stdout.split('|')[1].split('Version: ')[-1].strip()
                    gpu_status = result.stdout.split('|')[3:]
                    gpu_dict = dict()
                    for i in range(len(gpu_status) // 4):
                        index = i * 4
                        gpu_state = str(gpu_status[index].split('   ')[2].strip())
                        gpu_power = int(gpu_status[index].split('   ')[-1].split('/')[0].split('W')[0].strip())
                        gpu_memory = int(gpu_status[index + 1].split('/')[0].split('M')[0].strip())
                        gpu_dict[i] = (gpu_state, gpu_power, gpu_memory)
                    context.host_gpu[hostname] = gpu_dict
                    # print(f'cuda ver: {cuda_ver}')
                    conda_env_dict = {
                        '10.1': 'mcr2_cuda101',
                        '10.2': 'mcr2_cuda102b',
                        '11.0': 'mcr2_cuda102b',
                        '11.5': 'mcr2_cuda116',
                        '11.6': 'mcr2_cuda116',
                        '11.7': 'mcr2_cuda116',
                        '11.8': 'mcr2_cuda116'
                    }

                    min_gpu_number = 2
                    available_gpus = []
                    for i, (gpu_state, gpu_power, gpu_memory) in gpu_dict.items():
                        if gpu_power <= 70 and gpu_memory <= 900:
                            gpu_str = f"GPU/id: {i}, GPU/state: {gpu_state}, GPU/memory: {gpu_memory}MiB, GPU/power: {gpu_power}W\n "
                            # print(gpu_str)
                            available_gpus.append(i)
                    if len(available_gpus) >= min_gpu_number and 'io88' not in hostname and 'io89' not in hostname:
                        # and 'io52' not in hostname
                        # print(f'{len(available_gpus)}>={min_gpu_number} gpus available on {hostname}')
                        try:
                            job = context.queue.get_nowait()
                            # command = f"CUDA_VISIBLE_DEVICES={available_gpus[0]},{available_gpus[1]} nohup conda run -n {conda_env_dict[cuda_ver]} {job} &"
                            command = f"CUDA_VISIBLE_DEVICES={available_gpus[0]},{available_gpus[1]} nohup conda run -n {conda_env_dict[cuda_ver]} sh -c '{job}' &"
                            # command = f"CUDA_VISIBLE_DEVICES={available_gpus[0]} nohup conda run -n {conda_env_dict[cuda_ver]} sh -c '{job}' &"
                            # 2 > / dev / null
                            print(hostname)
                            print(command)
                            # result = await asyncio.wait_for(conn.run(command), timeout=timeout)
                            # print(result.stdout)
                            await conn.create_process(command)
                            await asyncio.sleep(120) # wait 60 seconds for things to take memory, etc.
                            # print('create')
                        except queue.Empty:
                            # print('did not get a job from queue')
                            pass
                        except Exception as ex:
                            raise ex


                # wait for a while...
                await asyncio.sleep(poll_delay)

    while True:
        try:
            # start SSH connection, or reconnect if it was disconnected
            await _loop_body()

        except asyncio.CancelledError:
            cprint(f"[{hostname:<{L}}] Closed as being cancelled.", attrs=['bold'])
            break
        except (asyncio.TimeoutError) as ex:
            # timeout (retry)
            cprint(f"Timeout after {timeout} sec: {hostname}", color='red')
            context.host_set_message(hostname, colored(f"Timeout after {timeout} sec", 'red'))
        except (asyncssh.misc.DisconnectError, asyncssh.misc.ChannelOpenError, OSError) as ex:
            # error or disconnected (retry)
            cprint(f"Disconnected : {hostname}, {str(ex)}", color='red')
            context.host_set_message(hostname, colored(str(ex), 'red'))
        except Exception as e:
            # A general exception unhandled, throw
            cprint(f"[{hostname:<{L}}] {e}", color='red')
            context.host_set_message(hostname, colored(f"{type(e).__name__}: {e}", 'red'))
            cprint(traceback.format_exc())
            raise

        # retry upon timeout/disconnected, etc.
        cprint(f"[{hostname:<{L}}] Disconnected, retrying in {poll_delay} sec...", color='yellow')
        await asyncio.sleep(poll_delay)


async def spawn_clients(hosts: List[str], exec_cmd: str, *,
                        default_port: int, verbose=False):
    '''Create a set of async handlers, one per host.'''

    def _parse_host_string(netloc: str) -> Tuple[str, Optional[int]]:
        """Parse a connection string (netloc) in the form of `HOSTNAME[:PORT]`
        and returns (HOSTNAME, PORT)."""
        pr = urllib.parse.urlparse('ssh://{}/'.format(netloc))
        assert pr.hostname is not None, netloc
        return (pr.hostname, pr.port)

    try:
        host_names, host_ports = zip(*(_parse_host_string(host) for host in hosts))

        # initial response
        for hostname in host_names:
            context.host_set_message(hostname, "Loading ...")

        name_length = max(len(hostname) for hostname in host_names)

        # launch all clients parallel
        await asyncio.gather(*[
            run_client(hostname, exec_cmd, port=port or default_port,
                    verbose=verbose, name_length=name_length)
            for (hostname, port) in zip(host_names, host_ports)
        ], add_jobs())
    except Exception as ex:
        # TODO: throw the exception outside and let aiohttp abort startup
        traceback.print_exc()
        cprint(colored("Error: An exception occured during the startup.", 'red'))


###############################################################################
# webserver handlers.
###############################################################################

# monkey-patch ansi2html scheme. TODO: better color codes
import ansi2html
scheme = 'solarized'
ansi2html.style.SCHEME[scheme] = list(ansi2html.style.SCHEME[scheme])
ansi2html.style.SCHEME[scheme][0] = '#555555'
ansi_conv = ansi2html.Ansi2HTMLConverter(dark_bg=True, scheme=scheme)


def render_gpustat_body():
    body = ''
    for host, status in context.host_status.items():
        if not status:
            continue
        body += status
    return ansi_conv.convert(body, full=False)


async def handler(request):
    '''Renders the html page.'''

    data = dict(
        ansi2html_headers=ansi_conv.produce_headers().replace('\n', ' '),
        http_host=request.host,
        interval=int(context.interval * 1000)
    )
    response = aiojinja2.render_template('index.html', request, data)
    response.headers['Content-Language'] = 'en'
    return response


async def websocket_handler(request):
    print("INFO: Websocket connection from {} established".format(request.remote))

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    async def _handle_websocketmessage(msg):
        if msg.data == 'close':
            await ws.close()
        else:
            # send the rendered HTML body as a websocket message.
            body = render_gpustat_body()
            await ws.send_str(body)

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.CLOSE:
            break
        elif msg.type == aiohttp.WSMsgType.TEXT:
            await _handle_websocketmessage(msg)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            cprint("Websocket connection closed with exception %s" % ws.exception(), color='red')

    print("INFO: Websocket connection from {} closed".format(request.remote))
    return ws

###############################################################################
# app factory and entrypoint.
###############################################################################

def create_app(*,
               hosts=['localhost'],
               default_port: int = 22,
               ssl_certfile: Optional[str] = None,
               ssl_keyfile: Optional[str] = None,
               exec_cmd: Optional[str] = None,
               verbose=True):
    if not exec_cmd:
        exec_cmd = DEFAULT_GPUSTAT_COMMAND

    app = web.Application()
    app.router.add_get('/', handler)
    app.add_routes([web.get('/ws', websocket_handler)])

    async def start_background_tasks(app):
        clients = spawn_clients(
            hosts, exec_cmd, default_port=default_port, verbose=verbose)
        # See #19 for why we need to this against aiohttp 3.5, 3.8, and 4.0
        loop = app.loop if hasattr(app, 'loop') else asyncio.get_event_loop()
        app['tasks'] = loop.create_task(clients)
        await asyncio.sleep(0.1)
    app.on_startup.append(start_background_tasks)

    async def shutdown_background_tasks(app):
        cprint(f"... Terminating the application", color='yellow')
        app['tasks'].cancel()
    app.on_shutdown.append(shutdown_background_tasks)

    # jinja2 setup
    import jinja2
    aiojinja2.setup(app,
                    loader=jinja2.FileSystemLoader(
                        os.path.join(__PATH__, 'template'))
                    )

    # SSL setup
    if ssl_certfile and ssl_keyfile:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=ssl_certfile,
                                    keyfile=ssl_keyfile)

        cprint(f"Using Secure HTTPS (SSL/TLS) server ...", color='green')
    else:
        ssl_context = None   # type: ignore
    return app, ssl_context


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hosts', nargs='*',
                        help='List of nodes. Syntax: HOSTNAME[:PORT]')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--port', type=int, default=48109,
                        help="Port number the web application will listen to. (Default: 48109)")
    parser.add_argument('--ssh-port', type=int, default=22,
                        help="Default SSH port to establish connection through. (Default: 22)")
    parser.add_argument('--interval', type=float, default=5.0,
                        help="Interval (in seconds) between two consecutive requests.")
    parser.add_argument('--ssl-certfile', type=str, default=None,
                        help="Path to the SSL certificate file (Optional, if want to run HTTPS server)")
    parser.add_argument('--ssl-keyfile', type=str, default=None,
                        help="Path to the SSL private key file (Optional, if want to run HTTPS server)")
    parser.add_argument('--exec', type=str,
                        default=DEFAULT_GPUSTAT_COMMAND,
                        help="command-line to execute (e.g. gpustat --color --gpuname-width 25)")
    args = parser.parse_args()

    hosts = args.hosts or ['localhost']
    cprint(f"Hosts : {hosts}", color='green')
    cprint(f"Cmd   : {args.exec}", color='yellow')

    if args.interval > 0.1:
        context.interval = args.interval

    app, ssl_context = create_app(
        hosts=hosts, default_port=args.ssh_port,
        ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
        exec_cmd=args.exec,
        verbose=args.verbose)

    web.run_app(app, host='0.0.0.0', port=args.port,
                ssl_context=ssl_context)

if __name__ == '__main__':
    main()
