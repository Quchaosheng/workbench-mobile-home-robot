"""One versioned place that decides what may leave the process.

Structured logs, evaluation reports and model-call records are artifacts: CI
uploads them, operators copy them and dashboards render them.  A credential that
reaches one of them is already exposed, so the leak has to stop at the single
point where untrusted text becomes an artifact.  This module is that point.

The rule table below is intentionally auditable:

* :data:`REDACTION_RULES_VERSION` names the exact rules.  Every artifact that
  was scrubbed carries the version that produced it, so "was this line redacted,
  and by which rules?" is answerable from the line itself.
* Known credential shapes are matched by format: GitHub tokens, ``sk-`` keys,
  JWTs, PEM blocks, repository/cloud prefixes, ``Authorization`` and ``Cookie``
  headers, URL userinfo, and credential query or CLI parameters.
* Arbitrary credential keys are matched by name, for payloads whose key names we
  do not control.
* Raw media and prompts are replaced by a reference marker instead of being
  copied, which is the "evidence by reference" rule in
  ``docs/security/hardening.md``.

Three limits are deliberate and must not be papered over:

* A secret with no known format and no credential-shaped key cannot be found in
  free text.  Redaction is a last line of defense, never a reason to log a
  secret.
* Correlation fields (``run_id``, ``event_id``, ``action_id``, ``sequence_no``,
  ``timestamp``, evidence references) are preserved on purpose, because an
  artifact that cannot be joined to a run is not evidence.
* Metadata suffixes win over raw-evidence keys, so ``prompt_sha256`` stays
  available: a hash is the safe substitute for the raw prompt, not a copy of it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTION_RULES_VERSION = "redaction-rules-v1"
REDACTED = "<redacted>"
EVIDENCE_REDACTED = "<evidence-ref:redacted>"
REDACTION_MARKER_KEY = "redaction"

# Identifiers and ordering keys are copied through untouched, including any
# string that only looks credential-shaped.  A credential must therefore never
# be used as an identifier; see the module docstring.
IDENTIFIER_FIELDS = frozenset(
    {
        "run_id",
        "event_id",
        "action_id",
        "task_id",
        "case_id",
        "correlation_ref",
        "sequence_no",
        "timestamp",
        "occurred_at",
        "service",
        "source",
        "level",
        "event",
    }
)

_KEY_SEPARATORS = re.compile(r"[^a-z0-9]+")

# "Tokens *counted*" is not "tokens *held*": these names appear in the local
# model telemetry and describe sizes, so the classifier keeps their integers.
_NON_CREDENTIAL_KEYS = frozenset(
    {
        "inputtokens",
        "outputtokens",
        "maxtokens",
        "maxnewtokens",
        "prompttokencount",
        "evaltokencount",
        "tokencount",
        "tokencounts",
    }
)

_CREDENTIAL_KEY_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "passphrase",
        "password",
        "passwords",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "token",
    }
)
# Suffix matching catches keys whose prefix we cannot enumerate, such as
# ``github_token``, ``client_secret`` or ``X-Api-Key``.  Plurals are excluded so
# that honest counters (``input_tokens``, ``output_tokens``) keep their value.
_CREDENTIAL_KEY_SUFFIXES = (
    "apikey",
    "privatekey",
    "secretkey",
    "accesskey",
    "signingkey",
    "sshkey",
    "authorization",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "pwd",
)
_CREDENTIAL_COMPACT_KEYS = frozenset({"key", "keys", "auth", "cookie"})

_RAW_EVIDENCE_TOKENS = frozenset(
    {
        "camera",
        "cameras",
        "frame",
        "frames",
        "image",
        "images",
        "messages",
        "photo",
        "photos",
        "prompt",
        "prompts",
        "raw",
        "recording",
        "recordings",
        "snapshot",
        "snapshots",
        "video",
        "videos",
    }
)
_METADATA_TOKENS = frozenset(
    {
        "count",
        "counts",
        "digest",
        "hash",
        "id",
        "ids",
        "name",
        "names",
        "ref",
        "refs",
        "reference",
        "references",
        "revision",
        "schema",
        "sha256",
        "sha512",
        "state",
        "status",
        "type",
        "uri",
        "url",
        "version",
    }
)


def _header_replacement(match: re.Match[str]) -> str:
    return f"{match.group('header')}{match.group('separator')}{REDACTED}"


def _flag_replacement(match: re.Match[str]) -> str:
    return f"{match.group('flag')}{match.group('separator')}{REDACTED}"


_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_HEADER_VALUE = re.compile(
    r"(?i)(?P<header>proxy-authorization|authorization|set-cookie|cookie)(?P<separator>\s*[:=]\s*)(?P<value>[^\r\n]+)"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(?P<scheme>bearer|basic)\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})")
# Public and auditable on purpose: this tuple is the rule table, so a reviewer
# (or a test) can scan any file with exactly the definitions the redactor uses.
CREDENTIAL_FORMAT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bsk-[A-Za-z0-9_-]{16,}\b",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    )
)
_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/\s@]{1,128})@")
# Key detection is deliberately name-based: query parameters, CLI flags and
# arbitrary payload keys all consult the same classifier as mapping keys, so
# there is exactly one definition of a "credential-shaped name" to audit.
# The value class deliberately excludes '/', '?', '&' and ';'. A pair whose
# value could span those characters would swallow a following credential pair
# (as in "https://host?access_token=...") whenever the outer key is not itself
# credential-shaped, and the credential would then never be scanned.
_PAIR = re.compile(
    r"(?P<prefix>[?&;]|\b|--?)"
    r"(?P<key>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})"
    r"(?P<separator>=|:)"
    r"(?P<value>[^\s&;,\"'<>\\/?]{1,512})"
)
_FLAG = re.compile(r"(?P<flag>(?:^|\s)--?[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})(?P<separator>\s+)(?P<value>\S{1,512})")


def _pair_replacement(match: re.Match[str]) -> str:
    if not _is_credential_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}{match.group('key')}{match.group('separator')}{REDACTED}"


def _flag_replacement(match: re.Match[str]) -> str:
    if not _is_credential_key(match.group("flag").lstrip("-")):
        return match.group(0)
    return f"{match.group('flag')}{match.group('separator')}{REDACTED}"


def _header_replacement(match: re.Match[str]) -> str:
    return f"{match.group('header')}{match.group('separator')}{REDACTED}"


def _key_tokens(key: object) -> frozenset[str]:
    return frozenset(token for token in _KEY_SEPARATORS.split(str(key).casefold()) if token)


def _key_compact(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold())


def _is_credential_key(key: object) -> bool:
    """Decide whether a mapping key or name holds a credential rather than a count."""
    compact = _key_compact(key)
    if compact in _NON_CREDENTIAL_KEYS:
        return False
    if _key_tokens(key) & _CREDENTIAL_KEY_TOKENS:
        return True
    return compact in _CREDENTIAL_COMPACT_KEYS or compact.endswith(_CREDENTIAL_KEY_SUFFIXES)


def _is_raw_evidence_key(key: object) -> bool:
    tokens = _key_tokens(key)
    return bool(tokens & _RAW_EVIDENCE_TOKENS) and not tokens & _METADATA_TOKENS


def redact_text(text: str) -> str:
    """Scrub every known credential shape from one string.

    Text that matches nothing is returned unchanged; callers compare the result
    with the input to decide whether to record a redaction marker.
    """
    if not text:
        return text
    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTED, text)
    redacted = _HEADER_VALUE.sub(_header_replacement, redacted)
    redacted = _AUTH_SCHEME.sub(lambda match: f"{match.group('scheme')} {REDACTED}", redacted)
    for pattern in CREDENTIAL_FORMAT_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    redacted = _URL_USERINFO.sub(lambda match: f"{match.group('scheme')}{REDACTED}@", redacted)
    redacted = _PAIR.sub(_pair_replacement, redacted)
    return _FLAG.sub(_flag_replacement, redacted)


def redact_exception(exc: BaseException) -> str:
    """Return a loggable exception summary with no credential text in it."""
    return f"{type(exc).__name__}: {redact_text(str(exc))}"


def _redact(value: Any, findings: list[int], *, credential_scope: bool = False) -> Any:
    """Sanitize ``value``, counting every substitution in ``findings[0]``.

    Inside a credential scope the *value* is untrusted regardless of its own key
    names, so every scalar below a credential-named key is replaced while the
    surrounding structure is kept.  Nested keys are still sanitized outside that
    scope, which is how ``{"auth": {"github_token": ...}}`` loses the token
    without losing the fact that an ``auth`` block was present.
    """
    if isinstance(value, str):
        if credential_scope:
            findings[0] += 1
            return REDACTED
        redacted = redact_text(value)
        if redacted != value:
            findings[0] += 1
        return redacted
    if isinstance(value, bytes | bytearray | memoryview):
        # Raw bytes are exactly the payload that must stay in the operator-owned
        # store; an artifact keeps the reference, never the content.
        findings[0] += 1
        return EVIDENCE_REDACTED
    if isinstance(value, Mapping):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_credential_key(key):
                sanitized[key] = _redact(item, findings, credential_scope=True)
            elif _is_raw_evidence_key(key):
                findings[0] += 1
                sanitized[key] = EVIDENCE_REDACTED
            else:
                sanitized[key] = _redact(item, findings, credential_scope=credential_scope)
        return sanitized
    if isinstance(value, list):
        return [_redact(item, findings, credential_scope=credential_scope) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, findings, credential_scope=credential_scope) for item in value)
    if credential_scope and value is not None:
        # A non-string scalar under a credential key (a numeric PIN, a flag) is
        # still credential material.
        findings[0] += 1
        return REDACTED
    return value


def redact_value(value: Any) -> Any:
    """Return a sanitized deep copy of a JSON-shaped value, never a mutation."""
    return _redact(value, [0])


def redact_mapping(mapping: Mapping[Any, Any]) -> tuple[dict[Any, Any], int]:
    """Return ``(sanitized copy, redacted value count)`` for one artifact record.

    Identifier fields are copied verbatim so the artifact stays joinable to its
    run; every other string is scanned, including nested mappings and lists.
    """
    findings = [0]
    sanitized: dict[Any, Any] = {}
    for key, item in mapping.items():
        if isinstance(key, str) and key.casefold() in IDENTIFIER_FIELDS:
            sanitized[key] = item
        elif _is_credential_key(key):
            sanitized[key] = _redact(item, findings, credential_scope=True)
        elif _is_raw_evidence_key(key):
            findings[0] += 1
            sanitized[key] = EVIDENCE_REDACTED
        else:
            sanitized[key] = _redact(item, findings)
    return sanitized, findings[0]
