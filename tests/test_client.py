import io
import json
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from jev_skills import client


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status
        self.read_sizes = []

    def read(self, size: int | None = -1):
        self.read_sizes.append(size)
        return super().read(size)


class ClientTests(unittest.TestCase):
    def test_single_fixed_https_post_with_json_and_bearer_auth(self):
        payload = {"model": "jev-latest", "state": {"user_request": "vidéo"}, "questions": {}}
        expected = {"model": "fixture", "answers": {}}
        response = FakeResponse(json.dumps(expected).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            result = client.request_evaluation(payload, "fixture-key", 1.25)
        self.assertEqual(result, expected)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 1.25})
        self.assertTrue(response.closed)
        self.assertEqual(response.read_sizes, [client.MAX_RESPONSE_BYTES + 1])

    def test_http_errors_are_sanitized_without_body_read_or_retry(self):
        body = FakeResponse(b"secret-key and private request")
        error = HTTPError("https://api.typesafe.ai/v1/systemone", 429, "secret-key", Message(), body)
        opener = Mock()
        opener.open.side_effect = error
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(client.EvaluationError) as caught:
                client.request_evaluation({}, "fixture-key", 1)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertNotIn("private request", str(caught.exception))
        self.assertEqual(body.read_sizes, [])
        self.assertTrue(body.closed)
        opener.open.assert_called_once()

    def test_http_error_cleanup_cannot_leak_private_exception(self):
        body = FakeResponse(b"private response")
        error = HTTPError(client.ENDPOINT, 401, "private reason", Message(), body)
        opener = Mock()
        opener.open.side_effect = error
        try:
            with patch.object(body, "close", side_effect=OSError("secret-key private request")):
                with patch("urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(client.EvaluationError) as caught:
                        client.request_evaluation({}, "fixture-key", 1)
            self.assertEqual(caught.exception.code, "authentication_error")
            self.assertNotIn("secret-key", str(caught.exception))
            self.assertNotIn("private request", str(caught.exception))
            opener.open.assert_called_once()
        finally:
            body.close()

    def test_malformed_json_is_rejected_not_coerced(self):
        bodies = [
            b"", b"not JSON: secret-key private request", b"[]", b"null", b"false",
            b"\xff", b'{"model": "first", "model": "second"}',
            b'{"answers": {"x": {"noul": 0.1, "noul": 0.9}}}',
            b'{"value": NaN}', b'{"value": Infinity}', b'{"value": -Infinity}',
            b'{"value": 1e999}', b'{"value": -1e999}', b'{}{}',
            b'{"deep":' + b'[' * 1500 + b']' * 1500 + b'}',
        ]
        for body in bodies:
            with self.subTest(body=body[:80]):
                response = FakeResponse(body)
                opener = Mock()
                opener.open.return_value = response
                with patch("urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(client.EvaluationError) as caught:
                        client.request_evaluation({}, "fixture-key", 1)
                self.assertEqual(caught.exception.code, "protocol_error")
                self.assertNotIn("secret-key", str(caught.exception))
                self.assertNotIn("private request", str(caught.exception))
                self.assertTrue(response.closed)
                opener.open.assert_called_once()

    def test_exact_response_limit_and_one_byte_over(self):
        for size in (client.MAX_RESPONSE_BYTES, client.MAX_RESPONSE_BYTES + 1):
            with self.subTest(size=size):
                response = FakeResponse(b'{}' + b' ' * (size - 2))
                opener = Mock()
                opener.open.return_value = response
                with patch("urllib.request.build_opener", return_value=opener):
                    if size == client.MAX_RESPONSE_BYTES:
                        self.assertEqual(client.request_evaluation({}, "fixture-key", 1), {})
                    else:
                        with self.assertRaises(client.EvaluationError) as caught:
                            client.request_evaluation({}, "fixture-key", 1)
                        self.assertEqual(caught.exception.code, "response_too_large")
                self.assertTrue(response.closed)
                self.assertEqual(response.read_sizes, [client.MAX_RESPONSE_BYTES + 1])
                opener.open.assert_called_once()

    def test_redirects_use_real_urllib_handlers_without_sending_credentials_again(self):
        from email.message import Message
        import urllib.request
        from urllib.response import addinfourl

        for status in (301, 302, 303, 307, 308):
            for location in ("https://elsewhere.invalid/steal", "http://elsewhere.invalid/steal",
                             "/same-origin"):
                with self.subTest(status=status, location=location):
                    body = FakeResponse(b"private redirect response")
                    headers = Message()
                    headers["Location"] = location
                    response = addinfourl(body, headers, client.ENDPOINT, status)
                    setattr(response, "msg", "fixture redirect")
                    with patch.object(urllib.request.HTTPSHandler, "https_open", return_value=response) as send:
                        with patch.object(urllib.request.HTTPHandler, "http_open") as insecure_send:
                            with self.assertRaises(client.EvaluationError) as caught:
                                client.request_evaluation({}, "fixture-key", 1)
                    self.assertEqual(caught.exception.code, "redirect_blocked")
                    send.assert_called_once()
                    insecure_send.assert_not_called()
                    self.assertEqual(body.read_sizes, [])
                    self.assertTrue(body.closed)

    def test_all_http_failures_are_single_attempt_and_safe(self):
        from email.message import Message
        codes = {300: "redirect_blocked", 304: "redirect_blocked", 401: "authentication_error",
                 403: "authentication_error", 422: "http_error", 429: "rate_limited",
                 500: "http_error", 529: "http_error"}
        for status, code in codes.items():
            for raises in (True, False):
                with self.subTest(status=status, raises=raises):
                    body = FakeResponse(b"private request secret-key", status=status)
                    opener = Mock()
                    if raises:
                        opener.open.side_effect = HTTPError(client.ENDPOINT, status, "secret-key", Message(), body)
                    else:
                        opener.open.return_value = body
                    with patch("urllib.request.build_opener", return_value=opener):
                        with self.assertRaises(client.EvaluationError) as caught:
                            client.request_evaluation({}, "fixture-key", 1)
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn("secret-key", str(caught.exception))
                    self.assertNotIn("private request", str(caught.exception))
                    self.assertEqual(body.read_sizes, [])
                    self.assertTrue(body.closed)
                    opener.open.assert_called_once()

    def test_network_and_read_errors_are_sanitized_and_never_retried(self):
        from urllib.error import URLError
        from http.client import IncompleteRead
        errors = [
            (TimeoutError("secret-key private request"), "timeout"),
            (URLError(TimeoutError("secret-key private request")), "timeout"),
            (URLError("secret-key private request"), "transport_error"),
            (OSError("secret-key private request"), "transport_error"),
            (IncompleteRead(b"secret-key private request", 20), "transport_error"),
        ]
        for error, code in errors:
            for on_read in (False, True):
                with self.subTest(code=code, on_read=on_read):
                    opener = Mock()
                    response = FakeResponse(b"{}")
                    if on_read:
                        response.read = Mock(side_effect=error)
                        opener.open.return_value = response
                    else:
                        opener.open.side_effect = error
                    with patch("urllib.request.build_opener", return_value=opener):
                        with self.assertRaises(client.EvaluationError) as caught:
                            client.request_evaluation({}, "fixture-key", 1)
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn("secret-key", str(caught.exception))
                    self.assertNotIn("private request", str(caught.exception))
                    self.assertTrue(caught.exception.__suppress_context__)
                    if on_read:
                        self.assertTrue(response.closed)
                    else:
                        response.close()
                    opener.open.assert_called_once()

    def test_invalid_local_values_cannot_construct_an_opener(self):
        cases = [
            ([], "key", 1), ({"bad": object()}, "key", 1),
            ({"bad": float("nan")}, "key", 1), ({1: "bad"}, "key", 1),
            ({"bad": (1, 2)}, "key", 1), ({"bad": "\ud800"}, "key", 1),
            ({}, "", 1), ({}, "key\r\nHeader: injected", 1), ({}, "kéy", 1),
            ({}, "key", True), ({}, "key", 0), ({}, "key", -1),
            ({}, "key", float("nan")), ({}, "key", float("inf")),
        ]
        for payload, key, timeout in cases:
            with self.subTest(payload=payload, key=key, timeout=timeout):
                with patch("urllib.request.build_opener") as build:
                    with self.assertRaises(client.EvaluationError) as caught:
                        client.request_evaluation(payload, key, timeout)
                self.assertEqual(caught.exception.code, "invalid_input")
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
