"""Safe Jenkins parameter adapter: no user-controlled shell interpolation."""
import os
import sys
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiwi_ci import main, write_reports


def run():
    try:
        kind = os.environ.get('CI_TEST_TYPE', 'api')
        if kind not in ('api', 'web'):
            raise ValueError
        suite = int(os.environ['CI_SUITE_ID'])
        if suite < 1:
            raise ValueError
        key = uuid.uuid5(uuid.NAMESPACE_URL, ':'.join([
            os.environ['JOB_NAME'], os.environ['BUILD_NUMBER'], kind, str(suite)]))
        args = ['--type', kind, '--suite', str(suite), '--key', str(key),
                '--output', 'kiwi-report.json', '--timeout', '900']
        if kind == 'web':
            mode = os.environ.get('CI_WEB_MODE', 'debug')
            if mode not in ('debug', 'formal'):
                raise ValueError
            args += ['--mode', mode]
            for field in ('plan', 'build', 'environment'):
                value = os.environ.get('CI_' + field.upper() + '_ID', '')
                if value:
                    number = int(value)
                    if number < 1:
                        raise ValueError
                    args += ['--' + field, str(number)]
        return main(args)
    except (ValueError, KeyError):
        write_reports(dict(status='error', error='Jenkins 执行参数无效。', results=[]), 'kiwi-report.json')
        return 2


if __name__ == '__main__':
    sys.exit(run())
