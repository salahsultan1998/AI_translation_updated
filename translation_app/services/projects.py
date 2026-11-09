"""Discover and validate independently maintained project and language files."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = ROOT / "config"
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取配置：{path.name}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"配置必须是对象：{path.name}")
    return data


def _safe_file(folder, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError("配置文件路径无效")
    target = (folder / relative).resolve()
    if not target.is_relative_to(folder.resolve()) or not target.is_file():
        raise ValueError(f"配置文件不存在或路径无效：{relative}")
    return target


def _project_language(folder, language):
    if not isinstance(language, str) or not _ID.fullmatch(language):
        raise ValueError("语言标识无效")
    language_folder = (folder / "languages" / language).resolve()
    if not language_folder.is_relative_to(folder.resolve()):
        raise ValueError("语言配置路径无效")
    data = _read(_safe_file(language_folder, "language.json"))
    if data.get("id") != language:
        raise ValueError(f"语言标识无效：{language}")
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in ("name", "database_name")):
        raise ValueError(f"语言名称无效：{language}")
    _safe_file(language_folder, data.get("instructions"))
    if "project_instructions" in data:
        _safe_file(language_folder, data["project_instructions"])
    return data


def list_languages(project_id):
    """Return only the languages configured inside the selected workspace."""
    project = get_project(project_id)
    folder = CONFIG_ROOT / "projects" / project["id"]
    return [_project_language(folder, language) for language in project["languages"]]


def list_projects():
    result = []
    for folder in sorted((CONFIG_ROOT / "projects").iterdir()):
        if not folder.is_dir():
            continue
        data = _read(folder / "project.json")
        if data.get("id") != folder.name or not _ID.fullmatch(folder.name):
            raise ValueError(f"项目标识无效：{folder.name}")
        if not all(isinstance(data.get(k), str) and data[k].strip() for k in ("name", "icon")):
            raise ValueError(f"项目名称、数据库标识或图标无效：{folder.name}")
        if not isinstance(data.get("enabled"), bool):
            raise ValueError(f"项目启用状态无效：{folder.name}")
        direct = data.get("direct", False)
        if type(direct) is not bool or direct != (folder.name == "direct"):
            raise ValueError(f"直接翻译配置无效：{folder.name}")
        if direct:
            if data.get("database_id") is not None:
                raise ValueError("直接翻译不能关联项目数据库")
        elif not isinstance(data.get("database_id"), str) or not data["database_id"].strip():
            raise ValueError(f"项目数据库标识无效：{folder.name}")
        supported = data.get("languages")
        if (not isinstance(supported, list) or not supported
                or any(not isinstance(lang, str) or not _ID.fullmatch(lang) for lang in supported)
                or len(set(supported)) != len(supported)):
            raise ValueError(f"项目语言配置无效：{folder.name}")
        if not direct:
            _safe_file(folder, data.get("project_prompt"))
        db_names = [_project_language(folder, lang)["database_name"].strip().casefold() for lang in supported]
        if len(set(db_names)) != len(db_names):
            raise ValueError(f"项目语言数据库名称重复：{folder.name}")
        data["direct"] = direct
        result.append(data)
    return result


def get_project(project_id):
    for project in list_projects():
        if project["id"] == project_id and project["enabled"]:
            return project
    raise ValueError("项目不存在或未启用")


def validate_selection(project_id, language):
    project = get_project(project_id)
    if language not in project["languages"]:
        raise ValueError("该项目不支持此语言")
    folder = CONFIG_ROOT / "projects" / project["id"]
    return project, _project_language(folder, language)


def compose_prompt(project_id, language):
    project, language_info = validate_selection(project_id, language)
    base = _safe_file(CONFIG_ROOT, "base_prompt.md").read_text(encoding="utf-8")
    base = base.replace("{target_lang}", language_info["database_name"])
    folder = CONFIG_ROOT / "projects" / project_id
    lang_folder = folder / "languages" / language
    parts = [base, _safe_file(lang_folder, language_info["instructions"]).read_text(encoding="utf-8")]
    if not project["direct"]:
        parts.append(_safe_file(folder, project["project_prompt"]).read_text(encoding="utf-8"))
    if "project_instructions" in language_info:
        parts.append(_safe_file(lang_folder, language_info["project_instructions"]).read_text(encoding="utf-8"))
    return "\n\n".join(part.strip() for part in parts if part.strip())
