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
