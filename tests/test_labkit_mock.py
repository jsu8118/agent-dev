"""Tests for labkit's mock Claude API: it must behave like the real API where it matters.

These double as documentation of the API rules the course teaches.
"""

import asyncio

import anthropic
import pytest
from anthropic.types.messages.batch_create_params import Request
from pydantic import BaseModel

from labkit import LEDGER, get_async_client, get_client, mock_api, text_of
from labkit.mock.cache import PromptCache
from labkit.models import get_spec

OPUS = "claude-opus-5"
HAIKU = "claude-haiku-4-5"
TOOLS = [{"name": "get_weather", "description": "Get the weather for a city.",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]


@pytest.fixture
def client():
    return get_client()


def _tool_turn(client):
    first = client.messages.create(model=OPUS, max_tokens=2000, tools=TOOLS,
                                   tool_choice={"type": "tool", "name": "get_weather"},
                                   messages=[{"role": "user", "content": "Weather in Oslo?"}])
    tool_use = next(b for b in first.content if b.type == "tool_use")
    return first, tool_use


def test_basic_message_shape(client):
    msg = client.messages.create(model=OPUS, max_tokens=2000, messages=[{"role": "user", "content": "hi"}])
    assert msg.type == "message" and msg.role == "assistant" and msg.stop_reason == "end_turn"
    assert text_of(msg)
    assert msg.usage.input_tokens > 0 and msg.usage.output_tokens > 0


def test_structured_output_parse(client):
    class Ticket(BaseModel):
        category: str
        priority: int
        email: str

    result = client.messages.parse(model=OPUS, max_tokens=2000, output_format=Ticket,
                                   messages=[{"role": "user", "content": "From ann@ex.com: invoice wrong"}])
    assert isinstance(result.parsed_output, Ticket)
    assert result.parsed_output.email == "ann@ex.com"


def test_streaming_matches_final_message(client):
    with client.messages.stream(model=OPUS, max_tokens=2000,
                                messages=[{"role": "user", "content": "stream please"}]) as stream:
        streamed = "".join(stream.text_stream)
        final = stream.get_final_message()
    assert streamed == text_of(final)
    assert LEDGER.total_calls == 1          # streamed calls are metered too


def test_tool_roundtrip_and_thinking_preserved(client):
    first, tool_use = _tool_turn(client)
    assert first.stop_reason == "tool_use"
    assert first.content[0].type == "thinking"         # Opus 5 thinks by default
    follow = client.messages.create(model=OPUS, max_tokens=2000, tools=TOOLS, messages=[
        {"role": "user", "content": "Weather in Oslo?"},
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": "4C, rain"}]},
    ])
    assert "4C, rain" in text_of(follow)


@pytest.mark.parametrize("label, kwargs", [
    ("sampling params removed", dict(model=OPUS, extra_body={"temperature": 0})),
    ("budget_tokens removed", dict(model=OPUS, thinking={"type": "enabled", "budget_tokens": 2048})),
    ("disabled thinking above high effort", dict(model=OPUS, thinking={"type": "disabled"},
                                                 output_config={"effort": "max"})),
    ("no effort on haiku", dict(model=HAIKU, output_config={"effort": "low"})),
    ("no adaptive on haiku", dict(model=HAIKU, thinking={"type": "adaptive"})),
    ("schema objects must be closed", dict(model=OPUS, output_config={"format": {"type": "json_schema", "schema": {
        "type": "object", "properties": {"a": {"type": "string"}}}}})),
    ("fallbacks need their beta header", dict(model=OPUS, extra_body={"fallbacks": "default"})),
    ("tool_choice needs tools", dict(model=OPUS, tool_choice={"type": "auto"})),
])
def test_invalid_requests_are_rejected(client, label, kwargs):
    with pytest.raises(anthropic.BadRequestError):
        client.messages.create(max_tokens=500, messages=[{"role": "user", "content": "hi"}], **kwargs)


def test_prefill_rejected_on_opus5_but_allowed_on_haiku(client):
    convo = [{"role": "user", "content": "Give JSON"}, {"role": "assistant", "content": "{"}]
    with pytest.raises(anthropic.BadRequestError):
        client.messages.create(model=OPUS, max_tokens=500, messages=convo)
    assert client.messages.create(model=HAIKU, max_tokens=500, messages=convo).stop_reason


def test_unknown_model_is_404(client):
    with pytest.raises(anthropic.NotFoundError):
        client.messages.create(model="claude-not-a-model", max_tokens=10, messages=[{"role": "user", "content": "x"}])


def test_tool_pairing_rules(client):
    first, tool_use = _tool_turn(client)
    user = {"role": "user", "content": "Weather in Oslo?"}
    assistant = {"role": "assistant", "content": first.content}
    with pytest.raises(anthropic.BadRequestError, match="without `tool_result`"):
        client.messages.create(model=OPUS, max_tokens=500, tools=TOOLS,
                               messages=[user, assistant, {"role": "user", "content": "thanks"}])
    with pytest.raises(anthropic.BadRequestError, match="must come first"):
        client.messages.create(model=OPUS, max_tokens=500, tools=TOOLS, messages=[user, assistant, {
            "role": "user", "content": [{"type": "text", "text": "result:"},
                                        {"type": "tool_result", "tool_use_id": tool_use.id, "content": "x"}]}])
    stripped = {"role": "assistant", "content": [b for b in first.content if b.type != "thinking"]}
    with pytest.raises(anthropic.BadRequestError, match="thinking"):
        client.messages.create(model=OPUS, max_tokens=500, tools=TOOLS, messages=[user, stripped, {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": "x"}]}])


def test_edited_thinking_block_is_rejected(client):
    first, tool_use = _tool_turn(client)
    edited = [b.model_dump() for b in first.content]
    edited[0]["thinking"] = "a different chain of thought"
    with pytest.raises(anthropic.BadRequestError, match="signature"):
        client.messages.create(model=OPUS, max_tokens=500, tools=TOOLS, messages=[
            {"role": "user", "content": "Weather in Oslo?"}, {"role": "assistant", "content": edited},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": "x"}]}])


def test_prompt_cache_prefix_semantics(client):
    system_text = "You are Kestrel's support assistant. Follow the policies below.\n" + ("Policy line. " * 400)
    def ask(prefix: str, question: str):
        return client.messages.create(model=OPUS, max_tokens=2000, messages=[{"role": "user", "content": question}],
                                      system=[{"type": "text", "text": prefix + system_text,
                                               "cache_control": {"type": "ephemeral"}}])
    first = ask("", "q1")
    second = ask("", "q2")
    changed = ask("Current time 09:14. ", "q3")
    assert first.usage.cache_creation_input_tokens > 0 and first.usage.cache_read_input_tokens == 0
    assert second.usage.cache_read_input_tokens == first.usage.cache_creation_input_tokens
    assert changed.usage.cache_read_input_tokens == 0          # any earlier byte change = miss


def test_cache_minimum_length_is_model_specific():
    cache = PromptCache()
    body = {"model": HAIKU, "messages": [{"role": "user", "content": "hi"}],
            "system": [{"type": "text", "text": "short system prompt " * 100, "cache_control": {"type": "ephemeral"}}]}
    assert cache.process(body, get_spec(HAIKU)).write_tokens == 0          # < 4096 tokens: silently not cached
    assert cache.process({**body, "model": OPUS}, get_spec(OPUS)).write_tokens > 0   # >= 512 tokens on Opus 5


def test_retries_on_injected_faults(client):
    mock_api().inject_faults(429, 529)
    assert client.messages.create(model=OPUS, max_tokens=2000, messages=[{"role": "user", "content": "x"}])
    mock_api().inject_faults(529, 529, 529)
    with pytest.raises(anthropic.OverloadedError):       # 529 -> OverloadedError (an APIStatusError)
        client.with_options(max_retries=1).messages.create(model=OPUS, max_tokens=500,
                                                            messages=[{"role": "user", "content": "x"}])


def test_refusal_and_server_side_fallback(client):
    prompt = [{"role": "user", "content": "[simulate:refusal] audit our firewall rules"}]
    refused = client.messages.create(model=OPUS, max_tokens=1000, messages=prompt)
    assert refused.stop_reason == "refusal" and refused.content == []
    rescued = client.beta.messages.create(model=OPUS, max_tokens=1000, messages=prompt,
                                          betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    assert rescued.stop_reason != "refusal"
    assert rescued.content[0].type == "fallback"
    assert any(it.type == "fallback_message" for it in rescued.usage.iterations)


def test_max_tokens_includes_thinking(client):
    msg = client.messages.create(model=OPUS, max_tokens=60, output_config={"effort": "max"},
                                 messages=[{"role": "user", "content": "Write a detailed analysis."}])
    assert msg.stop_reason == "max_tokens"


def test_batches_lifecycle(client):
    batch = client.messages.batches.create(requests=[
        Request(custom_id=f"t{i}", params={"model": HAIKU, "max_tokens": 200,
                                           "messages": [{"role": "user", "content": f"ticket {i}"}]})
        for i in range(4)])
    assert batch.processing_status == "in_progress"
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        pass
    results = {r.custom_id: r.result.type for r in client.messages.batches.results(batch.id)}
    assert results == {f"t{i}": "succeeded" for i in range(4)}


def test_tool_runner_executes_tools(client):
    calls = []

    @anthropic.beta_tool
    def get_weather(city: str) -> str:
        """Get the weather for a city.

        Args:
            city: City name.
        """
        calls.append(city)
        return "sunny"

    runner = client.beta.messages.tool_runner(model=OPUS, max_tokens=2000, tools=[get_weather],
                                              tool_choice={"type": "any"}, max_iterations=4,
                                              messages=[{"role": "user", "content": "Weather in Rome?"}])
    final = runner.until_done()
    assert calls and final.stop_reason == "end_turn"


def test_async_client_parallel_calls():
    async def run():
        client = get_async_client()
        return await asyncio.gather(*[
            client.messages.create(model=OPUS, max_tokens=2000, messages=[{"role": "user", "content": f"q{i}"}])
            for i in range(5)])
    results = asyncio.run(run())
    assert len(results) == 5 and all(r.stop_reason == "end_turn" for r in results)


def test_count_tokens_and_models(client):
    small = client.messages.count_tokens(model=OPUS, messages=[{"role": "user", "content": "hello"}]).input_tokens
    big = client.messages.count_tokens(model=OPUS, messages=[{"role": "user", "content": "hello " * 500}]).input_tokens
    assert big > small > 0
    assert client.models.retrieve(HAIKU).max_input_tokens == 200_000
