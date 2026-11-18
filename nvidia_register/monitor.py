"""One scheduled collection pass, with a persistent local action queue."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request,urlopen
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from nvidia_sync import sync,locked
import earnings


def tick(root,get,notify):
    data=root/'register_data'
    with locked(data/'monitor'):
        db=sqlite3.connect(data/'monitor.sqlite3')
        db.execute("CREATE TABLE IF NOT EXISTS notices (key TEXT PRIMARY KEY,acc TEXT,message TEXT,active INTEGER,sent INTEGER,updated TEXT)")
        now=datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec='seconds')
        def notice(key,acc,msg):
            db.execute("""INSERT INTO notices VALUES(?,?,?,1,0,?)
                ON CONFLICT(key) DO UPDATE SET active=1,sent=0,message=excluded.message,updated=excluded.updated
                WHERE notices.active=0 OR notices.message!=excluded.message""",(key,acc,msg,now))
        def resolve(key):
            db.execute('UPDATE notices SET active=0 WHERE key=?',(key,))
        try:
            sync(root,get)
            resolve('sync')
        except (ValueError,KeyError,OSError) as e:
            notice('sync','','SEC取得失敗：'+str(e))
        records={r['accession_number']:r for r in json.loads((root/'section_manifest.json').read_text())['filings']}
        for p in sorted((root/'nvidia_inbox').glob('*/record.json')):
            r=json.loads(p.read_text()); acc=r['accession_number']
            if not db.execute('SELECT 1 FROM notices WHERE key=?',('new:'+acc,)).fetchone():
                notice('new:'+acc,acc,'新着8-K：台帳で確認してください')
            records[acc]=r
            runs=root/'inbox_reviews'/acc/r['sha256']
            for folder in runs.glob('section-*'):
                logs=sorted(folder.glob('*/run.json'))
                if not logs: continue
                states=[json.loads(p.read_text())['status'] for p in logs]
                key='run:'+acc+':'+folder.name
                if 'completed_pending_review' in states: resolve(key)
                elif states[-1]!='preparing': notice(key,acc,'本文抽出失敗 '+folder.name+' '+states[-1])
        for acc,r in records.items():
            if '2.02' not in r['api_items']: continue
            try:
                earnings.collect(root,r,get)
                resolve('metrics:'+acc)
            except (OSError,ValueError,KeyError) as e:
                notice('metrics:'+acc,acc,'決算指標の抽出要確認：'+str(e))
        db.commit()
        pending=db.execute('SELECT key,acc,message FROM notices WHERE active=1 AND sent=0 ORDER BY updated,key').fetchall()
        if pending and notify(pending):
            with db: db.executemany('UPDATE notices SET sent=1 WHERE key=?',[(p[0],) for p in pending])
        db.close()
        return len(pending)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--contact',required=True)
    args=parser.parse_args()
    def fetch(url):
        time.sleep(.3)
        with urlopen(Request(url,headers={'User-Agent':'CompanyDisclosureMonitor/1.1 '+args.contact}),timeout=40) as response:
            raw=response.read(16_000_001)
        if len(raw)>16_000_000: raise ValueError("資料のサイズを確認してください")
        return raw
    def notify(rows):
        message='NVIDIA Monitor: '+str(len(rows))+' 件。'+rows[0][2]+' '+rows[0][1]+'  http://localhost:8774/'
        print(message,flush=True)
        try:
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                '[Console]::InputEncoding=[Text.UTF8Encoding]::new($false); $u=$env:USERNAME; $s=[Console]::In.ReadToEnd(); & msg.exe $u /TIME:60 $s; exit $LASTEXITCODE'],
                input=message.encode('utf-8'),timeout=20,capture_output=True)
            return result.returncode==0
        except (OSError,subprocess.TimeoutExpired):
            return False
    print(json.dumps({'notifications_pending_at_start_of_delivery':tick(ROOT,fetch,notify)}))
