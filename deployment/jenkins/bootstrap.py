"""Prepare private Jenkins initialization data OUTSIDE the source repository."""
import argparse
import json
import os
import secrets
import shutil
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True)
    parser.add_argument('--ca',required=True,help='Validated public Kiwi certificate/CA; never a private key')
    args=parser.parse_args()
    directory=Path(args.directory).resolve()
    repository=Path(__file__).resolve().parents[2]
    if directory==repository or repository in directory.parents:
        raise SystemExit('Private initialization files must be outside the repository')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (directory/'bootstrap.json').exists():
        raise SystemExit('Initialization data already exists; will not overwrite or rotate credentials')
    credentials=[];suites={}
    for kind in ('api','web'):
        token=os.environ.get('KIWI_'+kind.upper()+'_CI_TOKEN','')
        suite=os.environ.get('KIWI_'+kind.upper()+'_SUITE_ID','')
        if token:
            if not suite.isdecimal() or int(suite)<1:
                raise SystemExit('Provide the corresponding positive suite ID')
            credentials.append(dict(id='kiwi-'+kind+'-suite',description='Kiwi '+kind+' suite',token=token))
            suites[kind+'_pass']=int(suite)
    username=os.environ.get('KIWI_JENKINS_ADMIN','kiwi-admin')
    password=secrets.token_urlsafe(24)
    data=dict(username=username,password=password,credentials=credentials,suites=suites)
    previous=os.umask(0o077)
    try:
        with (directory/'bootstrap.json').open('x',encoding='utf-8') as export:json.dump(data,export)
        with (directory/'admin-login.txt').open('x',encoding='utf-8') as export:
            export.write('Jenkins: http://localhost:9081\n账号: '+username+'\n密码: '+password+'\n')
        shutil.copyfile(args.ca,directory/'kiwi-ca.pem')
        (directory/'kiwi-ca.pem').chmod(0o644)
    finally:
        os.umask(previous)
    print('Private initialization data created. Login details are in admin-login.txt; never commit this directory.')


if __name__=='__main__':main()
