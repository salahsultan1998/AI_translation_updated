"""Application-level non-secret settings."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def model_settings():
    data = json.loads((ROOT / 'config' / 'defaults.json').read_text(encoding='utf-8'))
    choices = data.get('model_options')
    default = os.environ.get('OPENROUTER_MODEL') or data.get('default_model')
    if not isinstance(choices, list) or not choices or any(not isinstance(x, str) or not x.strip() for x in choices):
        raise ValueError('模型配置无效')
    if not isinstance(default, str) or not default.strip():
        raise ValueError('默认模型配置无效')
    if default not in choices:
        choices = [default, *choices]
    return choices, default


def translation_settings(data):
    options, default = model_settings()
    model = data.get('model') or default
    if model not in options:
        raise ValueError('模型配置无效')
    size = data.get('batch_size', 10)
    if type(size) is not int or not 1 <= size <= 50:
        raise ValueError('每批行数必须在 1 到 50 之间')
    custom = data.get('custom_instructions', '')
    if not isinstance(custom, str) or len(custom) > 20000:
        raise ValueError('补充翻译要求必须为文本，且不可超过 20000 字符')
    return {'model': model, 'batch_size': size, 'use_smart_budget': bool(data.get('use_smart_budget', True)),
            'use_exact_memory': data.get('use_exact_memory') is True, 'custom_instructions': custom}
