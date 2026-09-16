"""One-attempt stdlib transport for the fixed TypeSafe HTTPS endpoint.

``timeout`` is a finite socket-operation timeout, not an end-to-end deadline
(DNS, multiple reads, and OS scheduling may exceed it). No retries or redirects.
"""

import json
import math
import urllib.error
import urllib.request


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ERROR_MESSAGES = {
    "invalid_input": "Invalid ranking request or connection settings.",
    "request_too_large": "Ranking request exceeds the configured byte limit.",
    "protocol_error": "Evaluation response does not match the expected protocol.",
    "response_too_large": "Evaluation response exceeds the byte limit.",
    "redirect_blocked": "Evaluation endpoint redirect was blocked.",
    "authentication_error": "Evaluation authentication was rejected.",
    "rate_limited": "Evaluation service rate limit was reached; no retry was made.",
    "http_error": "Evaluation service returned an unsuccessful HTTP status.",
    "timeout": "Evaluation request timed out; no retry was made.",
    "transport_error": "Evaluation request failed; no retry was made.",
}


class EvaluationError(Exception):
    """An error code and static message, never remote body/header/exception text."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_MESSAGES else "transport_error"
        super().__init__(_ERROR_MESSAGES[self.code])


def validate_connection(api_key: str, timeout: float) -> None:
    if not isinstance(api_key, str) or not api_key or any(
        not 33 <= ord(char) <= 126 for char in api_key
    ):
        raise EvaluationError("invalid_input")
    validate_timeout(timeout)


def validate_timeout(timeout: float) -> None:
    try:
        valid_timeout = (
            type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
        )
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise EvaluationError("invalid_input")


def _check_json(value) -> None:
    """Reject implicit JSON coercions, nonfinite numbers, and non-JSON types."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError
            _check_json(item)
    elif isinstance(value, list):
        for item in value:
            _check_json(item)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError
    elif value is not None and type(value) not in (str, int, bool):
        raise ValueError


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def encode_payload(payload: dict) -> bytes:
    """Use the same JSON bytes for the engine's size gate and the wire."""
    try:
        if not isinstance(payload, dict):
            raise ValueError
        _check_json(payload)
        return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise EvaluationError("invalid_input") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_error(code: int) -> EvaluationError:
    if 300 <= code < 400:
        return EvaluationError("redirect_blocked")
    if code in (401, 403):
        return EvaluationError("authentication_error")
    if code == 429:
        return EvaluationError("rate_limited")
    return EvaluationError("http_error")


def request_evaluation(payload: dict, api_key: str, timeout: float) -> dict:
    """POST once; close responses, bound reads, and expose only safe errors.

    This transport checks JSON framing; the engine checks the Noul schema.
    No environment key lookup or endpoint override is performed.
    """
    validate_connection(api_key, timeout)
    body = encode_payload(payload)
    try:
        request = urllib.request.Request(
            ENDPOINT, data=body, method="POST",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json", "Accept": "application/json"},
        )
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise _http_error(response.status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise EvaluationError("response_too_large")
    except urllib.error.HTTPError as exc:
        try:
            exc.close()  # Never read an error body, even during cleanup.
        except Exception:
            pass  # Cleanup failures must not replace the sanitized HTTP error.
        raise _http_error(exc.code) from None
    except TimeoutError:
        raise EvaluationError("timeout") from None
    except urllib.error.URLError as exc:
        code = "timeout" if isinstance(exc.reason, TimeoutError) else "transport_error"
        raise EvaluationError(code) from None
    except EvaluationError:
        raise
    except Exception:
        raise EvaluationError("transport_error") from None
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(result, dict):
            raise ValueError
        _check_json(result)
        return result
    except (ValueError, RecursionError):
        raise EvaluationError("protocol_error") from None
