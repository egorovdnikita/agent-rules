#!/usr/bin/env python3
"""Сборка адаптеров и безопасное локальное подключение навыков."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('claude-code', 'codex', 'claude-chat', 'chatgpt')


def build():
    output = ROOT / '.skill-build'
    output.mkdir(exist_ok=True)
    hashes = {}
    for skill in sorted((ROOT / 'skills').iterdir()):
        if not skill.is_dir():
            continue
        if not re.fullmatch(r'[a-z0-9-]{1,64}', skill.name):
            raise ValueError('Недопустимое имя: ' + skill.name)
        source = (skill / 'SKILL.md').read_text()
        if not source.startswith('---\n') or '\n---\n' not in source[4:]:
            raise ValueError('Нет YAML: ' + skill.name)
        for key in ('name:', 'description:'):
            if key not in source.split('\n---\n', 1)[0]:
                raise ValueError('Нет поля ' + key)
        for path in skill.rglob('*'):
            if path.is_symlink():
                raise ValueError('Ссылки внутри пакета запрещены: ' + str(path))
        for platform in PLATFORMS:
            dest = output / platform / skill.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill, dest)
            adapter = (ROOT / 'adapters' / (platform + '.md')).read_text()
            if '<!-- PLATFORM_ADAPTER -->' not in source:
                raise ValueError('Нет места адаптера: ' + skill.name)
            if platform in ('codex', 'claude-code'):
                for child in list(dest.iterdir()):
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                for child in skill.iterdir():
                    (dest / child.name).symlink_to(child, target_is_directory=child.is_dir())
                (dest / 'platform.md').symlink_to(ROOT / 'adapters' / (platform + '.md'))
            else:
                (dest / 'SKILL.md').write_text(source.replace('<!-- PLATFORM_ADAPTER -->', adapter))
            if platform in ('claude-chat', 'chatgpt'):
                archive = output / platform / (skill.name + '.zip')
                with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                    for path in sorted(dest.rglob('*')):
                        if path.is_file():
                            info = zipfile.ZipInfo(str(path.relative_to(dest.parent)), (2026, 1, 1, 0, 0, 0))
                            info.compress_type = zipfile.ZIP_DEFLATED
                            z.writestr(info, path.read_bytes())
                hashes[str(archive.relative_to(output))] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'manifest.json').write_text(json.dumps(hashes, indent=2) + '\n')
    return output


def install(home):
    # Проверить ВСЕ конфликты до первой записи. Существующие навыки не заменяем.
    plans = []
    for platform, folder in (('codex', '.agents/skills'), ('claude-code', '.claude/skills')):
        for skill in sorted((ROOT / 'skills').iterdir()):
            if not skill.is_dir():
                continue
            target = home / folder / skill.name
            source = ROOT / '.skill-build' / platform / skill.name
            if target.is_symlink() and target.resolve() == source.resolve():
                continue
            if target.exists() or target.is_symlink():
                raise FileExistsError('Не заменяю существующий навык: ' + str(target))
            plans.append((target, source))
    build()
    for target, source in plans:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=True)
    print('Подключено новых ссылок:', len(plans))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'install'])
    parser.add_argument('--home', type=Path, default=Path.home())
    args = parser.parse_args()
    if args.command == 'install':
        install(args.home)
    else:
        print(build())
