"""Regression coverage for OpenAI message phases through SSE and storage."""
import json
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

from openai.types.responses import Response, ResponseOutputMessage

from api.application import create_app
from chat.responses import StreamingGenerationAccumulator
from chat.responses import finalized_response_parts
from models.catalog import get_model
from models.openai import _openai_input, stream_openai


COMMENTARY = '관련 자료를 확인하고 핵심 결과를 정리하겠습니다.'
ANSWER = '확인한 결과, 사용자의 판단 기준을 명시적으로 기록하는 것이 중요합니다.'


class PhaseStream:
    def __init__(self, phase_at='added', terminal='completed', text=True):
        self.phase_at, self.terminal, self.text = phase_at, terminal, text
        self.items = [ResponseOutputMessage.model_validate({
            'id': mid, 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'phase': phase, 'content': [{'type': 'output_text', 'text': value, 'annotations': (
                [{'type': 'url_citation', 'url': 'https://example.com/source', 'title': 'Source',
                  'start_index': 0, 'end_index': len(value)}] if phase == 'final_answer' else []
            )}],
        }) for mid, phase, value in [('m1', 'commentary', COMMENTARY), ('m2', 'final_answer', ANSWER)]]
        self.response = Response.model_construct(
            id='phase-fixture', output=self.items, status=terminal, usage=None,
            incomplete_details=None,
        )

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def __iter__(self):
        for index, item in enumerate(self.items):
            yield N(type='response.output_item.added', output_index=index,
                    item=item if self.phase_at == 'added' else item.model_copy(update={'phase': None}))
            if self.text:
                value = item.content[0].text
                for piece in (value[:5], value[5:]):
                    yield N(type='response.output_text.delta', item_id=item.id, content_index=0,
                            output_index=index, delta=piece)
                for annotation in item.content[0].annotations:
                    yield N(type='response.output_text.annotation.added', item_id=item.id,
                            content_index=0, annotation=annotation)
            yield N(type='response.output_item.done', output_index=index,
                    item=item if self.phase_at != 'completed' else item.model_copy(update={'phase': None}))
        yield N(type=f'response.{self.terminal}', response=self.response)


def phase_events(phase_at='added', terminal='completed', text=True):
    return stream_openai([{'role': 'user', 'content': '설명해줘'}], 'gpt-6.1-sol', False, 'medium',
                        client=N(responses=N(stream=lambda **kwargs: PhaseStream(phase_at, terminal, text))))


class OpenAIPhaseTest(unittest.TestCase):
    def test_boundaries_late_phases_citations_and_replay(self):
        for phase_at in ('added', 'done', 'completed'):
            with self.subTest(phase_at=phase_at):
                events = list(phase_events(phase_at))
                acc = StreamingGenerationAccumulator([], get_model('gpt-6.1-sol'))
                for event in events: acc.observe(event)
                generation = events[-1]['generation']
                parts = finalized_response_parts(acc.response_parts, generation)
                texts = [p for p in parts if p['type'] == 'text']
                self.assertEqual([p['text'] for p in texts], [COMMENTARY, ANSWER])
                self.assertEqual([p['phase'] for p in texts], ['commentary', 'final_answer'])
                live_citation = next(e['citation'] for e in events if e['type'] == 'citation_added')
                saved_citation = generation['webSearchTrace']['citations'][0]
                self.assertEqual(live_citation['startIndex'], len(COMMENTARY))
                self.assertEqual(saved_citation['endIndex'], len(COMMENTARY + ANSWER))
                messages = [{'id': 'u', 'role': 'user', 'content': '설명해줘'},
                            {'id': 'a', 'role': 'assistant', 'content': generation['content'], 'responseParts': parts}]
                for history in ([], [{**generation['providerState'], 'outputMessageId': 'a'}]):
                    replay = _openai_input(messages, history)
                    self.assertEqual([p['phase'] for p in replay if p.get('role') == 'assistant'],
                                     ['commentary', 'final_answer'])

    def test_terminal_only_and_incomplete_response_keep_phases(self):
        for terminal in ('completed', 'incomplete'):
            generation = list(phase_events(terminal=terminal, text=False))[-1]['generation']
            parts = finalized_response_parts([], generation)
            self.assertEqual([p['phase'] for p in parts if p['type'] == 'text'], ['commentary', 'final_answer'])
            self.assertEqual(generation['providerStatus'], terminal)

    def test_http_stream_and_reload_keep_phases(self):
        with tempfile.TemporaryDirectory() as directory, patch('api.routes.stream_response', side_effect=lambda *a, **kw: phase_events()):
            app = create_app({'TESTING': True, 'DATA_DIR': directory, 'SECRET_KEY': 'test',
                              'SEED_DEMO_ACCOUNT': True, 'DEFAULT_CREDIT_LIMIT_USD': 10.0})
            client = app.test_client()
            self.assertEqual(client.post('/api/auth/login', json={'studyId': 'P-001', 'password': 'prototype'}).status_code, 200)
            conv = client.post('/api/conversations', json={'modelId': 'gpt-6.1-sol'}).get_json()['conversation']
            response = client.post(f"/api/conversations/{conv['id']}/messages",
                                   json={'modelId': 'gpt-6.1-sol', 'content': '설명해줘'},
                                   headers={'Accept': 'text/event-stream'})
            self.assertEqual(response.status_code, 200)
            events = []
            for block in response.data.decode().strip().split('\n\n'):
                lines = block.splitlines()
                events.append((lines[0][7:], json.loads(lines[1][6:])))
            deltas = [data for kind, data in events if kind == 'text_delta']
            self.assertEqual([d['phase'] for d in deltas], ['commentary'] * 2 + ['final_answer'] * 2)
            saved = events[-1][1]['assistantMessage']
            reloaded = client.get(f"/api/conversations/{conv['id']}/messages").get_json()['messages'][-1]
            self.assertEqual(saved['responseParts'], reloaded['responseParts'])
            self.assertEqual([p['phase'] for p in reloaded['responseParts'] if p['type'] == 'text'], ['commentary', 'final_answer'])
