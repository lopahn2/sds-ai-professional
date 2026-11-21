"""폐기 API+실제 Chromium. 실배포나 운영 자격은 사용하지 않는다."""
import socket
import subprocess
import threading
import time
from pathlib import Path
import uvicorn
from operator_gateway_support import local_gateway
from test_core_support import Harness


def test_real_browser_minimum_summary_and_role_entry():
    h=Harness(); iid=h.create()
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(h.app,log_level='critical',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and thread.is_alive() and time.monotonic()<deadline: time.sleep(.02)
        assert server.started
        with local_gateway(h, port) as url:
            result=subprocess.run(['node','liveprobe/application_tests/test_human_browser.mjs',url,iid],cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=45)
        assert result.returncode==0,result.stdout+result.stderr
        print(result.stdout)
    finally:
        server.should_exit=True; thread.join(10); sock.close(); h.close()
        assert not thread.is_alive()
