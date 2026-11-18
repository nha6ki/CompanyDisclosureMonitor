#!/usr/bin/env python3
"""Apply this release's selected files, checking hashes and backing up originals."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(target):
    source = Path(__file__).resolve().parent
    target = target.resolve()
    manifest = json.loads((source / 'llm/trial_fix_update.json').read_text(encoding='utf-8'))
    if manifest['release'] != 'nvidia-trial-fix-1':
        raise ValueError('更新版が一致しません。今回のZIPを使用してください。')
    if not (target / 'llm_runner.py').is_file():
        raise ValueError('対象はllm_runner.pyのあるプロジェクトフォルダを指定してください。')
    changes = []
    # Inspect every file before writing anything; never overwrite local edits silently.
    for entry in manifest['files']:
        rel = Path(entry['path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('更新ファイルのパスが不正です。')
        src, dst = source / rel, target / rel
        if not dst.resolve().is_relative_to(target) or dst.is_symlink():
            raise ValueError(f'更新先がプロジェクト外です: {rel}')
        if sha(src) != entry['after_sha256']:
            raise ValueError(f'更新ファイルが不一致です: {rel}')
        current = sha(dst) if dst.is_file() else None
        if current == entry['after_sha256']:
            continue
        if current != entry['before_sha256'] or (dst.exists() and not dst.is_file()):
            raise ValueError(f'ローカル変更または版の違いがあります。上書きしません: {rel}')
        changes.append((src, dst, rel))
    if not changes:
        return {'status': 'already_applied', 'release': manifest['release']}
    backup = target / 'update_backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True, exist_ok=False)
    # Back up all originals before replacing the first file.
    for src, dst, rel in changes:
        if dst.is_file():
            old = backup / rel
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, old)
    (backup / 'update_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    for src, dst, rel in changes:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dst.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copy2(src, temporary)
            temporary.replace(dst)
        finally:
            temporary.unlink(missing_ok=True)
    return {'status': 'updated', 'release': manifest['release'],
            'files_updated': len(changes), 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    args = parser.parse_args()
    try:
        result = apply(args.project)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'update_stopped', 'reason': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
