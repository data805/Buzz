import json

from buzzbench.streaming import chunk_has_output, sse_data


def chunk(delta):
    return json.dumps({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta}]})


def test_sse_data_lines():
    assert sse_data('data: {"a":1}') == '{"a":1}'
    assert sse_data("data:[DONE]") == "[DONE]"
    assert sse_data("") is None
    assert sse_data(": keep-alive") is None
    assert sse_data("event: x") is None


def test_role_only_chunk_is_not_a_token():
    assert not chunk_has_output(chunk({"role": "assistant", "content": ""}))


def test_content_and_tool_calls_count():
    assert chunk_has_output(chunk({"content": "Hello"}))
    assert chunk_has_output(chunk({"tool_calls": [{"index": 0, "function": {"name": "opt_out"}}]}))


def test_done_usage_and_garbage_do_not_count():
    assert not chunk_has_output("[DONE]")
    assert not chunk_has_output(json.dumps({"choices": [], "usage": {"completion_tokens": 3}}))
    assert not chunk_has_output("not json")
