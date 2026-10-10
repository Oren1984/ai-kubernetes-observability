"""Handling of untrusted text (log lines, exception messages, event messages).

Telemetry content is written by the observed workload, so it may contain
secrets or text crafted to steer an LLM. It is always treated as data.
"""
import re

MAX_TEXT_LEN = 300

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SECRET = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)(\S+)"
)
_INJECTION = re.compile(
    r"(?i)(ignore (all |any |the )?(previous|prior|above) (instructions|rules|prompts?)"
    r"|disregard (all |the )?(previous|prior|above)"
    r"|you are now\b|new instructions?:|system prompt|</?(system|assistant|user)>"
    r"|\b(system|assistant)\s*:"
    r"|\bact as\b|\bjailbreak\b"
    r"|\b(run|execute|invoke)\b.{0,40}\b(kubectl|terraform|docker|shell|bash|powershell)\b"
    r"|approve (all|every|this) recommendation|mark .{0,30} as (approved|resolved))"
)

INJECTION_PLACEHOLDER = "[removed: suspected instruction injection in telemetry text]"


def sanitize_text(text: str, max_len: int = MAX_TEXT_LEN) -> str:
    """Strip control characters, mask credential-looking values, bound length."""
    text = _CONTROL_CHARS.sub(" ", str(text))
    text = _SECRET.sub(r"\1\2[REDACTED]", text)
    return text if len(text) <= max_len else text[:max_len] + "…[truncated]"


_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_.:/ -]")
_TRACE_ID = re.compile(r"^[0-9a-f]{16,32}$")
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]()<>#!|~$])")


def safe_label(value, max_len: int = 80) -> str:
    """Identifier-like telemetry fields (span names, exception types, event
    reasons, log `event` attributes) are also written by the workload. They are
    reduced to a plain character set so they cannot carry prose, markup or
    query syntax into observations, evidence ids or backend queries."""
    return _LABEL_UNSAFE.sub("_", str(value))[:max_len]


def is_trace_id(value) -> bool:
    return isinstance(value, str) and bool(_TRACE_ID.match(value))


def escape_markdown(text) -> str:
    """Render text literally in a Markdown view: no links, images or formatting."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(text))


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


def neutralize_for_llm(text: str, max_len: int = MAX_TEXT_LEN) -> str:
    """What an LLM is allowed to see of an untrusted string."""
    return INJECTION_PLACEHOLDER if looks_like_injection(text) else sanitize_text(text, max_len)
