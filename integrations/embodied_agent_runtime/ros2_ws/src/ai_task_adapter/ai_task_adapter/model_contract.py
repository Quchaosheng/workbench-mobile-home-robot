"""Strict, provider-neutral contract for model-generated workflow choices."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import build_opener, HTTPRedirectHandler, Request


class ModelPlanError(ValueError):
    """Raised when an untrusted model response does not satisfy the contract."""


class ModelTransportError(RuntimeError):
    """Raised when the model endpoint cannot provide a bounded response."""


@dataclass(frozen=True)
class ModelWorkflowPlan:
    workflow_id: str
    target_id: str
    duration_ms: int


def build_messages(
    request: str,
    workflows: Iterable[str],
    targets: Iterable[str],
    max_duration_ms: int,
) -> list[dict[str, str]]:
    workflow_list = list(workflows)
    target_list = list(targets)
    system = (
        'You translate an untrusted user request into one safe workflow choice. '
        'Return JSON only, with exactly these fields: workflow_id, target_id, duration_ms. '
        f'workflow_id must be one of {workflow_list}. target_id must be one of {target_list}. '
        f'duration_ms must be an integer from 1 through {max_duration_ms}. '
        'Do not include CAN identifiers, device commands, tool calls, explanations, markdown, '
        'or extra fields.'
    )
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': request},
    ]


def parse_model_plan(
    content: str,
    workflows: Iterable[str],
    targets: Iterable[str],
    max_duration_ms: int,
) -> ModelWorkflowPlan:
    workflow_set = set(workflows)
    target_set = set(targets)
    if not workflow_set or not target_set:
        raise ModelPlanError('workflow and target allowlists must not be empty')
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ModelPlanError('model response is not JSON') from error
    expected_fields = {'workflow_id', 'target_id', 'duration_ms'}
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ModelPlanError(
            'model response must contain exactly workflow_id, target_id, duration_ms')

    workflow_id = payload['workflow_id']
    target_id = payload['target_id']
    duration_ms = payload['duration_ms']
    if not isinstance(workflow_id, str) or workflow_id not in workflow_set:
        raise ModelPlanError('workflow_id is not allowlisted')
    if not isinstance(target_id, str) or target_id not in target_set:
        raise ModelPlanError('target_id is not allowlisted')
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise ModelPlanError('duration_ms must be an integer')
    if duration_ms <= 0 or duration_ms > max_duration_ms:
        raise ModelPlanError('duration_ms is outside the configured limit')
    return ModelWorkflowPlan(workflow_id, target_id, duration_ms)


class _RejectRedirects(HTTPRedirectHandler):

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def request_chat_completion(
    endpoint: str,
    model_name: str,
    api_key: str,
    request: str,
    workflows: Iterable[str],
    targets: Iterable[str],
    max_duration_ms: int,
    timeout_sec: float,
    api_style: str = 'chat_completions',
) -> str:
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme not in {'http', 'https'} or not parsed_endpoint.netloc:
        raise ModelTransportError('model_endpoint must be an HTTP or HTTPS URL')
    if parsed_endpoint.username or parsed_endpoint.password:
        raise ModelTransportError('model_endpoint must not contain credentials')
    if api_key and parsed_endpoint.scheme != 'https':
        raise ModelTransportError('API keys require an HTTPS model_endpoint')
    if not request or len(request) > 4096:
        raise ModelTransportError('request must contain between 1 and 4096 characters')
    if not math.isfinite(timeout_sec) or timeout_sec <= 0:
        raise ModelTransportError('timeout_sec must be a positive finite value')
    if api_style not in {'chat_completions', 'responses'}:
        raise ModelTransportError('api_style must be chat_completions or responses')

    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'Embodied-Agent-Runtime/1.0',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    messages = build_messages(request, workflows, targets, max_duration_ms)
    if api_style == 'responses':
        payload = {
            'model': model_name,
            'instructions': messages[0]['content'],
            'input': messages[1]['content'],
            'max_output_tokens': 128,
        }
    else:
        payload = {
            'model': model_name,
            'max_tokens': 128,
            'temperature': 0,
            'response_format': {'type': 'json_object'},
            'messages': messages,
        }
    http_request = Request(
        endpoint,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with build_opener(_RejectRedirects()).open(http_request, timeout=timeout_sec) as response:
            response_body = response.read(1_048_577)
    except HTTPError as error:
        raise ModelTransportError(f'model endpoint returned HTTP {error.code}') from error
    except URLError as error:
        raise ModelTransportError('model endpoint is unreachable') from error
    except TimeoutError as error:
        raise ModelTransportError('model endpoint timed out') from error
    if len(response_body) > 1_048_576:
        raise ModelTransportError('model response exceeds one MiB')

    try:
        body = json.loads(response_body.decode('utf-8'))
        if api_style == 'responses':
            content = _responses_content(body)
        else:
            content = body['choices'][0]['message']['content']
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise ModelTransportError('model endpoint returned an invalid response') from error
    if not isinstance(content, str):
        raise ModelTransportError('model response content is not text')
    return content


def _responses_content(body: object) -> str:
    if not isinstance(body, dict):
        raise TypeError
    output_text = body.get('output_text')
    if isinstance(output_text, str) and output_text:
        return output_text
    output = body.get('output')
    if not isinstance(output, list):
        raise KeyError
    parts = []
    for item in output:
        if not isinstance(item, dict) or item.get('type') != 'message':
            continue
        content = item.get('content')
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict) and
                part.get('type') in {'output_text', 'text'} and
                isinstance(part.get('text'), str)
            ):
                parts.append(part['text'])
    if not parts:
        raise KeyError
    return ''.join(parts)
