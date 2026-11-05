"""Run acceptance against a private short-lived PostgreSQL cluster on loopback."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def main():
    root=Path(__file__).resolve().parents[1]
    binaries=Path(os.environ.get('POSTGRES_BIN','C:/Program Files/PostgreSQL/18/bin'))
    for name in ('initdb.exe','pg_ctl.exe','createdb.exe'):
        if not (binaries/name).is_file():
            raise RuntimeError('PostgreSQL tools unavailable; set POSTGRES_BIN.')
    work=Path(tempfile.mkdtemp(prefix='.test-pg-',dir=root)).resolve()
    assert work.is_relative_to(root)
    data=work/'data'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    def run(name,args,**kwargs):
        kwargs.pop('capture_output',None);kwargs.pop('text',None)
        with (work/'commands.log').open('ab') as log:
            return subprocess.run([str(binaries/name),*args],cwd=root,creationflags=flags,check=True,stdout=log,stderr=log,timeout=90,**kwargs)
    run('initdb.exe',['-D',str(data),'-U','postgres','--auth=trust','--encoding=UTF8','--no-locale'],capture_output=True,text=True)
    started=False
    try:
        run('pg_ctl.exe',['-D',str(data),'-l',str(work/'postgres.log'),'-o',f'-h 127.0.0.1 -p {port} -F','-w','start'],capture_output=True,text=True)
        started=True
        run('createdb.exe',['-h','127.0.0.1','-p',str(port),'-U','postgres','fashionstore_acceptance_test'],capture_output=True,text=True)
        env=os.environ.copy()
        env.update(PHASE6_POSTGRES_TEST_URL=f'postgresql://postgres@127.0.0.1:{port}/fashionstore_acceptance_test',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
        print('Temporary loopback PostgreSQL ready; commercial database untouched.',flush=True)
        result=subprocess.run([sys.executable,'-m','pytest','tests/test_phase6_postgres.py','tests/test_postgres_commercial.py','tests/test_postgres_migrations.py','-q','-p','no:cacheprovider','--tb=short'],cwd=root,env=env,creationflags=flags,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
        print(result.stdout,flush=True)
        if result.stderr: print(result.stderr,flush=True)
        return result.returncode
    finally:
        if started:
            run('pg_ctl.exe',['-D',str(data),'-m','fast','-w','stop'],capture_output=True,text=True)
            print('Temporary PostgreSQL stopped.',flush=True)


if __name__=='__main__':
    sys.exit(main())
