"""Background translation engine, independent of Flask and browser state."""
import json
import os
from pathlib import Path
import re
import time

import requests

from .projects import compose_prompt, validate_selection
from .spreadsheets import read_mapped_rows, write_translations

_TAGS = re.compile(r"(\{[^{}]+\}|%[sdif]|<[^>]+>)")


def _resources(project, language, settings):
    if project["direct"]:
        return {}, {}, ""
    token = settings.get("access_token")
    from translation_app.auth_config import auth_enabled
    anonymous = not auth_enabled()
    if not token and not anonymous:
        raise ValueError("登录已过期，请重新登录")
    # Production uses the user's token; local development uses the configured key.
    from .database import ProjectDatabase
    engine = ProjectDatabase(token, allow_anonymous=anonymous)
    game = project["database_id"].lower().strip()
    lang = language["database_name"].lower().strip()

    def fetch_all(table, columns, order):
        # Supabase can cap response rows; fetch every page rather than silently
        # dropping the remainder of large project glossaries or memories.
        rows, offset, page_size = [], 0, 1000
        while True:
            response = (engine.client.table(table).select(columns).eq("game_id", game)
                        .eq("target_language", lang).order(order).range(offset, offset + page_size - 1).execute())
            page = response.data or []
            if not page:
                return rows
            rows.extend(page)
            offset += len(page)

    glossary = fetch_all("glossaries", "source_term,target_term,note", "source_term")
    memory = fetch_all("translation_memory", "source_text,target_text", "source_text")
    return ({row["source_term"]: {"target": row["target_term"], "note": row.get("note") or ""}
             for row in glossary},
            {row["source_text"]: row["target_text"] for row in memory},
            engine.get_game_profile(game))


class _ContextIndex:
    def __init__(self, terms, memory, profile):
        self.terms, self.memory, self.profile = terms, memory, profile
        self.sources = list(memory)
        self.automaton = None
        self.rapid = None
        try:
            import ahocorasick
            if terms:
                self.automaton = ahocorasick.Automaton()
                for term in terms:
                    if term:
                        self.automaton.add_word(term, term)
                if len(self.automaton):
                    self.automaton.make_automaton()
                else:
                    self.automaton = None
        except ImportError:
            pass
        try:
            from rapidfuzz import process, fuzz
            self.rapid = (process, fuzz)
        except ImportError:
            pass

    def build(self, rows):
        pieces, found_terms, found_memory = [], set(), set()
        if self.profile:
            pieces.append("### GAME TONE & STYLE INSTRUCTIONS:\n" + self.profile)
        for _, source, _ in rows:
            matches = (term for _, term in self.automaton.iter(source)) if self.automaton else (
                term for term in self.terms if term and term in source)
            for term in matches:
                item = self.terms[term]
                target = item.get("target", "") if isinstance(item, dict) else item
                note = item.get("note", "") if isinstance(item, dict) else ""
                found_terms.add(f"{term} : {target}" + (f" (Rule: {note})" if note else ""))
            if self.rapid:
                process, fuzz = self.rapid
                memory_matches = [value for value, _, _ in process.extract(source, self.sources, scorer=fuzz.WRatio,
                                                                              limit=2, score_cutoff=85)]
            else:
                from difflib import get_close_matches
                memory_matches = get_close_matches(source, self.sources, n=2, cutoff=.85)
            for match in memory_matches:
                found_memory.add(f"{match} -> {self.memory[match]}")
        if found_terms:
            pieces.append("### GLOSSARY TERMS:\n" + "\n".join("- " + s for s in sorted(found_terms)))
        if found_memory:
            pieces.append("### TRANSLATION MEMORY:\n" + "\n".join("- " + s for s in sorted(found_memory)))
        return "\n\n".join(pieces)


def _call_model(rows, system_prompt, target_language, context, settings):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("未配置翻译服务密钥")
    endpoint = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
    model = settings.get("model") or os.getenv("OPENROUTER_MODEL", "qwen/qwen3.6-flash")
    fallback = os.getenv("OPENROUTER_FALLBACK_MODEL", model)
    payload_rows = [{"row_id": row_id, "source_text": source, "context": ctx} for row_id, source, ctx in rows]
    instruction = settings.get("custom_instructions", "").strip()
    user = (f"### TARGET LANGUAGE: {target_language}\n\n### REFERENCE CONTEXT:\n{context}\n\n"
            f"### CUSTOM INSTRUCTIONS:\n{instruction}\n\n### SOURCE TEXT:\n"
            + json.dumps(payload_rows, ensure_ascii=False))
    system = system_prompt + ('\n\nRespond only with JSON: {"translations": '
                              '[{"row_id": 0, "translated_text": "..."}]}')
    data = {"model": model, "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}], "temperature": 0.2, "response_format": {"type": "json_object"}}
    response = None
    transient_statuses = {429, 500, 502, 503, 504}
    for attempt in range(3):
        try:
            response = requests.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=data, timeout=(10, 90))
        except requests.ConnectTimeout:
            if attempt == 2:
                raise RuntimeError("无法连接翻译服务，请稍后重试") from None
        except requests.ReadTimeout:
            # The provider may still complete a request after a read timeout;
            # replaying it could create a second paid translation.
            raise RuntimeError("翻译服务响应超时，请检查网络后重新提交") from None
        except requests.RequestException:
            raise RuntimeError("翻译服务连接中断，请检查网络后重新提交") from None
        else:
            status = response.status_code
            if status < 400:
                break
            if status not in transient_statuses:
                raise RuntimeError(f"翻译服务拒绝请求（HTTP {status}），请检查模型和服务配置")
            if attempt == 2:
                raise RuntimeError(f"翻译服务暂不可用（HTTP {status}），请稍后重试")
        if attempt == 1 and fallback != model:
            data["model"] = fallback
        time.sleep(2 ** attempt)
    try:
        content = response.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        return json.loads(content)["translations"]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ValueError("翻译服务返回的格式无效") from None


def _check_response(rows, response):
    expected = {row[0]: row[1] for row in rows}
    if not isinstance(response, list):
        raise ValueError("翻译结果格式无效")
    output = {}
    for item in response:
        if not isinstance(item, dict) or type(item.get("row_id")) is not int:
            raise ValueError("翻译结果行号无效")
        row_id, text = item["row_id"], item.get("translated_text")
        if row_id not in expected or row_id in output or not isinstance(text, str) or not text.strip():
            raise ValueError("翻译结果缺失、重复或为空")
        if sorted(_TAGS.findall(expected[row_id])) != sorted(_TAGS.findall(text)):
            raise ValueError(f"第 {row_id + 1} 行的变量或标签不匹配")
        output[row_id] = text
    if output.keys() != expected.keys():
        raise ValueError("翻译结果缺少行")
    return output


def _prepare_batches(rows, batch_size, smart):
    unique, seen, aliases = [], {}, {}
    for row_id, source, context in rows:
        key = (source, context)
        if key in seen:
            aliases[row_id] = seen[key]
        else:
            seen[key] = row_id
            unique.append((row_id, source, context))
    batches, batch, chars = [], [], 0
    for row in unique:
        size = len(row[1]) + len(row[2])
        if batch and (len(batch) >= batch_size or (smart and chars + size > 1000)):
            batches.append(batch)
            batch, chars = [], 0
        batch.append(row)
        chars += size
    if batch:
        batches.append(batch)
    return batches, aliases


def translate_file(input_path, output_path, project_id, language, mappings, settings, progress_callback=None,
                   *, provider=None, resource_loader=None):
    """Translate mapped sheets; a failure raises and never produces an output file."""
    project, lang = validate_selection(project_id, language)
    settings = dict(settings or {})
    batch_size = settings.get("batch_size", 10)
    if type(batch_size) is not int or not 1 <= batch_size <= 50:
        raise ValueError("每批行数必须在 1 到 50 之间")
    mapped = read_mapped_rows(input_path, mappings)
    prepared = {name: _prepare_batches(rows, batch_size, bool(settings.get("use_smart_budget", True)))
                for name, rows in mapped.items()}
    total = sum(len(batches) for batches, _ in prepared.values())
    if not total:
        raise ValueError("没有可翻译的源文本")
    prompt = settings.get('system_prompt') if 'system_prompt' in settings else compose_prompt(project_id, language)
    if progress_callback:
        progress_callback(0, total)
    terms, memory, profile = (resource_loader or _resources)(project, lang, settings)
    context_index = _ContextIndex(terms, memory, profile)
    completed, results = 0, {}
    for name, (batches, aliases) in prepared.items():
        translated = {}
        for batch in batches:
            context = context_index.build(batch)
            raw = (provider or _call_model)(batch, prompt, lang["database_name"], context, settings)
            translated.update(_check_response(batch, raw))
            completed += 1
            if progress_callback:
                progress_callback(completed, total)
        for row_id, representative in aliases.items():
            translated[row_id] = translated[representative]
        results[name] = translated
    target_path = Path(output_path)
    temp = target_path.with_name(target_path.stem + ".tmp" + target_path.suffix)
    try:
        write_translations(input_path, temp, mappings, results)
        temp.replace(output_path)
    finally:
        temp.unlink(missing_ok=True)
    return {"rows": sum(len(rows) for rows in mapped.values()), "batches": total, "sheets": len(mapped)}
