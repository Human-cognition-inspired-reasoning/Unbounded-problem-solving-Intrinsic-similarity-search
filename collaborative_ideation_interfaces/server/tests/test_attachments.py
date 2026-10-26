"""File boundaries: ownership, immutable research data, conversion, and provider input."""
import base64
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from api.application import create_app
from files.errors import AttachmentError
from files.preparation import prepare, digest
from models.anthropic import _anthropic_messages
from models.common import _normalize_messages, _estimated_input_tokens
from models.gemini import _gemini_interaction_input
from models.openai import _openai_input
from tests.fakes import fake_stream_response


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        keys = patch.dict(os.environ, {'OPENAI_API_KEY':'test-key','ANTHROPIC_API_KEY':'test-key','GOOGLE_API_KEY':'test-key'})
        keys.start()
        self.addCleanup(keys.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.temp.name, 'SECRET_KEY': 'test',
                               'SEED_DEMO_ACCOUNT': True, 'ATTACHMENT_STORAGE': 'local'})
        self.client = self.app.test_client()
        self.client.post('/api/auth/login', json={'studyId': 'P-001', 'password': 'prototype'})
        self.service = self.app.extensions['attachments']
        self.store = self.service.store

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, data=b'item,value\nmarker,91\n', name='data.csv', complete=True):
        r = self.client.post('/api/attachments', json={'name': name, 'size': len(data)})
        self.assertEqual(r.status_code, 200, r.get_json())
        ticket = r.get_json()
        aid = ticket['attachment']['id']
        r = self.client.put(ticket['upload']['url'], data=data, content_type='application/octet-stream')
        self.assertEqual(r.status_code, 200, r.get_json())
        if not complete:
            return aid
        r = self.client.post(f'/api/attachments/{aid}/complete', json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()['attachment']

    def conversation(self):
        r = self.client.post('/api/conversations', json={'modelId': 'gpt-5.6-luna'})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()['conversation']['id']

    def test_original_hash_and_idempotent_complete(self):
        data = 'name,value\r\n한글,91\r\n'.encode()
        attachment = self.upload(data)
        aid = attachment['id']
        self.assertEqual(attachment['sha256'], digest(data))
        manifest = self.service.get('P-001', aid)['manifest']
        self.assertEqual(self.service.objects.read(manifest['original']['key'], 1024), data)
        self.assertEqual(self.service.objects.read(manifest['files'][0]['key'], 1024), 'name,value\n한글,91\n'.encode())
        # Research storage remains available internally, but participants cannot download it.
        for path in ('original', 'files/0'):
            self.assertEqual(self.client.get(f'/api/attachments/{aid}/{path}').status_code, 404)
        repeated = self.client.post(f'/api/attachments/{aid}/complete', json={}).get_json()['attachment']
        self.assertEqual(repeated, attachment)
        self.assertNotIn('key', json.dumps(attachment))
        self.assertEqual(self.client.put(f'/api/attachments/{aid}/upload', data=data).status_code, 409)

    def test_ownership_and_bound_files_cannot_be_removed_or_rebound(self):
        a = self.upload()
        self.store.ensure_account('P-002', 'another-password', 10)
        other = self.app.test_client()
        other.post('/api/auth/login', json={'studyId': 'P-002', 'password': 'another-password'})
        self.assertEqual(other.post(f"/api/attachments/{a['id']}/complete", json={}).status_code, 404)
        self.assertEqual(other.delete(f"/api/attachments/{a['id']}").status_code, 404)
        with self.store.write('P-002') as tx:
            with self.assertRaises(AttachmentError):
                self.service.bind(tx, [a['id']], {})
        cid = self.conversation()
        with patch('api.routes.stream_response', side_effect=fake_stream_response):
            r = self.client.post(f'/api/conversations/{cid}/messages', json={'modelId':'gpt-5.6-luna','reasoningEffort':'none','content':'read', 'attachmentIds':[a['id']]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.client.delete(f"/api/attachments/{a['id']}").status_code, 409)
        with self.store.write('P-001') as tx:
            with self.assertRaises(AttachmentError):
                self.service.bind(tx, [a['id']], {})

    def test_file_only_message_and_followup_replay_without_prompt_concat(self):
        a = self.upload()
        cid = self.conversation()
        with patch('api.routes.stream_response', side_effect=fake_stream_response) as stream:
            first = self.client.post(f'/api/conversations/{cid}/messages', json={'modelId':'gpt-5.6-luna','reasoningEffort':'none','content':'', 'attachmentIds':[a['id']]})
            self.assertEqual(first.status_code, 200, first.get_json())
            second = self.client.post(f'/api/conversations/{cid}/messages', json={'modelId':'gpt-5.6-luna','reasoningEffort':'none','content':'What was the value?'})
            self.assertEqual(second.status_code, 200, second.get_json())
            messages = stream.call_args.args[0]
        self.assertEqual(messages[0]['content'], '')
        self.assertEqual(base64.b64decode(messages[0]['_files'][0]['base64']), b'item,value\nmarker,91\n')
        self.assertEqual(messages[-1]['content'], 'What was the value?')
        saved = self.store.read('P-001').messages(cid)[0]
        self.assertNotIn('_files', saved)
        self.assertEqual(saved['attachments'][0]['id'], a['id'])
        calls = self.store._connection().execute('SELECT request FROM model_calls WHERE study_id=?', ('P-001',)).fetchall()
        self.assertTrue(all(a['sha256'] in row[0] for row in calls))
        self.assertTrue(all('base64' not in row[0] for row in calls))

    def test_upload_binary_limit_separate_from_json_limit_and_quota(self):
        aid = self.upload(b'x' * (1024**2 + 1), 'large.txt', complete=False)
        self.assertEqual(self.client.delete(f'/api/attachments/{aid}').status_code, 200)
        self.service.config['ATTACHMENT_PARTICIPANT_BYTES'] = 30
        self.upload(b'a,b\nx,y\n')
        r = self.client.post('/api/attachments', json={'name':'a.csv','size':25})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['error']['code'], 'ATTACHMENT_QUOTA')

    def test_invalid_file_failure_preserves_retry_and_releases_claim(self):
        aid = self.upload(b'not a pdf', 'bad.pdf', complete=False)
        for _ in range(2):
            r = self.client.post(f'/api/attachments/{aid}/complete', json={})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(self.service.get('P-001', aid)['status'], 'pending')
        self.assertEqual(self.client.delete(f'/api/attachments/{aid}').status_code, 200)
        self.assertIsNone(self.store.read('P-001').attachment(aid))

    def test_xlsx_and_pptx_are_not_advertised_or_accepted(self):
        config = self.client.get('/api/attachments/config').get_json()
        self.assertNotIn('maxSheets', config)
        for extension in ('.xlsx', '.pptx'):
            self.assertNotIn(extension, config['extensions'])
            for suffix in (extension, extension.upper()):
                r = self.client.post('/api/attachments', json={'name':'blocked'+suffix,'size':10})
                self.assertEqual(r.status_code,400,r.get_json())
                with self.assertRaises(AttachmentError):
                    prepare(b'placeholder','blocked'+suffix,self.app.config)
        self.assertEqual(self.store.read('P-001').attachments(),[])
        for extension in ('.pdf','.docx','.txt','.md','.png','.jpg','.jpeg','.csv','.tsv'):
            self.assertIn(extension,config['extensions'])

    def test_existing_disallowed_uploads_cannot_be_finalized_or_sent(self):
        # Simulate records created before the supported-format policy changed.
        # Stored bytes stay intact; only new upload/finalization/binding is denied.
        for extension in ('.xlsx','.pptx'):
            a = self.upload()
            aid = a['id']
            with self.store.write('P-001') as tx:
                tx._connection.execute('UPDATE attachments SET filename=? WHERE id=?',('old'+extension,aid))
            r=self.client.post(f'/api/attachments/{aid}/complete',json={})
            self.assertEqual(r.status_code,400,r.get_json())
            with self.store.write('P-001') as tx:
                with self.assertRaises(AttachmentError):
                    self.service.bind(tx,[aid],{})
            original = self.service.get('P-001', aid)['manifest']['original']
            self.assertEqual(self.service.objects.read(original['key'], 1024), b'item,value\nmarker,91\n')
            hydrated=self.service.resolve('P-001',[{'role':'user','content':'old question','attachments':[a]}],'openai')
            self.assertEqual(base64.b64decode(hydrated[0]['_files'][0]['base64']),b'item,value\nmarker,91\n')
            with self.store.write('P-001') as tx:
                tx.update_attachment(aid,status='pending')
            self.assertEqual(self.client.post(f'/api/attachments/{aid}/upload-ticket',json={}).status_code,400)
            self.assertEqual(self.client.put(f'/api/attachments/{aid}/upload',data=b'changed').status_code,400)

    def test_csv_never_silently_truncates(self):
        with self.assertRaises(AttachmentError):
            prepare(b'a,b\n'*1001,'a.csv',self.app.config)
        self.assertEqual(prepare(b'a,b\n'*1000,'a.csv',self.app.config)[0]['rows'],1000)

    def test_text_pdf_preserves_korean_and_markdown(self):
        from pypdf import PdfReader
        f=prepare('# 제목\n  한글 문장 **강조**\n'.encode(),'note.md',self.app.config)[0]
        text=PdfReader(io.BytesIO(f['data'])).pages[0].extract_text()
        self.assertIn('# 제목',text); self.assertIn('**강조**',text)
        self.assertEqual(f['mime'],'application/pdf')

    def test_context_rejections_do_not_bind_file(self):
        a=self.upload()
        cid=self.conversation()
        self.service.config['ATTACHMENT_CONTEXT_BYTES']=1
        r=self.client.post(f'/api/conversations/{cid}/messages',json={'modelId':'gpt-5.6-luna','reasoningEffort':'none','content':'hi','attachmentIds':[a['id']]})
        self.assertEqual(r.status_code,409)
        self.assertIsNone(self.service.get('P-001',a['id'])['message_id'])
        self.assertEqual(self.store.read('P-001').messages(cid),[])

    def test_credit_preflight_rejects_before_binding_or_provider_call(self):
        a=self.upload(); cid=self.conversation()
        with self.store.write('P-001') as tx:
            tx._connection.execute('UPDATE participants SET credit_limit_usd=0.000001 WHERE study_id=?',('P-001',))
        with patch('api.routes.stream_response') as stream:
            r=self.client.post(f'/api/conversations/{cid}/messages',json={'modelId':'gpt-5.6-luna','content':'read','attachmentIds':[a['id']]})
            self.assertEqual(r.status_code,402,r.get_json())
            self.assertEqual(r.get_json()['error']['code'],'ATTACHMENT_BUDGET')
            stream.assert_not_called()
        self.assertIsNone(self.service.get('P-001',a['id'])['message_id'])

    def test_each_provider_receives_document_blocks_and_prior_files(self):
        files=[{'name':'a.pdf','mime':'application/pdf','base64':'YWJj','estimatedTokens':4000},
               {'name':'a.png','mime':'image/png','base64':'YWJj','estimatedTokens':6000},
               {'name':'a.csv','mime':'text/csv','base64':'YWJj','fileId':'file-test','estimatedTokens':10}]
        messages=[{'role':'user','content':'original question','_files':files},
                  {'role':'assistant','content':'answer'}, {'role':'user','content':'followup'}]
        oa=_openai_input(messages,None); an=_anthropic_messages(messages,None); ge=_gemini_interaction_input(messages,None)
        self.assertEqual(oa[0]['content'][0]['text'],'original question')
        self.assertEqual([p['type'] for p in oa[0]['content'][1:]],['input_file','input_image','input_file'])
        self.assertEqual(oa[0]['content'][1]['file_data'], 'data:application/pdf;base64,YWJj')
        self.assertEqual(oa[0]['content'][2]['image_url'], 'data:image/png;base64,YWJj')
        self.assertEqual(an[0]['content'][1]['source'], {'type': 'base64', 'media_type': 'application/pdf', 'data': 'YWJj'})
        self.assertEqual(an[0]['content'][2]['source'], {'type': 'base64', 'media_type': 'image/png', 'data': 'YWJj'})
        self.assertEqual(an[0]['content'][3]['source'],{'type':'file','file_id':'file-test'})
        self.assertEqual([p['data'] for p in ge[0]['content'][1:]], ['YWJj'] * 3)
        self.assertEqual([p['type'] for p in ge[0]['content'][1:]],['document','image','document'])
        self.assertGreaterEqual(_estimated_input_tokens(_normalize_messages(messages)),10010)

    def test_provider_file_lookup_failure_finishes_generation(self):
        a=self.upload(); cid=self.conversation()
        with patch.object(self.service,'resolve',side_effect=FileNotFoundError('missing')):
            r=self.client.post(f'/api/conversations/{cid}/messages',json={'modelId':'gpt-5.6-luna','reasoningEffort':'none','content':'read','attachmentIds':[a['id']]},headers={'Accept':'text/event-stream'})
            self.assertIn('ATTACHMENT_UNAVAILABLE',r.get_data(as_text=True))
        self.assertFalse(self.client.get('/api/conversations').get_json()['conversations'][0]['isGenerating'])

    def test_export_checksums_and_prune_preserves_submitted_files(self):
        from ops.attachments import export_participant, prune_unsubmitted
        from pathlib import Path
        a=self.upload(); cid=self.conversation()
        with patch('api.routes.stream_response', side_effect=fake_stream_response):
            self.client.post(f'/api/conversations/{cid}/messages',json={'modelId':'gpt-5.6-luna','content':'read','attachmentIds':[a['id']]})
        abandoned=self.upload()
        with self.store.write('P-001') as tx:
            for aid in [a['id'],abandoned['id']]:
                tx.update_attachment(aid,updated_at='2020-01-01T00:00:00+00:00')
        self.assertEqual(prune_unsubmitted(self.service),1)
        self.assertEqual(prune_unsubmitted(self.service,apply=True),1)
        self.assertIsNotNone(self.service.get('P-001',a['id']))
        target=Path(self.temp.name)/'export'
        self.assertEqual(export_participant(self.service,'P-001',target),1)
        manifest=json.loads((target/'attachments.json').read_text())[0]['manifest']
        for f in [manifest['original'],*manifest['files']]:
            self.assertEqual(digest((target/f['exportPath']).read_bytes()),f['sha256'])
        # Whole-participant updates must preserve attachment foreign keys.
        self.store.update_participant('P-001',lambda state: None)
        self.assertIsNotNone(self.service.get('P-001',a['id'])['message_id'])

    def test_derived_quota_failure_can_be_removed(self):
        self.service.config['ATTACHMENT_PARTICIPANT_BYTES']=100
        aid=self.upload('한글'.encode(),'note.txt',complete=False)
        r=self.client.post(f'/api/attachments/{aid}/complete',json={})
        self.assertEqual(r.status_code,409,r.get_json())
        self.assertEqual(self.client.delete(f'/api/attachments/{aid}').status_code,200)

    def test_local_resolution_checks_bytes_before_sending_to_providers(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (2, 2), 'white').save(image, format='PNG')
        attachment = self.upload(image.getvalue(), 'image.png')
        messages = [{'role': 'user', 'content': 'question', 'attachments': [attachment]}]
        file = self.service.get('P-001', attachment['id'])['manifest']['files'][0]
        original = self.service.objects.read(file['key'], 1024)
        for provider in ('openai', 'anthropic', 'google'):
            with self.subTest(provider=provider):
                resolved = self.service.resolve('P-001', messages, provider)
                self.assertEqual(base64.b64decode(resolved[0]['_files'][0]['base64']), original)
                self.assertNotIn('url', resolved[0]['_files'][0])
        self.assertNotIn('_files', messages[0])
        self.service.objects.path(file['key']).write_bytes(b'corrupt')
        for provider in ('openai', 'anthropic', 'google'):
            with self.subTest(provider=provider):
                with self.assertRaises(AttachmentError) as caught:
                    self.service.resolve('P-001', messages, provider)
                self.assertEqual(caught.exception.code, 'ATTACHMENT_INTEGRITY')

    def test_replayed_staging_cannot_replace_original(self):
        a=self.upload()
        row=self.service.get('P-001',a['id'])
        self.service.objects.put(row['manifest']['staging'],b'changed','application/octet-stream')
        self.assertEqual(self.service.complete('P-001',a['id'])['sha256'],a['sha256'])
        self.assertEqual(self.service.objects.read(row['manifest']['original']['key'],1024),b'item,value\nmarker,91\n')

if __name__=='__main__': unittest.main()
