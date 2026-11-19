#!/usr/bin/env python3
"""Bounded three-document LLM trial. Default: prepare only; --execute sends requests."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

import structured_events as events

ROOT = Path(__file__).resolve().parent
MAX_RESPONSE_BYTES = 2_000_000


class TrialError(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def check_config(config):
    required = {'base_url', 'model', 'api_key_env', 'response_format', 'max_output_tokens',
                'token_parameter', 'instruction_role', 'timeout_seconds'}
    if set(config) - {'generation'} != required:
        raise TrialError('config_fields_invalid')
    url = urlsplit(config['base_url'])
    loopback = url.hostname in ('localhost', '127.0.0.1', '::1')
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.scheme not in ('https', 'http') or (url.scheme == 'http' and not loopback)):
        raise TrialError('base_url_invalid')
    if config['response_format'] not in ('json_schema', 'json_object'):
        raise TrialError('response_format_invalid')
    if config['token_parameter'] not in ('max_completion_tokens', 'max_tokens'):
        raise TrialError('token_parameter_invalid')
    if config['instruction_role'] not in ('system', 'developer'):
        raise TrialError('instruction_role_invalid')
    for key, low, high in [('max_output_tokens', 512, 12000), ('timeout_seconds', 1, 900)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise TrialError(key + '_invalid')
    name = config['api_key_env']
    if name is not None and (not isinstance(name, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', name)):
        raise TrialError('api_key_env_invalid')
    if name == 'OPENAI_API_KEY' and url.hostname != 'api.openai.com':
        raise TrialError('openai_key_destination_mismatch')
    if name is None and not loopback:
        raise TrialError('external_api_key_env_required')
    model = config['model']
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200):
        raise TrialError('model_invalid')
    generation = config.get('generation', {})
    if not isinstance(generation, dict) or set(generation) - {'temperature', 'seed', 'reasoning_effort'}:
        raise TrialError('generation_fields_invalid')
    if 'temperature' in generation and (type(generation['temperature']) not in (int, float)
                                      or not 0 <= generation['temperature'] <= 2):
        raise TrialError('temperature_invalid')
    if 'seed' in generation and type(generation['seed']) is not int:
        raise TrialError('seed_invalid')
    if 'reasoning_effort' in generation and generation['reasoning_effort'] != 'none':
        raise TrialError('reasoning_effort_invalid')


def provider_schema(schema):
    """Generation schema with string patterns; full checks still run in Python."""
    out = {k: schema[k] for k in ('type', 'required', 'additionalProperties', 'enum', 'pattern') if k in schema}
    if 'const' in schema:
        out['enum'] = [schema['const']]
    if 'enum' in out and 'type' not in out:
        types = sorted({'null' if v is None else 'string' for v in out['enum']})
        out['type'] = types[0] if len(types) == 1 else types
    if 'properties' in schema:
        out['properties'] = {k: provider_schema(v) for k, v in schema['properties'].items()}
    if 'items' in schema:
        out['items'] = provider_schema(schema['items'])
    if 'anyOf' in schema:
        out['anyOf'] = [provider_schema(s) for s in schema['anyOf']]
    return out


def document_input(acc, context):
    s = context['section']
    issuer = {'issuer': context['issuer']} if 'issuer' in context else {}
    return {**issuer, 'accession_number': acc, 'source_sha256': context['source_sha256'],
            'as_of_date': context['filing_date'], 'section_id': s['section_id'],
            'section_sha256': s['text_sha256'], 'item': s['item'],
            'blocks': [{'block_number': s['block_start'] + i, 'text': b} for i, b in enumerate(s['blocks'])]}


def make_request(config, doc):
    prompt = (events.DATA / 'extraction_prompt.txt').read_text(encoding='utf-8')
    if 'issuer' in doc:
        prompt = prompt.replace('NVIDIAの指定された提出書類', '指定された提出企業の開示書類')
    prompt += '\n今回のdocumentsは1資料だけです。この1資料だけをfilings配列に含めてください。'
    # Generation constraints alone do not communicate the full contract to the model.
    prompt += '\n出力JSON Schema:\n' + json.dumps(events.SCHEMA, ensure_ascii=False, separators=(',', ':'))
    if 'issuer' in doc:
        prompt = prompt.replace('"title":"NVIDIA event output v0.1"', '"title":"SEC filing event output"')
    body = {'model': config['model'] or 'MODEL_NOT_CONFIGURED', 'stream': False,
            'messages': [{'role': config['instruction_role'], 'content': prompt},
                         {'role': 'user', 'content': json.dumps({'documents': [doc]}, ensure_ascii=False)}],
            config['token_parameter']: config['max_output_tokens']}
    body.update(config.get('generation', {}))
    schema = provider_schema(events.SCHEMA)
    if config['response_format'] == 'json_schema':
        body['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'nvidia_events', 'strict': True, 'schema': schema}}
    else:
        body['response_format'] = {'type': 'json_object'}
    if urlsplit(config['base_url']).hostname == 'api.openai.com':
        body['store'] = False
    return body


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(config, body):
    headers = {'Content-Type': 'application/json'}
    if config['api_key_env']:
        key = os.environ.get(config['api_key_env'], '')
        if not key:
            raise TrialError('api_key_missing')
        headers['Authorization'] = 'Bearer ' + key
    request = Request(config['base_url'].rstrip('/') + '/chat/completions',
                      data=json.dumps(body, ensure_ascii=False).encode(), headers=headers, method='POST')
    try:
        handlers = [NoRedirect()]
        if urlsplit(config['base_url']).hostname in ('127.0.0.1','localhost','::1'):
            handlers.append(ProxyHandler({}))
        with build_opener(*handlers).open(request, timeout=config['timeout_seconds']) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        # Do not echo provider error bodies or headers, which may contain credentials.
        raise TrialError(f'http_{exc.code}') from None
    except (URLError, TimeoutError, OSError):
        raise TrialError('network_error_no_automatic_retry') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TrialError('response_too_large')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise TrialError('response_not_json') from None


def parse_response(response):
    if not isinstance(response, dict):
        raise TrialError('response_shape_invalid')
    choices = response.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise TrialError('response_choices_invalid')
    choice = choices[0]
    if choice.get('finish_reason') != 'stop':
        raise TrialError('response_incomplete_or_filtered')
    message = choice.get('message')
    if not isinstance(message, dict) or message.get('refusal') or message.get('tool_calls'):
        raise TrialError('response_refused_or_not_text')
    content = message.get('content')
    if not isinstance(content, str):
        raise TrialError('response_content_invalid')
    try:
        return json.loads(content)
    except ValueError:
        raise TrialError('model_output_not_json') from None


def run(config, contexts, out_dir, execute=False, sender=post):
    check_config(config)
    # Each run gets a new directory so failed runs cannot leave an old success result.
    out_dir.mkdir(parents=True, exist_ok=False)
    log = {'status': 'preparing', 'execute_requested': execute, 'api_requests_attempted': 0,
           'api_responses_received': 0, 'automatic_retry': False, 'human_review_status': 'pending',
           'semantic_correctness_verified': False, 'publication_performed': False,
           'config': config, 'requests': []}

    def finish(status, code, reason=None):
        log['status'] = status
        if reason: log['reason'] = reason
        events.dump(out_dir / 'run.json', log)
        return code, log

    if not contexts or len(contexts) > 3 or any(c['status'] != 'sections_ready' or not c['section'] for c in contexts.values()):
        return finish('blocked_source', 2, 'source_missing_changed_or_not_ready')
    requests = []
    for acc, context in contexts.items():
        body = make_request(config, document_input(acc, context))
        requests.append((acc, body))
        log['requests'].append({'accession_number': acc, 'request_sha256': digest(body),
                                'document_sha256': digest(document_input(acc, context)),
                                'messages_sha256': digest(body['messages']),
                                'source_sha256': context['source_sha256'],
                                'section_sha256': context['section']['text_sha256'],
                                'request_bytes': len(json.dumps(body, ensure_ascii=False).encode())})
    missing = []
    if not config['model']: missing.append('model')
    if config['api_key_env'] and not os.environ.get(config['api_key_env']): missing.append('api_key')
    log['missing_configuration'] = missing
    if not execute:
        return finish('dry_run', 0)
    if missing:
        return finish('blocked_configuration', 2)
    combined = {'schema_version': events.VERSION, 'filings': []}
    for i, (acc, body) in enumerate(requests):
        folder = out_dir / acc
        events.dump(folder / 'request.json', body)  # Auth headers are never stored.
        log['api_requests_attempted'] += 1
        events.dump(out_dir / 'run.json', log)
        start = time.perf_counter()
        try:
            response = sender(config, body)
            log['api_responses_received'] += 1
            events.dump(folder / 'response.json', response)
            if isinstance(response, dict):
                log['requests'][i].update({'returned_model': response.get('model'),
                                          'usage': response.get('usage')})
            candidate = parse_response(response)
        except TrialError as exc:
            log['requests'][i]['seconds'] = round(time.perf_counter() - start, 3)
            return finish('request_or_output_error', 2, str(exc))
        log['requests'][i].update({'seconds': round(time.perf_counter() - start, 3),
                                   'returned_model': response.get('model'), 'usage': response.get('usage')})
        events.dump(folder / 'candidate.json', candidate)
        checked = events.validate(candidate, {acc: contexts[acc]})
        # validate() itself performs no inference; run.json records the API calls separately.
        checked['input_kind'] = 'llm_api_candidate'
        events.dump(folder / 'validation.json', checked)
        if not checked['mechanical_checks_passed']:
            return finish('mechanical_validation_failed', 2)
        combined['filings'].extend(candidate['filings'])
    checked = events.validate(combined, contexts)
    checked['input_kind'] = 'llm_api_candidate'
    events.dump(out_dir / 'candidate.json', combined)
    events.dump(out_dir / 'validation.json', checked)
    if not checked['mechanical_checks_passed']:
        return finish('mechanical_validation_failed', 2)
    return finish('completed_pending_review', 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'llm/ollama-qwen35-9b.json')
    parser.add_argument('--source-dir', type=Path, action='append')
    parser.add_argument('--execute', action='store_true', help='実際に最大3回送信。省略時は通信しない')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir or ROOT / 'llm_runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    try:
        config = events.load_json(args.config)
        contexts = events.source_contexts(args.source_dir or [ROOT / 'cache', ROOT / 'validation/cache'])
        code, log = run(config, contexts, output, execute=args.execute)
    except (TrialError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'configuration_or_io_error',
                          'reason': str(exc) if isinstance(exc, TrialError) else type(exc).__name__})); return 1
    print(json.dumps({k: log[k] for k in ('status', 'api_requests_attempted', 'human_review_status', 'missing_configuration') if k in log}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
