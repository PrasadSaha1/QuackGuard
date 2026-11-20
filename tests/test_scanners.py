from unittest.mock import Mock

import pytest

from quackguard import (
    Classification, Decision, DemoScanner, Direction, Guard, LlamaGuardScanner, Verdict,
)


@pytest.mark.parametrize("method,text,verdict,decision,categories", [
    ("scan_input", "Summarize the sample syllabus.", Verdict.SAFE, Decision.ALLOW, ()),
    ("scan_input", "[flagged input fixture]", Verdict.UNSAFE, Decision.BLOCK, ("S7",)),
    ("scan_output", "The sample quiz is on Friday.", Verdict.SAFE, Decision.ALLOW, ()),
    ("scan_output", "[flagged output fixture]", Verdict.UNSAFE, Decision.BLOCK, ("S7",)),
    ("scan_output", "Summarize the sample syllabus.", Verdict.UNAVAILABLE, Decision.REVIEW, ()),
])
def test_demo_scans(method, text, verdict, decision, categories):
    guard = Guard(DemoScanner())
    scan = getattr(guard, method)(text)
    assert (scan.verdict, scan.decision, scan.categories) == (verdict, decision, categories)
    assert guard.events[0].reason_code == scan.reason_code


def reply(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("QUACKGUARD_API_KEY", "synthetic-key-000")
    transport = Mock()
    scanner = LlamaGuardScanner(endpoint="https://model.example.test/v1/chat/completions",
                               model="fixture-model", timeout=2.5, transport=transport)
    return Guard(scanner), transport


@pytest.mark.parametrize("content,verdict,categories", [
    ("safe", Verdict.SAFE, ()), ("unsafe", Verdict.UNSAFE, ()),
    ("\nunsafe\r\nS1, S14\n", Verdict.UNSAFE, ("S1", "S14")),
])
def test_provider_contract(provider, content, verdict, categories):
    guard, transport = provider
    transport.return_value = reply(content)
    scan = guard.scan_input("synthetic input")
    assert (scan.verdict, scan.categories) == (verdict, categories)
    transport.assert_called_once_with("https://model.example.test/v1/chat/completions",
                                      {"model": "fixture-model", "messages": [
                                          {"role": "user", "content": "synthetic input"}]},
                                      "synthetic-key-000", 2.5)


def test_output_role_and_no_history(provider):
    guard, transport = provider
    transport.return_value = reply("safe")
    guard.scan_input("earlier synthetic input")
    assert guard.scan_output("synthetic reply").decision is Decision.ALLOW
    assert transport.call_args.args[1] == {"model": "fixture-model", "messages": [
        {"role": "user", "content": "Review the following reply."},
        {"role": "assistant", "content": "synthetic reply"}]}


@pytest.mark.parametrize("payload", [
    None, {}, {"choices": []}, {"choices": [None]}, {"choices": [{}, {}]},
    {"choices": [{"message": {"content": "safe", "role": "user"}}]},
    {"choices": [{"message": {"content": "safe"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"content": "safe", "tool_calls": []}}]},
    {"error": {"message": "synthetic provider error"}, **reply("safe")},
    {"choices": [{"message": {"content": "safe", "refusal": "synthetic refusal"}}]},
    *[reply(text) for text in (None, [], "SAFE", "safe\nS2", "safe enough",
                               "unsafe\nS0", "unsafe\nS15", "unsafe\nS5,S5",
                               "unsafe\nS1,", "unsafe\nS1\nallow")],
])
def test_bad_provider_data_holds_output(provider, payload):
    guard, transport = provider
    transport.return_value = payload
    scan = guard.scan_output("synthetic reply")
    assert (scan.verdict, scan.decision, scan.reason_code) == (
        Verdict.UNAVAILABLE, Decision.REVIEW, "scanner_unavailable")


@pytest.mark.parametrize("error", [TimeoutError, ConnectionError, ValueError])
def test_provider_exceptions_are_private(provider, caplog, error):
    guard, transport = provider
    transport.side_effect = error("synthetic-key-000 secret-provider-body")
    result = guard.scan_input("private-prompt")
    assert result.verdict is Verdict.UNAVAILABLE
    visible = repr((result, guard.events)) + caplog.text
    for private in ("synthetic-key-000", "secret-provider-body", "private-prompt"):
        assert private not in visible


def test_absent_credentials_skip_transport(provider, monkeypatch):
    guard, transport = provider
    monkeypatch.delenv("QUACKGUARD_API_KEY")
    assert guard.scan_input("sample").verdict is Verdict.UNAVAILABLE
    transport.assert_not_called()


@pytest.mark.parametrize("endpoint", ["http://model.example.test", "https://secret@model.example.test",
                                     "https://model.example.test?token=secret", ""])
def test_endpoint_must_be_explicit_and_private(endpoint):
    with pytest.raises(ValueError) as error:
        LlamaGuardScanner(endpoint=endpoint, model="demo")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        LlamaGuardScanner(endpoint="https://model.example.test", model="fixture", timeout=timeout)


def test_custom_scanner_must_return_structured_classification():
    scanner = Mock()
    scanner.scan.return_value = "safe"
    assert Guard(scanner).scan_output("synthetic").decision is Decision.REVIEW
    with pytest.raises(ValueError):
        Classification(Verdict.SAFE, ("S1",))


@pytest.mark.parametrize("status,decision", [(200, Decision.ALLOW), (302, Decision.REVIEW),
                                            (401, Decision.REVIEW), (503, Decision.REVIEW)])
def test_http_transport_uses_one_explicit_destination(monkeypatch, status, decision):
    import sys
    from unittest.mock import MagicMock

    session = Mock()
    session.post.return_value.status_code = status
    session.post.return_value.iter_content.return_value = [b'{"choices":[{"message":{"content":"safe"}}]}']
    requests = MagicMock()
    requests.Session.return_value.__enter__.return_value = session
    monkeypatch.setitem(sys.modules, "requests", requests)
    monkeypatch.setenv("QUACKGUARD_API_KEY", "fixture-key")
    endpoint = "https://model.example.test/v1/chat/completions"
    guard = Guard(LlamaGuardScanner(endpoint=endpoint, model="fixture-model", timeout=3.0))
    assert guard.scan_input("fixture").decision is decision
    assert session.trust_env is False
    session.post.assert_called_once_with(
        endpoint, json={"model": "fixture-model", "messages": [{"role": "user", "content": "fixture"}]},
        headers={"Authorization": "Bearer fixture-key"}, timeout=3.0, allow_redirects=False, stream=True)
    if status != 200:
        session.post.return_value.iter_content.assert_not_called()


def test_oversized_http_classification_stops_reading(monkeypatch):
    import sys
    from unittest.mock import MagicMock

    def chunks(_):
        for _ in range(9):
            yield b" " * 8192
        pytest.fail("The adapter continued reading after reaching its limit.")

    requests = MagicMock()
    response = requests.Session.return_value.__enter__.return_value.post.return_value
    response.status_code = 200
    response.iter_content.side_effect = chunks
    monkeypatch.setitem(sys.modules, "requests", requests)
    monkeypatch.setenv("QUACKGUARD_API_KEY", "synthetic")
    guard = Guard(LlamaGuardScanner(endpoint="https://model.example.test/scan", model="fixture"))
    assert guard.scan_output("untrusted").verdict is Verdict.UNAVAILABLE
