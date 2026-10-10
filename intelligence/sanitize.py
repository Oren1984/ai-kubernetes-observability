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


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


def neutralize_for_llm(text: str) -> str:
    """What an LLM is allowed to see of an untrusted string."""
    return INJECTION_PLACEHOLDER if looks_like_injection(text) else sanitize_text(text)
