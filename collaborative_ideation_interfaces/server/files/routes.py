"""Authenticated attachment HTTP endpoints."""
from flask import Blueprint, jsonify, request

from api.auth import current_study_id, login_required
from files.errors import AttachmentError
from files.preparation import FORMATS

def blueprint(service):
    bp = Blueprint('attachments', __name__)

    def object_payload():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise AttachmentError('요청 형식이 올바르지 않습니다.')
        return payload

    @bp.get('/api/attachments/config')
    @login_required
    def config():
        return jsonify(enabled=service.backend != 'off', extensions=list(FORMATS),
                       maxFileBytes=service.config['ATTACHMENT_MAX_FILE_BYTES'], maxFiles=service.config['ATTACHMENT_MAX_FILES'])

    @bp.post('/api/attachments')
    @login_required
    def init():
        payload = object_payload()
        return jsonify(service.init(current_study_id(), payload.get('name', ''), payload.get('size')))

    @bp.delete('/api/attachments/<aid>')
    @login_required
    def discard(aid):
        service.discard(current_study_id(), aid)
        return jsonify(ok=True)

    @bp.post('/api/attachments/<aid>/upload-ticket')
    @login_required
    def renew_ticket(aid):
        row = service.get(current_study_id(), aid)
        service.require_supported(row)
        if row['status'] != 'pending' or row['manifest'].get('original'):
            raise AttachmentError('이미 업로드한 파일입니다.', status=409)
        ticket = {
            'method': 'PUT', 'url': f'/api/attachments/{aid}/upload', 'headers': {'Content-Type': 'application/octet-stream'}}
        return jsonify(upload=ticket)

    @bp.put('/api/attachments/<aid>/upload')
    @login_required
    def upload(aid):
        row = service.get(current_study_id(), aid)
        service.require_supported(row)
        if service.backend != 'local' or row['status'] != 'pending' or row['manifest'].get('original'):
            raise AttachmentError('이 업로드는 더 이상 사용할 수 없습니다.', status=409)
        request.max_content_length = service.config['ATTACHMENT_MAX_FILE_BYTES']
        data = request.get_data()
        if len(data) != row['size_bytes']:
            raise AttachmentError('파일 크기가 다릅니다.')
        service.objects.put(row['manifest']['staging'], data, 'application/octet-stream')
        return jsonify(ok=True)

    @bp.post('/api/attachments/<aid>/complete')
    @login_required
    def complete(aid):
        object_payload()
        return jsonify(attachment=service.complete(current_study_id(), aid))

    return bp
