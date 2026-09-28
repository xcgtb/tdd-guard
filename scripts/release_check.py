#!/usr/bin/env python3
"""发布前静态检查：拒绝把本地密钥、运行数据和构建缓存带进发布目录。"""
from pathlib import Path
import re, sys

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = {'.git', '.pytest_cache', '__pycache__', 'data', '.env'}
BAD_EXT = {'.pyc', '.pyo'}

# Compose 示例只能放在仓库根目录，不能放进 .github/workflows/，否则会被 GitHub 当成 Actions workflow。
BAD_WORKFLOW_FILES = {'.github/workflows/docker-compose.yml', '.github/workflows/docker-compose.yaml', '.github/workflows/docker-compose.example.yml'}
def is_placeholder_secret(name: str, value: str) -> bool:
    value = value.strip().strip('"').strip("'")
    return not value or value in {
        'change-me', 'change-me-please', 'your-secret', 'your-token',
        '<your-secret>', '<your-token>', '请改成你自己的强密码'
    }

errors = []
for bad in BAD_WORKFLOW_FILES:
    if (ROOT / bad).exists():
        errors.append(f'forbidden workflow file: {bad}')
for p in ROOT.rglob('*'):
    rel = p.relative_to(ROOT)
    if any(part in FORBIDDEN for part in rel.parts) or p.suffix in BAD_EXT:
        errors.append(f'forbidden: {rel}')
    if p.is_file() and p.name not in {'release_check.py'}:
        try:
            text = p.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        if p.name.startswith('.env'):
            for line in text.splitlines():
                m = re.match(r'^\s*(WEB_PASSWORD|WEB_TOKEN|TG_BOT_TOKEN)\s*=\s*(.*?)\s*$', line)
                if m and not is_placeholder_secret(m.group(1), m.group(2)):
                    errors.append(f'possible secret: {rel}: {m.group(1)} is non-empty')
if errors:
    print('\n'.join(errors))
    sys.exit(1)
print('release check: OK')
