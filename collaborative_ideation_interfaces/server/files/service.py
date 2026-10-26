"""Attachment lifecycle and private provider resolution, independent of HTTP."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading
import uuid

from files.errors import AttachmentError
from files.objects import LocalObjects
from files.preparation import FORMATS, PREPARATION_VERSION, digest, prepare
from runtime.config import configure_attachments


def now():
    return datetime.now(timezone.utc).isoformat()


def public_attachment(row):
    manifest = row['manifest']
    result = {'id': row['id'], 'name': row['filename'], 'size': row['size_bytes'], 'status': row['status'],
              'sha256': manifest.get('original', {}).get('sha256'), 'preparationVersion': manifest.get('preparationVersion', PREPARATION_VERSION),
              'files': [{k: v for k, v in f.items() if k != 'key'} for f in manifest.get('files', [])]}
    return result


class AttachmentService:
    def __init__(self, config, store):
        self.config = configure_attachments(config)
        self.store = store
        self.backend = self.config['ATTACHMENT_STORAGE']
        if self.backend not in ('off', 'local'):
            raise ValueError('ATTACHMENT_STORAGE must be off or local')
        self.objects = (LocalObjects(Path(self.config['DATA_DIR']) / 'attachments')
                        if self.backend == 'local' else None)
        self.preparing = threading.BoundedSemaphore(2)
        self.provider_lock = threading.Lock()

    def require_enabled(self):
        if self.objects is None:
            raise AttachmentError('파일 첨부가 아직 설정되지 않았습니다.', 'ATTACHMENTS_DISABLED', 503)

    def get(self, study_id, attachment_id):
        self.require_enabled()
        row = self.store.read(study_id).attachment(attachment_id)
        if not row:
            raise AttachmentError('첨부파일을 찾을 수 없습니다.', 'ATTACHMENT_NOT_FOUND', 404)
        if row['backend'] != self.backend:
            raise AttachmentError('첨부파일 저장소 설정이 변경되었습니다.', 'ATTACHMENT_STORAGE_MISMATCH', 503)
        return row

    @staticmethod
    def require_supported(row):
        if Path(row['filename']).suffix.lower() not in FORMATS:
            raise AttachmentError('지원하지 않는 파일 형식입니다. PDF 또는 CSV로 저장해 첨부해 주세요.')

    def init(self, study_id, filename, size):
        self.require_enabled()
        filename = str(filename).replace('\\', '/').split('/')[-1]
        if not filename or len(filename) > 180 or any(ord(c) < 32 for c in filename):
            raise AttachmentError('파일명이 올바르지 않습니다.')
        if Path(filename).suffix.lower() not in FORMATS:
            raise AttachmentError('지원하지 않는 파일 형식입니다.')
        if type(size) is not int or not 0 < size <= self.config['ATTACHMENT_MAX_FILE_BYTES']:
            raise AttachmentError('파일 크기가 허용 범위를 벗어났습니다.')
        aid = str(uuid.uuid4())
        staging = f'staging/{study_id}/{aid}'
        row = dict(id=aid, filename=filename, size_bytes=size, stored_bytes=size, backend=self.backend,
                   status='pending', created_at=now(), updated_at=now(), manifest={'staging': staging}, provider_refs={})
        with self.store.write(study_id) as tx:
            uploads = tx.attachments()
            if len(uploads) >= 500 or sum(a['stored_bytes'] for a in uploads) + size > self.config['ATTACHMENT_PARTICIPANT_BYTES']:
                raise AttachmentError('참여자 첨부 저장 한도에 도달했습니다.', 'ATTACHMENT_QUOTA', 409)
            tx.insert_attachment(row)
        ticket = {'method': 'PUT', 'url': f'/api/attachments/{aid}/upload', 'headers': {'Content-Type': 'application/octet-stream'}}
        return {'attachment': public_attachment(row), 'upload': ticket}

    def complete(self, study_id, aid):
        row = self.get(study_id, aid)
        self.require_supported(row)
        if row['status'] == 'ready':
            return public_attachment(row)
        if not self.preparing.acquire(blocking=False):
            raise AttachmentError('파일 준비 요청이 많습니다. 잠시 후 다시 시도해 주세요.', 'ATTACHMENT_BUSY', 429)
        claimed = False
        try:
            with self.store.write(study_id) as tx:
                row = tx.attachment(aid)
                if row['status'] == 'ready':
                    return public_attachment(row)
                if row['status'] == 'deleting':
                    raise AttachmentError('제거 중인 파일입니다. 다시 첨부해 주세요.', status=409)
                stale = datetime.fromisoformat(row['updated_at']) < datetime.now(timezone.utc) - timedelta(minutes=3)
                if row['status'] == 'processing' and not stale:
                    raise AttachmentError('이미 준비 중인 파일입니다. 잠시 후 다시 시도해 주세요.', 'ATTACHMENT_BUSY', 409)
                tx.update_attachment(aid, status='processing', updated_at=now())
                claimed = True
            manifest = row['manifest']
            source_key = manifest.get('original', {}).get('key') or manifest['staging']
            try:
                try:
                    data = self.objects.read(source_key, self.config['ATTACHMENT_MAX_FILE_BYTES'])
                except FileNotFoundError:
                    data = self.objects.read(manifest['staging'], self.config['ATTACHMENT_MAX_FILE_BYTES'])
            except (FileNotFoundError, ValueError) as exc:
                raise AttachmentError('업로드가 완료되지 않았거나 파일 크기가 다릅니다. 다시 첨부해 주세요.') from exc
            if len(data) != row['size_bytes']:
                raise AttachmentError('업로드된 파일 크기가 다릅니다. 다시 첨부해 주세요.')
            suffix = Path(row['filename']).suffix.lower()
            sha = digest(data)
            if manifest.get('original') and manifest['original']['sha256'] != sha:
                raise AttachmentError('업로드한 원본이 변경되었습니다. 다시 첨부해 주세요.')
            original_key = f'originals/{study_id}/{aid}/{sha}{suffix}'
            manifest['original'] = {'key': original_key, 'sha256': sha, 'size': len(data), 'mime': FORMATS[suffix], 'name': row['filename']}
            manifest['preparationVersion'] = PREPARATION_VERSION
            with self.store.write(study_id) as tx:
                tx.update_attachment(aid, manifest=manifest, updated_at=now())
            self.objects.put(original_key, data, FORMATS[suffix])
            previous_keys = manifest.get('retiredKeys', []) + [f['key'] for f in manifest.get('files', []) if f['key'] != original_key]
            files = prepare(data, row['filename'], self.config)
            manifest['files'] = []
            blobs = []
            for index, item in enumerate(files):
                blob = item.pop('data')
                item.update(index=index, size=len(blob), sha256=digest(blob))
                item['key'] = original_key if item['sha256'] == sha else f'derived/{study_id}/{aid}/{item["sha256"]}{Path(item["name"]).suffix.lower()}'
                blobs.append(blob)
                manifest['files'].append(item)
            if sum(f['estimatedTokens'] for f in manifest['files']) > self.config['ATTACHMENT_CONTEXT_TOKENS']:
                raise AttachmentError('파일의 추정 입력량이 한도를 초과합니다. 페이지 또는 표 범위를 줄여 주세요.')
            manifest['retiredKeys'] = list(set(previous_keys) - {f['key'] for f in manifest['files']})
            for key in manifest['retiredKeys']:
                self.objects.delete(key)
            manifest['retiredKeys'] = []
            stored = len(data) + sum(f['size'] for f in manifest['files'] if f['sha256'] != sha)
            with self.store.write(study_id) as tx:
                total = sum(a['stored_bytes'] for a in tx.attachments() if a['id'] != aid)
                if total + stored > self.config['ATTACHMENT_PARTICIPANT_BYTES']:
                    raise AttachmentError('변환본을 포함한 첨부 저장 한도에 도달했습니다.', 'ATTACHMENT_QUOTA', 409)
                tx.update_attachment(aid, manifest=manifest, stored_bytes=stored, updated_at=now())
            for file, blob in zip(manifest['files'], blobs):
                self.objects.put(file['key'], blob, file['mime'])
            with self.store.write(study_id) as tx:
                tx.update_attachment(aid, status='ready', updated_at=now())
            # Ready attachments use the preserved original and prepared files.
            try:
                self.objects.delete(manifest['staging'])
            except Exception:
                # Discard/prune can retry cleanup for unsubmitted attachments.
                pass
            return public_attachment(self.get(study_id, aid))
        except Exception as exc:
            if claimed:
                with self.store.write(study_id) as tx:
                    current = tx.attachment(aid)
                    if current and current['status'] == 'processing':
                        tx.update_attachment(aid, status='pending', updated_at=now(), manifest=manifest if 'manifest' in locals() else current['manifest'])
            if isinstance(exc, AttachmentError):
                raise
            raise AttachmentError('파일을 준비하지 못했습니다. 파일이 올바른지 확인하고 다시 시도해 주세요.', 'ATTACHMENT_PREPARATION_FAILED', 503) from exc
        finally:
            self.preparing.release()

    def discard(self, study_id, aid, before=None):
        self.get(study_id, aid)
        with self.store.write(study_id) as tx:
            row = tx.attachment(aid)
            if before and (datetime.fromisoformat(row['updated_at']) >= before or row['message_id']):
                return False
            if row['message_id'] or row['status'] == 'processing':
                raise AttachmentError('전송했거나 준비 중인 파일은 제거할 수 없습니다.', status=409)
            tx.update_attachment(aid, status='deleting', updated_at=now())
        manifest = row['manifest']
        keys = {manifest['staging'], *manifest.get('retiredKeys', [])}
        if manifest.get('original'):
            keys.add(manifest['original']['key'])
        keys.update(f['key'] for f in manifest.get('files', []))
        for key in keys:
            self.objects.delete(key)
        with self.store.write(study_id) as tx:
            tx.delete_unbound_attachment(aid)
        return True

    def bind(self, tx, ids, message):
        if not isinstance(ids, list) or len(ids) > self.config['ATTACHMENT_MAX_FILES'] or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise AttachmentError('첨부파일 목록이 올바르지 않습니다.')
        result = []
        for aid in ids:
            row = tx.attachment(aid)
            if not row or row['status'] != 'ready' or row['message_id'] or row['backend'] != self.backend:
                raise AttachmentError('사용할 수 없는 첨부파일입니다. 다시 첨부해 주세요.')
            self.require_supported(row)
            result.append(public_attachment(row))
        if result:
            message['attachments'] = result

    def validate_context(self, messages):
        files = [f for m in messages for a in m.get('attachments', []) for f in a['files']]
        if (len(files) > 20 or sum(f['size'] for f in files) > self.config['ATTACHMENT_CONTEXT_BYTES']
            or sum(f.get('pages', 0) for f in files) > self.config['ATTACHMENT_MAX_PAGES']
            or sum(f['estimatedTokens'] for f in files) > self.config['ATTACHMENT_CONTEXT_TOKENS']):
            raise AttachmentError('이 대화의 첨부 입력 한도에 도달했습니다. 필요한 파일만 새 대화에 첨부해 주세요.', 'ATTACHMENT_CONTEXT_LIMIT', 409)

    def resolve(self, study_id, messages, provider):
        result = copy.deepcopy(messages)
        for message in result:
            if not message.get('attachments'):
                continue
            message['_files'] = []
            for attachment in message.get('attachments', []):
                row = self.get(study_id, attachment['id'])
                for file in row['manifest']['files']:
                    item = dict(file)
                    if provider == 'anthropic' and file['mime'] == 'text/csv':
                        # A real uploaded document reference, not a text block appended to the question.
                        item['fileId'] = self.anthropic_csv(study_id, row['id'], file)
                    else:
                        data = self.objects.read(file['key'], self.config['ATTACHMENT_MAX_DERIVED_BYTES'])
                        if digest(data) != file['sha256']:
                            raise AttachmentError('보관된 파일의 무결성을 확인할 수 없습니다.', 'ATTACHMENT_INTEGRITY', 503)
                        item['base64'] = base64.b64encode(data).decode('ascii')
                    message['_files'].append(item)
        return result

    def anthropic_csv(self, study_id, aid, file):
        from anthropic import Anthropic, NotFoundError
        key = os.getenv('ANTHROPIC_API_KEY', '')
        if not key:
            raise AttachmentError('Claude API 설정을 확인해 주세요.', 'MISSING_API_KEY', 503)
        cache_key = digest(key.encode())[:16] + ':' + file['sha256']
        with self.provider_lock:
            row = self.get(study_id, aid)
            refs = row['provider_refs']
            if cache_key in refs:
                with Anthropic(api_key=key, timeout=30, max_retries=1) as client:
                    try:
                        client.beta.files.retrieve_metadata(refs[cache_key])
                        return refs[cache_key]
                    except NotFoundError:
                        pass  # Recreate an expired/deleted provider copy from our immutable CSV.
            data = self.objects.read(file['key'], self.config['ATTACHMENT_MAX_DERIVED_BYTES'])
            if digest(data) != file['sha256']:
                raise AttachmentError('보관된 표 파일이 변경되었습니다.', 'ATTACHMENT_INTEGRITY', 503)
            with Anthropic(api_key=key, timeout=45, max_retries=1) as client:
                uploaded = client.beta.files.upload(file=(file['name'], data, 'text/plain'))
            refs[cache_key] = uploaded.id
            with self.store.write(study_id) as tx:
                tx.update_attachment(aid, provider_refs=refs)
            return uploaded.id
