"""Build the office and employee applications without modifying existing deployments."""
from pathlib import Path
import os
import shutil
import subprocess
import sys


def main():
    source = Path(__file__).resolve().parent
    destination = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else source / 'release'
    destination.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
    # Foreign ICU DLLs on PATH break Qt's Windows ICU imports in frozen builds.
    environment['PATH'] = os.pathsep.join([str(Path(sys.executable).parent), str(windows / 'System32'), str(windows)])
    for module, name in [('server_office', '간부방호출기'), ('client_app', '직원용번호표'), ('portal', '직원통합웹')]:
        log = destination / (name + '-build.log')
        command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--console' if module == 'portal' else '--windowed',
                   *(['--add-data', str(source / 'employee.html') + os.pathsep + '.'] if module == 'server_office' else ['--add-data', str(source / 'portal.html') + os.pathsep + '.'] if module == 'portal' else []),
                   '--name', name, '--distpath', str(destination), '--workpath', str(destination / '_build' / module),
                   '--specpath', str(destination / '_spec'), str(source / (module + '.py'))]
        with log.open('w', encoding='utf-8') as output:
            subprocess.run(command, cwd=source, env=environment, stdout=output, stderr=subprocess.STDOUT, check=True)
        print(f'Built: {name}', flush=True)
    shutil.copy2(source / 'organization.json', destination / 'organization.json')


if __name__ == '__main__':
    main()
