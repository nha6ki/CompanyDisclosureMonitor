import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from jsonschema import Draft202012Validator

import llm_runner as runner
import test_structured_events as fixtures


class LLMConnectionTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.StructuredEventChecks(); f.setUp()
        self.ctx = f.ctx
        self.ctx[f.acc]['section']['item'] = '5.02'
        self.payload = f.payload
        self.config = json.loads((runner.ROOT / 'llm/openai.example.json').read_text())
        self.config.update(model='test-model', api_key_env='TEST_LLM_KEY')
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'run'
        self.env = patch.dict(os.environ, {'TEST_LLM_KEY': 'test-token'}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def response(self, payload=None):
        return {'model': 'test-model', 'usage': {'prompt_tokens': 10, 'completion_tokens': 20},
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(payload or self.payload)}}]}

    def test_dry_run_never_sends_and_does_not_save_key(self):
        sender = MagicMock(side_effect=AssertionError('network called'))
        code, log = runner.run(self.config, self.ctx, self.out, sender=sender)
        self.assertEqual((code, log['status'], log['api_requests_attempted']), (0, 'dry_run', 0))
        sender.assert_not_called()
        self.assertNotIn('test-token', (self.out / 'run.json').read_text())

    def test_missing_configuration_stops_before_sending(self):
        self.config['model'] = None
        del os.environ['TEST_LLM_KEY']
        sender = MagicMock()
        code, log = runner.run(self.config, self.ctx, self.out, execute=True, sender=sender)
        self.assertEqual(code, 2)
        self.assertEqual(log['status'], 'blocked_configuration')
        self.assertEqual(log['missing_configuration'], ['model', 'api_key'])
        sender.assert_not_called()

    def test_mock_success_validates_and_stays_pending(self):
        sender = MagicMock(return_value=self.response())
        code, log = runner.run(self.config, self.ctx, self.out, execute=True, sender=sender)
        self.assertEqual((code, log['status']), (0, 'completed_pending_review'))
        self.assertEqual(log['api_requests_attempted'], 1)
        self.assertEqual(log['human_review_status'], 'pending')
        self.assertFalse(log['publication_performed'])
        self.assertEqual(log['requests'][0]['usage']['completion_tokens'], 20)
        self.assertTrue((self.out / 'candidate.json').exists())
        for p in self.out.rglob('*.json'):
            self.assertNotIn('test-token', p.read_text())

    def test_changed_original_blocks_model_call(self):
        self.ctx[next(iter(self.ctx))]['status'] = 'source_changed'
        sender = MagicMock()
        code, log = runner.run(self.config, self.ctx, self.out, execute=True, sender=sender)
        self.assertEqual((code, log['status']), (2, 'blocked_source'))
        sender.assert_not_called()

    def test_first_network_error_stops_without_retry(self):
        first = next(iter(self.ctx.values()))
        self.ctx['0000000001-26-000002'] = copy.deepcopy(first)
        sender = MagicMock(side_effect=runner.TrialError('http_429'))
        code, log = runner.run(self.config, self.ctx, self.out, execute=True, sender=sender)
        self.assertEqual(code, 2)
        self.assertEqual(sender.call_count, 1)
        self.assertFalse((self.out / 'candidate.json').exists())
        self.assertEqual(log['reason'], 'http_429')

    def test_wrong_document_response_does_not_produce_aggregate(self):
        self.payload['filings'][0]['accession_number'] = '0000000001-26-000099'
        code, log = runner.run(self.config, self.ctx, self.out, execute=True,
                               sender=MagicMock(return_value=self.response()))
        self.assertEqual((code, log['status']), (2, 'mechanical_validation_failed'))
        self.assertFalse((self.out / 'candidate.json').exists())

    def test_refusal_truncation_and_non_json_are_not_salvaged(self):
        for variant in ('refusal', 'length', 'invalid_json'):
            with self.subTest(variant=variant):
                response = self.response()
                choice = response['choices'][0]
                if variant == 'refusal': choice['message']['refusal'] = 'Cannot comply'
                if variant == 'length': choice['finish_reason'] = 'length'
                if variant == 'invalid_json': choice['message']['content'] = '```json\n{}\n```'
                with self.assertRaises(runner.TrialError): runner.parse_response(response)

    def test_openai_secret_cannot_be_sent_to_other_host(self):
        self.config.update(base_url='https://example.org/v1', api_key_env='OPENAI_API_KEY')
        with self.assertRaisesRegex(runner.TrialError, 'destination_mismatch'):
            runner.check_config(self.config)

    def test_provider_schema_and_request_use_source_only(self):
        schema = runner.provider_schema(runner.events.SCHEMA)
        Draft202012Validator.check_schema(schema)
        self.assertNotIn('uniqueItems', json.dumps(schema))
        acc = next(iter(self.ctx))
        body = runner.make_request(self.config, runner.document_input(acc, self.ctx[acc]))
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertIn('Alex Sample', body['messages'][1]['content'])
        self.assertNotIn('Suzanne Nora Johnson', json.dumps(body))
        self.assertNotIn('test-token', json.dumps(body))

    def test_json_object_mode_requires_explicit_configuration(self):
        self.config['response_format'] = 'json_object'
        acc = next(iter(self.ctx))
        body = runner.make_request(self.config, runner.document_input(acc, self.ctx[acc]))
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertIn('出力JSON Schema', body['messages'][0]['content'])

    def test_generation_contract_rejects_evt_prefix_before_local_validation(self):
        acc = next(iter(self.ctx))
        body = runner.make_request(self.config, runner.document_input(acc, self.ctx[acc]))
        validator = Draft202012Validator(body['response_format']['json_schema']['schema'])
        self.assertEqual(list(validator.iter_errors(self.payload)), [])
        self.payload['filings'][0]['events'][0]['event_id'] = 'evt-001'
        errors = list(validator.iter_errors(self.payload))
        self.assertTrue(any(list(e.path) == ['filings', 0, 'events', 0, 'event_id'] for e in errors))

    def test_both_response_modes_expose_full_contract_in_prompt(self):
        acc = next(iter(self.ctx))
        for mode in ('json_schema', 'json_object'):
            with self.subTest(mode=mode):
                self.config['response_format'] = mode
                body = runner.make_request(self.config, runner.document_input(acc, self.ctx[acc]))
                embedded = json.loads(body['messages'][0]['content'].split('\n出力JSON Schema:\n', 1)[1])
                self.assertEqual(embedded, runner.events.SCHEMA)

    def test_wire_request_is_json_and_secret_only_in_header(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(self.response()).encode()
        opener = MagicMock(); opener.open.return_value = response
        with patch('llm_runner.build_opener', return_value=opener):
            returned = runner.post(self.config, {'model': 'test-model'})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-token')
        self.assertNotIn(b'test-token', req.data)
        self.assertEqual(returned['model'], 'test-model')

    def test_existing_output_directory_is_preserved(self):
        self.out.mkdir()
        marker = self.out / 'previous-result.txt'; marker.write_text('preserve')
        with self.assertRaises(FileExistsError): runner.run(self.config, self.ctx, self.out)
        self.assertEqual(marker.read_text(), 'preserve')

    def test_local_presets_have_identical_messages_and_bounded_settings(self):
        bodies = []
        for name in ('ollama-qwen35-9b.json', 'ollama-qwen38-27b.json'):
            config = json.loads((runner.ROOT / 'llm' / name).read_text())
            runner.check_config(config)
            acc = next(iter(self.ctx))
            bodies.append(runner.make_request(config, runner.document_input(acc, self.ctx[acc])))
            sender = MagicMock(side_effect=AssertionError('network called'))
            _, log = runner.run(config, self.ctx, self.out / name, sender=sender)
            self.assertEqual(log['api_requests_attempted'], 0)
            self.assertEqual(log['missing_configuration'], [])
            sender.assert_not_called()
        self.assertNotEqual(bodies[0]['model'], bodies[1]['model'])
        self.assertEqual(bodies[0]['messages'], bodies[1]['messages'])
        self.assertEqual(bodies[0]['response_format'], bodies[1]['response_format'])
        for body in bodies:
            self.assertEqual(body['reasoning_effort'], 'none')
            self.assertEqual(body['temperature'], 0)
            self.assertEqual(body['max_tokens'], 8000)

    def test_generation_cannot_override_model_or_messages(self):
        self.config['generation'] = {'model': 'unexpected-model'}
        with self.assertRaisesRegex(runner.TrialError, 'generation_fields_invalid'):
            runner.check_config(self.config)

    def test_incomplete_response_keeps_elapsed_time_and_usage(self):
        response = self.response()
        response['choices'][0]['finish_reason'] = 'length'
        code, log = runner.run(self.config, self.ctx, self.out, execute=True,
                               sender=MagicMock(return_value=response))
        self.assertEqual(code, 2)
        self.assertIn('seconds', log['requests'][0])
        self.assertEqual(log['requests'][0]['usage']['completion_tokens'], 20)
        self.assertFalse((self.out / 'candidate.json').exists())


if __name__ == '__main__':
    unittest.main()
