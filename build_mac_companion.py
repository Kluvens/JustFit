"""Build a local macOS companion bundle; never publishes or installs it."""
import argparse
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

project = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--vault', required=True, type=Path)
args = parser.parse_args()
app = args.output
if app.exists():
    raise SystemExit('输出位置已存在；请指定新的输出位置。')
contents = app / 'Contents'
binary = contents / 'MacOS'
resources = contents / 'Resources'
binary.mkdir(parents=True)
resources.mkdir()
plist = {'CFBundleName': '健身记录同步', 'CFBundleDisplayName': '健身记录同步',
         'CFBundleIdentifier': 'local.justinyang.fitness.receiver', 'CFBundleExecutable': 'FitnessMac',
         'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.1', 'CFBundleVersion': '1',
         'NSHighResolutionCapable': True, 'FitnessVaultRoot': str(args.vault.resolve())}
with (contents / 'Info.plist').open('wb') as output:
    plistlib.dump(plist, output)
for name in ('fitness_service.py', 'fitness_store.py', 'fitness_knowledge.py', 'fitness_index_note.py'):
    shutil.copy2(project / name, resources / name)
subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-module-cache-path', '/private/tmp/fitness-swift-cache',
                str(project / 'FitnessMac.swift'), '-o', str(binary / 'FitnessMac')], check=True)
print(f'已生成本地测试构建：{app}')
