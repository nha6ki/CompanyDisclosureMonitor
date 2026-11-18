"""Install the daily Windows task from within WSL."""
import argparse
import base64
import os
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def quote(value):
    return "'"+value.replace("'","''")+"'"


def install(contact,at):
    distro=os.environ.get('WSL_DISTRO_NAME','')
    if not distro or not re.fullmatch(r'[\w.-]+',distro):
        raise ValueError('WSLのUbuntu内で実行してください')
    if not re.fullmatch(r'[a-zA-Z0-9._+%-]+@[a-zA-Z0-9.-]+',contact):
        raise ValueError('連絡先のメールアドレスを指定してください')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at): raise ValueError('時刻はHH:MMです')
    args=['-d',distro,'--exec',str(ROOT/'.venv/bin/python'),str(ROOT/'nvidia_register/monitor.py'),'--contact',contact]
    script=r"""
$ErrorActionPreference='Stop'
$action=New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wsl.exe" -Argument ARGUMENTS
$trigger=New-ScheduledTaskTrigger -Daily -At TIMEVALUE
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'NVIDIA Disclosure Monitor' -Action $action -Trigger $trigger -Settings $settings -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -RunLevel Limited -Force | Out-Null
""".replace('ARGUMENTS',quote(subprocess.list2cmdline(args))).replace('TIMEVALUE',quote(at))
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',
                    base64.b64encode(script.encode('utf-16le')).decode()],check=True)
    print('Windows タスク NVIDIA Disclosure Monitor：毎日 '+at+'（Windowsの現地時刻）')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--contact',required=True)
    parser.add_argument('--at',default='09:00')
    args=parser.parse_args()
    install(args.contact,args.at)
