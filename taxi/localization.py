"""Shared UI catalog used for server-side decision briefs and errors."""
import json
from pathlib import Path

EN = json.loads((Path(__file__).parent / 'static' / 'locales' / 'en.json').read_text())


def translate(text: str, language: str = 'zh') -> str:
    if language != 'en':
        return text
    if text in EN:
        return EN[text]
    if text.startswith('模型服务暂时不可用（HTTP '):
        code = text.split('HTTP ', 1)[1].split('）', 1)[0]
        return f'Model service unavailable (HTTP {code}). Try a fixed brief.'
    return text
