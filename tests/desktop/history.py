#!/usr/bin/env python3
"""Read collector-generated retained history through the built API; synthetic only."""
import argparse
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('oracle',ROOT/'tests/contracts/validate.py')
oracle=importlib.util.module_from_spec(spec);spec.loader.exec_module(oracle)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    out=args.output.resolve()
    if not out.is_relative_to(ROOT/'artifacts'):p.error('use repository artifacts')
    source=out/'synthetic/history/retained.json'
    stored=oracle.validate_history(source.read_bytes(),persisted=True)
    with tempfile.TemporaryDirectory(dir=out) as temp:
        data=Path(temp);(data/'history.json').write_bytes(source.read_bytes())
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        env={k:v for k,v in os.environ.items() if not k.startswith('METER_')}
        env.update(METER_DATA_DIR=str(data),METER_API_TOKEN='a'*64,METER_TIMEZONE='America/Los_Angeles',METER_API_BIND_ADDRESS='127.0.0.1',METER_API_PORT=str(port))
        process=subprocess.Popen([str(out/'meter-api')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        def get(auth=True):
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            try:
                c.request('GET','/v2/history',headers={'Authorization':'Bearer '+'a'*64} if auth else {})
                r=c.getresponse();return r.status,r.read(65537)
            finally:c.close()
        try:
            for attempt in range(50):
                try:status,raw=get();break
                except ConnectionError:time.sleep(.05)
            else:raise AssertionError('API did not start')
            assert status==200
            response=oracle.validate_history(raw)
            assert len(response['cycles'])==len(stored['cycles'])>=5
            assert len(raw)>4096,'history must exercise the separate response bound'
            for before,after in zip(stored['cycles'],response['cycles']):
                assert before['cycle']==after['cycle'] and before['timezone']==after['timezone']
                for old,new in zip(before['days'],after['days']):
                    if old['coverage']!='future':assert old==new,'historical value changed'
            assert get(False)[0]==401
            (data/'history.json').write_bytes(b' '*65537)
            assert get()[0]==503
            (data/'history.json').write_text('{')
            assert get()[0]==503
            (data/'history.json').unlink()
            (data/'history.json').symlink_to(source)
            assert get()[0]==503
            (data/'history.json').unlink()
            status,empty=get();assert status==200 and oracle.validate_history(empty)['cycles']==[]
            result={'result':'passed','retained_periods':len(response['cycles']),'response_bytes':len(raw),'authenticated':True,'oversized_corrupt_symlink_rejected':True}
            (out/'history-api.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
        finally:
            process.terminate();stdout,stderr=process.communicate(timeout=10)
            assert process.returncode==0 and not stdout and not stderr,'unclean API exit or logs'
if __name__=='__main__':main()
