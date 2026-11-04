"""Check final validation prerequisites without exposing credentials.

--postgres runs the existing concurrency test against the explicitly configured
disposable database. --gemini runs the opt-in generic live-provider test.
Neither is run implicitly and neither writes to the commercial database.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from dotenv import dotenv_values
from sqlalchemy.engine import make_url


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--gemini',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    env=os.environ.copy()
    settings=dotenv_values(root/'.env')
    url=env.get('PHASE6_POSTGRES_TEST_URL') or settings.get('PHASE6_POSTGRES_TEST_URL')
    postgres='not_configured'
    if url:
        try:
            parsed=make_url(url)
            postgres='ready' if parsed.get_backend_name()=='postgresql' and (parsed.database or '').endswith('_test') else 'invalid_test_database'
        except Exception:postgres='invalid_configuration'
    report={'postgres_prerequisite':postgres,'postgres_test':'not_run',
            'gemini_test':'not_run','android_devices':0,'physical_android_test':'requires_manual_validation',
            'microphone_test':'requires_manual_validation'}
    adb=Path(env.get('LOCALAPPDATA',''))/'Android/Sdk/platform-tools/adb.exe'
    if adb.is_file():
        try:
            result=subprocess.run([str(adb),'devices'],capture_output=True,text=True,timeout=15)
            report['android_devices']=sum(line.endswith('\tdevice') for line in result.stdout.splitlines())
        except (OSError,subprocess.TimeoutExpired):report['android_detection']='unavailable'
    env['OMP_NUM_THREADS']='1';env['OPENBLAS_NUM_THREADS']='1'
    def run(file):
        result=subprocess.run([sys.executable,'-m','pytest',file,'-q','-p','no:cacheprovider'],cwd=root,env=env)
        return 'passed' if result.returncode==0 else 'failed'
    if args.postgres and postgres=='ready':
        env['PHASE6_POSTGRES_TEST_URL']=url
        report['postgres_test']=run('tests/test_phase6_postgres.py')
    if args.gemini:
        env['PHASE6_LIVE_GEMINI']='1'
        report['gemini_test']=run('tests/test_live_analytics.py')
    # No connection strings, device identifiers, account names or provider keys.
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if 'failed' in report.values():return 1
    if args.postgres and postgres!='ready':return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
