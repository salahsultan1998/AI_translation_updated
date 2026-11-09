"""Background translation engine, independent of Flask and browser state."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import logging
import math
import os
from pathlib import Path
import re
import time
import threading

import requests

from . import provider_runtime
from .resources import load_resources as _resources
from .translation_state import Checkpoint, check_cancelled, fingerprint, output_lock
from .projects import load_prompt_pair, validate_selection
from .spreadsheets import read_mapped_rows, write_translations

_TAGS = re.compile(r"(\{[^{}]+\}|%[sdif]|<[^>]+>)")


class _ContextIndex:
    def __init__(self, terms, memory):
        self.terms, self.memory = terms, memory
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
        self._references = {}

    def references(self, source):
        if source in self._references:
            return self._references[source]
        matches = (term for _, term in self.automaton.iter(source)) if self.automaton else (
            term for term in self.terms if term and term in source)
        terms = set()
        for term in matches:
            item = self.terms[term]
            target = item.get("target", "") if isinstance(item, dict) else item
            note = item.get("note", "") if isinstance(item, dict) else ""
            terms.add(f"{term} : {target}" + (f" (Rule: {note})" if note else ""))
        if source in self.memory:
            memory_matches = [source]
        elif self.rapid:
            process, fuzz = self.rapid
            memory_matches = [value for value, _, _ in process.extract(
                source, self.sources, scorer=fuzz.WRatio, limit=2, score_cutoff=85)]
        else:
            from difflib import get_close_matches
            memory_matches = get_close_matches(source, self.sources, n=2, cutoff=.85)
        result = terms, {f"{match} -> {self.memory[match]}" for match in memory_matches}
        if len(self._references) >= 256:
            self._references.pop(next(iter(self._references)))
        self._references[source] = result
        return result

    def build(self, rows):
        pieces, found_terms, found_memory = [], set(), set()
        for _, source, _ in rows:
            terms, memory = self.references(source)
            found_terms.update(terms)
            found_memory.update(memory)
        if found_terms:
            pieces.append("### GLOSSARY TERMS:\n" + "\n".join("- " + s for s in sorted(found_terms)))
        if found_memory:
            pieces.append("### TRANSLATION MEMORY:\n" + "\n".join("- " + s for s in sorted(found_memory)))
        return "\n\n".join(pieces)


def _timing(settings, step, **values):
    logging.getLogger(__name__).info('[timing] job=%s step=%s %s',
        settings.get('_job_id', 'standalone'), step,
        ' '.join(f'{key}={value}' for key, value in values.items()))


def _call_model(rows, system_prompt, target_language, context, settings):
    prepared_at = time.perf_counter()
    data = _request_data(rows, system_prompt, target_language, context, settings)
    _timing(settings, "prepare_request", seconds=f"{time.perf_counter() - prepared_at:.4f}", rows=len(rows))
    logger = logging.getLogger(__name__)
    job_id = settings.get('_job_id', 'standalone')
    messages = data['messages']
    logger.info('[prompt] job=%s rows=%s system_chars=%s user_chars=%s total_chars=%s estimated_input_tokens=%s',
                job_id, len(rows), len(messages[0]['content']), len(messages[1]['content']),
                sum(len(item['content']) for item in messages),
                sum(_estimate_tokens(item['content']) + 8 for item in messages))
    if os.getenv('TRANSLATION_LOG_PROMPTS', 'true').lower() in {'1', 'true', 'yes', 'on'}:
        for item in messages:
            logger.info('[prompt] job=%s role=%s BEGIN\n%s\n[prompt] job=%s role=%s END',
                        job_id, item['role'], item['content'], job_id, item['role'])
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("未配置翻译服务密钥")
    endpoint = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
    model = data['model']
    fallback = os.getenv("OPENROUTER_FALLBACK_MODEL", model)
    response = None
    transient_statuses = {429, 500, 502, 503, 504}
    for attempt in range(3):
        check_cancelled(settings)
        waiting = time.perf_counter()
        sent = None
        status, usage = None, {}
        try:
            with provider_runtime.request_slot(settings):
                check_cancelled(settings)
                sent = time.perf_counter()
                _timing(settings, 'request_start', attempt=attempt + 1, model=data['model'],
                        rows=len(rows), reasoning_enabled=data['reasoning']['enabled'],
                        slot_wait_seconds=f'{sent - waiting:.4f}')
                response = provider_runtime.post(endpoint, headers={"Authorization": f"Bearer {key}"},
                                                 json=data, timeout=(10, 90))
            status = response.status_code
            if status < 400:
                try:
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    raise ValueError('翻译服务返回的格式无效') from None
                raw_usage = body.get('usage')
                if isinstance(raw_usage, dict):
                    usage = {key: value for key, value in raw_usage.items()
                             if key in {'prompt_tokens', 'completion_tokens'}
                             and type(value) is int and value >= 0}
                break
            if status not in transient_statuses:
                raise RuntimeError(f"翻译服务拒绝请求（HTTP {status}），请检查模型和服务配置")
            if attempt == 2:
                raise RuntimeError(f"翻译服务暂不可用（HTTP {status}），请稍后重试")
        except requests.ConnectTimeout:
            if attempt == 2:
                raise RuntimeError("无法连接翻译服务，请稍后重试") from None
        except requests.ReadTimeout:
            # An uncertain, potentially charged request is never replayed automatically.
            raise RuntimeError("翻译服务响应超时，请检查网络后重新提交") from None
        except requests.RequestException:
            raise RuntimeError("翻译服务连接中断，请检查网络后重新提交") from None
        finally:
            if sent is not None:
                _timing(settings, 'request_end', attempt=attempt + 1, status=status,
                        seconds=f'{time.perf_counter() - sent:.4f}', **usage)
            record = settings.get('_record_request')
            if record and sent is not None:
                record({'seconds': time.perf_counter() - sent, 'wait_seconds': sent - waiting,
                        'status': status, 'usage': usage})
        if attempt == 1 and fallback != model:
            data["model"] = fallback
        delay = 2 ** attempt
        if response is not None and status == 429:
            try:
                delay = max(delay, min(30, float(response.headers.get('Retry-After', '0'))))
            except (ValueError, TypeError):
                pass
        _timing(settings, 'retry_wait', seconds=delay)
        event = settings.get('_cancel_event')
        if event is None:
            time.sleep(delay)
        elif event.wait(delay):
            check_cancelled(settings)
    try:
        content = body["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        return json.loads(content)["translations"]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ValueError("翻译服务返回的格式无效") from None


def _request_data(rows, system_prompt, target_language, context, settings):
    model = settings.get("model") or os.getenv("OPENROUTER_MODEL", "qwen/qwen3.6-flash")
    payload_rows = [{"row_id": row_id, "source_text": source, "context": ctx} for row_id, source, ctx in rows]
    instruction = settings.get("instructions", "")
    temporary = settings.get('custom_instructions', '').strip()
    user = (f"### TARGET LANGUAGE: {target_language}\n\n### REFERENCE CONTEXT:\n{context}\n\n"
            f"### INSTRUCTIONS:\n{instruction}\n\n")
    if temporary:
        user += f'### TEMPORARY INSTRUCTIONS:\n{temporary}\n\n'
    user += '### SOURCE TEXT:\n' + json.dumps(payload_rows, ensure_ascii=False)
    system = system_prompt
    return {"model": model, "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}], "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "reasoning": {"enabled": False},
            "max_tokens": settings.get('_output_budget', 4000)}


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


def _estimate_tokens(text):
    # A language-aware heuristic, not a model tokenizer. Leave ample room for
    # target expansion; provider usage is recorded separately when available.
    return math.ceil(sum(.25 if ord(char) < 128 else 1 for char in text))


def _output_estimate(rows):
    return sum(2 * _estimate_tokens(source) + 48 for _, source, _ in rows)


def _prepare_batches(rows, batch_size, smart):
    unique, seen, aliases = [], {}, {}
    for row_id, source, context in rows:
        key = (source, context)
        if key in seen:
            aliases[row_id] = seen[key]
        else:
            seen[key] = row_id
            unique.append((row_id, source, context))
    return list(_iter_batches(unique, batch_size, smart)), aliases


def _iter_batches(rows, batch_size, smart):
    budget = provider_runtime.int_setting('TRANSLATION_OUTPUT_TOKEN_BUDGET', 4000, 256, 32000)
    batch, output_tokens = [], 0
    for row in rows:
        row_tokens = _output_estimate([row])
        if batch and (len(batch) >= batch_size or (smart and output_tokens + row_tokens > budget)):
            yield batch
            batch = []
            output_tokens = 0
        batch.append(row)
        output_tokens += row_tokens
    if batch:
        yield batch


def _budget_batches(batch, index, prompt, language, settings, input_budget):
    context = index.build(batch)
    data = _request_data(batch, prompt, language, context, settings)
    input_tokens = sum(_estimate_tokens(item['content']) + 8 for item in data['messages'])
    if input_tokens <= input_budget and _output_estimate(batch) <= settings['_output_budget']:
        yield batch, context
    elif len(batch) > 1:
        middle = len(batch) // 2
        yield from _budget_batches(batch[:middle], index, prompt, language, settings, input_budget)
        yield from _budget_batches(batch[middle:], index, prompt, language, settings, input_budget)
    else:
        raise ValueError('翻译输入超过 Token 预算，请缩短原文、上下文或补充要求')


def translate_file(input_path, output_path, project_id, language, mappings, settings, progress_callback=None,
                   *, provider=None, resource_loader=None):
    """Bounded parallel translation with validated, job-local recovery checkpoints."""
    settings = dict(settings or {})
    settings.setdefault('_cancel_event', threading.Event())
    performance = {'timings': {}, 'records': [], 'rows': 0, 'stage': 'waiting',
                   'stage_started': time.perf_counter()}
    settings['_performance'] = performance
    started = time.perf_counter()
    succeeded = False
    try:
        with output_lock(output_path, settings):
            result = _translate_file(input_path, output_path, project_id, language, mappings, settings,
                                     progress_callback, provider, resource_loader)
            succeeded = True
            return result
    finally:
        _timing(settings, performance['stage'] + '_end',
                seconds=f"{time.perf_counter() - performance['stage_started']:.4f}")
        _timing(settings, 'total', seconds=f'{time.perf_counter() - started:.4f}', succeeded=succeeded)
        logging.getLogger(__name__).info('Translation attempt: %s', json.dumps({
            'job_id': settings.get('_job_id'), 'succeeded': succeeded, 'stage': performance['stage'],
            'rows': performance['rows'], 'duration_seconds': round(time.perf_counter() - started, 4),
            'timings': {key: round(value, 4) for key, value in performance['timings'].items()},
            'requests': _request_summary(performance['records'])}))


def _translate_file(input_path, output_path, project_id, language, mappings, settings, progress, provider, loader):
    started = time.perf_counter()
    performance = settings['_performance']
    timings, requests_metrics = performance['timings'], performance['records']
    metrics_lock = threading.Lock()

    def record_request(item):
        with metrics_lock:
            requests_metrics.append(item)

    settings['_record_request'] = record_request
    stage_callback = settings.get('_stage_callback')

    def stage(name):
        check_cancelled(settings)
        if name != performance['stage']:
            now = time.perf_counter()
            _timing(settings, performance['stage'] + '_end',
                    seconds=f"{now - performance['stage_started']:.4f}")
            performance['stage'], performance['stage_started'] = name, now
            _timing(settings, name + '_start')
        if stage_callback:
            stage_callback(name)

    project, lang = validate_selection(project_id, language)
    batch_size = settings.get('batch_size', 10)
    if type(batch_size) is not int or not 1 <= batch_size <= 50:
        raise ValueError('每批行数必须在 1 到 50 之间')
    concurrency = provider_runtime.int_setting('TRANSLATION_CONCURRENCY', 50, 1, 128)
    input_budget = provider_runtime.int_setting('TRANSLATION_INPUT_TOKEN_BUDGET', 6000, 256, 64000)
    settings['_output_budget'] = provider_runtime.int_setting('TRANSLATION_OUTPUT_TOKEN_BUDGET', 4000, 256, 32000)
    stage('reading')
    mark = time.perf_counter()
    mapped = read_mapped_rows(input_path, mappings)
    # Global IDs avoid sheet-local row-number collisions and allow cross-sheet
    # deduplication. Context remains part of the key, as in the original engine.
    unique, positions, seen, offset = [], {}, {}, 0
    for name, rows in mapped.items():
        for row_id, source, context in rows:
            key = (source, context)
            if key not in seen:
                seen[key] = offset + row_id
                unique.append((seen[key], source, context))
            positions[(name, row_id)] = seen[key]
        offset += max((row[0] for row in rows), default=-1) + 1
    if not unique:
        raise ValueError('没有可翻译的源文本')
    performance['rows'] = len(positions)
    timings['read_seconds'] = time.perf_counter() - mark
    pair = load_prompt_pair(project_id, language)
    prompt = settings.get('system_prompt', pair['system'])
    settings.setdefault('instructions', pair['instructions'])
    # Provisional progress is visible even while resources are being loaded.
    if progress:
        progress(0, len(unique))
    stage('resources')
    mark = time.perf_counter()
    terms, memory, _ = (loader or _resources)(project, lang, settings)
    timings['resources_seconds'] = time.perf_counter() - mark
    stage('planning')
    mark = time.perf_counter()
    version = fingerprint({'version': 1, 'rows': unique, 'positions': list(positions.items()),
                           'project': project_id, 'database': project.get('database_id'),
                           'language': language, 'target': lang['database_name'], 'mappings': mappings,
                           'prompt': prompt, 'model': settings.get('model') or os.getenv('OPENROUTER_MODEL', 'qwen/qwen3.6-flash'),
                           'endpoint': os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
                           'fallback': os.getenv('OPENROUTER_FALLBACK_MODEL', ''),
                           'reasoning': {'enabled': False},
                           'instructions': settings['instructions'],
                           'custom_instructions': settings.get('custom_instructions', ''),
                           'exact_memory': bool(settings.get('use_exact_memory')),
                           'terms': terms, 'memory': memory})
    checkpoint = Checkpoint(output_path, version)
    try:
        translated = checkpoint.read()
        # Checkpoints are private, but still validate them before trusting data.
        valid = _check_response([row for row in unique if row[0] in translated], [
            {'row_id': row_id, 'translated_text': text} for row_id, text in translated.items()])
        translated = valid
        restored = len(translated)
        exact = {}
        if settings.get('use_exact_memory') and not settings.get('custom_instructions', '').strip():
            for row in unique:
                row_id, source, context = row
                if row_id not in translated and not context and memory.get(source):
                    try:
                        exact.update(_check_response([row], [{'row_id': row_id, 'translated_text': memory[source]}]))
                    except ValueError:
                        pass  # Unsafe memory is a reference only, never a fast-path result.
        if exact:
            checkpoint.save(exact)
            translated.update(exact)
        pending_rows = [row for row in unique if row[0] not in translated]
        index = _ContextIndex(terms, memory)

        def plans():
            for batch in _iter_batches(pending_rows, batch_size, bool(settings.get('use_smart_budget', True))):
                check_cancelled(settings)
                prepared_at = time.perf_counter()
                for plan in _budget_batches(batch, index, prompt, lang['database_name'], settings, input_budget):
                    _timing(settings, 'context_and_budget', rows=len(plan[0]),
                            seconds=f'{time.perf_counter() - prepared_at:.4f}')
                    yield plan
                    prepared_at = time.perf_counter()

        timings['planning_seconds'] = time.perf_counter() - mark
        completed = len(translated)
        if progress:
            progress(completed, len(unique))
        stage('translating')
        mark = time.perf_counter()
        error = None
        iterator = iter(plans())
        batch_count = 0

        def run(plan):
            check_cancelled(settings)
            batch, context = plan
            raw = (provider or _call_model)(batch, prompt, lang['database_name'], context, settings)
            checked_at = time.perf_counter()
            result = _check_response(batch, raw)
            _timing(settings, 'validate_response', rows=len(batch),
                    seconds=f'{time.perf_counter() - checked_at:.4f}')
            return result

        # Only keep concurrency requests in flight, so failure never leaves an
        # unbounded queue of potentially billable calls. Drain already sent calls
        # and save valid responses even when a neighbouring batch fails.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            active = {}

            def submit():
                nonlocal error, batch_count
                try:
                    plan = next(iterator, None)
                except Exception as failure:
                    error = failure
                    settings['_cancel_event'].set()
                    return False
                if plan is not None:
                    active[pool.submit(run, plan)] = plan
                    batch_count += 1
                    return True
                return False

            for _ in range(concurrency):
                if not submit():
                    break
            last_check = time.monotonic()
            while active:
                done, _ = wait(active, timeout=.25, return_when=FIRST_COMPLETED)
                if error is None and time.monotonic() - last_check > 1:
                    try:
                        stage('translating')
                    except Exception as failure:
                        error = failure
                        settings['_cancel_event'].set()
                    last_check = time.monotonic()
                for future in done:
                    active.pop(future)
                    try:
                        output = future.result()
                        saved_at = time.perf_counter()
                        checkpoint.save(output)
                        _timing(settings, 'checkpoint_save', rows=len(output),
                                seconds=f'{time.perf_counter() - saved_at:.4f}')
                        translated.update(output)
                        completed += len(output)
                        if error is None and progress:
                            progress(completed, len(unique))
                    except Exception as failure:
                        if error is None:
                            error = failure
                            settings['_cancel_event'].set()
                if error is None:
                    for _ in done:
                        if not submit():
                            break
        timings['translation_seconds'] = time.perf_counter() - mark
        if error:
            raise error
        stage('writing')
        mark = time.perf_counter()
        results = {name: {} for name in mapped}
        for (name, row_id), representative in positions.items():
            results[name][row_id] = translated[representative]
        target_path = Path(output_path)
        temp = target_path.with_name(target_path.stem + '.tmp' + target_path.suffix)
        try:
            write_translations(input_path, temp, mappings, results)
            check_cancelled(settings)
            temp.replace(output_path)
        finally:
            temp.unlink(missing_ok=True)
        timings['write_seconds'] = time.perf_counter() - mark
        summary = {'rows': len(positions), 'batches': batch_count, 'sheets': len(mapped),
                   'unique_rows': len(unique), 'deduplicated_rows': len(positions) - len(unique),
                   'restored_rows': restored, 'memory_rows': len(exact), 'concurrency': concurrency,
                   'timings': {key: round(value, 4) for key, value in timings.items()},
                   'duration_seconds': round(time.perf_counter() - started, 4),
                   'requests': _request_summary(requests_metrics)}
        return summary
    finally:
        checkpoint.close()


def _request_summary(records):
    durations = sorted(item['seconds'] for item in records)
    return {'attempts': len(records),
            'rate_limits': sum(item['status'] == 429 for item in records),
            'http_errors': sum(item['status'] is not None and item['status'] >= 400 for item in records),
            'request_seconds': round(sum(durations), 4),
            'slot_wait_seconds': round(sum(item['wait_seconds'] for item in records), 4),
            'p95_seconds': round(durations[math.ceil(.95 * len(durations)) - 1], 4) if durations else 0,
            'prompt_tokens': sum(item['usage'].get('prompt_tokens', 0) or 0 for item in records),
            'completion_tokens': sum(item['usage'].get('completion_tokens', 0) or 0 for item in records)}
