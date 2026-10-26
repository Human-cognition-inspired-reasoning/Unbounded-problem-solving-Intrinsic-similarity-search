"""Public HTTP errors and SSE serialization."""
import json

from flask import jsonify


def error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def sse_event(event_type: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n"


def public_stream_event(event):
    kind = event['type']
    if kind == 'text_delta':
        return kind, {key: event[key] for key in ('delta', 'messageId', 'contentIndex', 'phase') if key in event}
    if kind == 'text_metadata':
        return kind, {'messageId': event['messageId'], 'phase': event['phase']}
    if kind == 'reasoning_delta':
        return kind, {'id': event.get('id'), 'index': int(event.get('index', 0)), 'delta': event['delta']}
    if kind == 'status':
        return kind, {'phase': event['phase']}
    if kind == 'search_started':
        return kind, {'id': event['id']}
    if kind == 'source_added':
        return kind, {'searchId': event['searchId'], 'source': event['source']}
    if kind == 'citation_added':
        return kind, event['citation']
    if kind == 'done':
        return 'message_done', event['payload']
    # Search queries remain in the private research trace.
    return None


def public_model_error(code: str) -> tuple[str, str, int]:
    if code == 'ATTACHMENT_UNAVAILABLE':
        return code, '첨부파일을 모델에 전달하지 못했습니다. 파일 저장소 또는 제공자 연결을 확인해 주세요.', 503
    if code == "CREDIT_LIMIT_EXCEEDED":
        return code, "사용 가능한 크레딧이 부족합니다.", 402
    if code == "API_KEY_MISSING":
        return code, "선택한 모델 공급자의 API 키가 서버에 설정되지 않았습니다.", 503
    if code == "CONTEXT_LIMIT_EXCEEDED":
        return code, "이 대화가 모델의 컨텍스트 한도를 초과했습니다. 새 대화를 시작해주세요.", 422
    return "LLM_REQUEST_FAILED", "응답을 생성하지 못했습니다. 다시 시도해주세요.", 502
