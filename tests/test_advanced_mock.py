"""Tests for the mock surfaces the advanced course relies on: tool search, deferred loading, inline tool
changes, code execution and programmatic tool calling, the Files API, per-message effort, clear_at,
task budgets, preserved thinking (model and prefix binding), cache concurrency and pre-warming, and the
Managed Agents endpoints.  As in test_labkit_mock.py, each test documents an API rule."""

from __future__ import annotations

import json
import threading

import anthropic
import pytest

from labkit import get_client, mock_api
from labkit.mock import say, scenario, tool, use_tools
from labkit.mock.reply import Reply, run_code, search_tools
from labkit.mock.request import MockRequest

OPUS = "claude-opus-5"
FABLE = "claude-fable-5-1"


def _tool(name: str, description: str, defer: bool = False, **extra) -> dict:
    d = {"name": name, "description": description,
         "input_schema": {"type": "object", "properties": {"order_id": {"type": "string", "description": "Order id"}},
                          "required": ["order_id"]}}
    if defer:
        d["defer_loading"] = True
    d.update(extra)
    return d


CATALOG = [_tool("get_order", "Get one sales order by id"),
           _tool("get_shipment_tracking", "Carrier tracking and ETA for a shipped order", defer=True),
           _tool("get_invoice_balance", "Open balance of an invoice", defer=True),
           _tool("book_field_service", "Book a field-service engineer visit", defer=True)]
SEARCH = {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}
BM25 = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
CODE = {"type": "code_execution_20260120", "name": "code_execution"}


@pytest.fixture
def client():
    mock_api().reset()
    return get_client()


# ----------------------------------------------------------------------------- tool search
@scenario("test.tool_search", match=lambda r: "<test_tool_search>" in r.system_text, priority=50)
def _tool_search_policy(req: MockRequest):
    if not req.tool_search_queries:
        return Reply(content=[search_tools("track|eta")])
    if not req.called("get_shipment_tracking"):
        return use_tools(tool("get_shipment_tracking", order_id="SO-10303"))
    return say("Tracking " + (req.calls("get_shipment_tracking")[0].result or ""))


def test_tool_search_discovers_deferred_tools_and_skips_their_tokens(client):
    plain = client.beta.messages.count_tokens(model=OPUS, tools=CATALOG + [SEARCH],
                                              messages=[{"role": "user", "content": "hi"}]).input_tokens
    loaded = client.beta.messages.count_tokens(model=OPUS, tools=[dict(t, defer_loading=False) for t in CATALOG] + [SEARCH],
                                               messages=[{"role": "user", "content": "hi"}]).input_tokens
    assert plain < loaded, "deferred tools must not cost prompt tokens until discovered"
    first = client.beta.messages.create(model=OPUS, max_tokens=2000, system="<test_tool_search>", tools=CATALOG + [SEARCH],
                                        messages=[{"role": "user", "content": "Where is SO-10303?"}])
    kinds = [b.type for b in first.content]
    assert "server_tool_use" in kinds and "tool_search_tool_result" in kinds and first.stop_reason == "tool_use"
    result = next(b for b in first.content if b.type == "tool_search_tool_result")
    assert [r.tool_name for r in result.content.tool_references] == ["get_shipment_tracking"]
    tool_use = next(b for b in first.content if b.type == "tool_use")
    assert tool_use.name == "get_shipment_tracking"


def test_tool_result_for_a_server_tool_is_rejected(client):
    first = client.beta.messages.create(model=OPUS, max_tokens=2000, system="<test_tool_search>", tools=CATALOG + [SEARCH],
                                        messages=[{"role": "user", "content": "Where is SO-10303?"}])
    srv = next(b for b in first.content if b.type == "server_tool_use")
    use = next(b for b in first.content if b.type == "tool_use")
    with pytest.raises(anthropic.BadRequestError, match="server tool"):
        client.beta.messages.create(model=OPUS, max_tokens=2000, system="<test_tool_search>", tools=CATALOG + [SEARCH],
                                    messages=[{"role": "user", "content": "Where is SO-10303?"},
                                              {"role": "assistant", "content": first.content},
                                              {"role": "user", "content": [
                                                  {"type": "tool_result", "tool_use_id": use.id, "content": "delivered"},
                                                  {"type": "tool_result", "tool_use_id": srv.id, "content": "x"}]}])
    ok = client.beta.messages.create(model=OPUS, max_tokens=2000, system="<test_tool_search>", tools=CATALOG + [SEARCH],
                                     messages=[{"role": "user", "content": "Where is SO-10303?"},
                                               {"role": "assistant", "content": first.content},
                                               {"role": "user", "content": [
                                                   {"type": "tool_result", "tool_use_id": use.id, "content": "delivered"}]}])
    assert "delivered" in "".join(b.text for b in ok.content if b.type == "text")


def test_all_deferred_is_rejected_and_invalid_regex_is_a_result_error(client):
    with pytest.raises(anthropic.BadRequestError, match="defer_loading"):
        client.beta.messages.create(model=OPUS, max_tokens=100, tools=[dict(t, defer_loading=True) for t in CATALOG],
                                    messages=[{"role": "user", "content": "hi"}])
    mock_api()._faults.clear()
    req = MockRequest({"model": OPUS, "max_tokens": 10, "tools": CATALOG + [SEARCH], "messages": []})
    from labkit.mock.api import _search_tools
    err = _search_tools(req, "regex", "(unclosed", 5)
    assert err["type"] == "tool_search_tool_result_error" and err["error_code"] == "invalid_tool_input"
    hits = _search_tools(req, "bm25", "invoice balance open", 5)
    assert hits["tool_references"][0]["tool_name"] == "get_invoice_balance"


# ----------------------------------------------------------------------------- inline tool changes
def test_tool_addition_surfaces_a_deferred_tool_without_a_cache_miss(client):
    tools = [_tool("get_order", "x" * 3000), _tool("book_field_service", "Book a visit", defer=True)]
    base = [{"role": "user", "content": "Book a visit for SO-10303"}]
    first = client.beta.messages.create(model=OPUS, max_tokens=500, tools=tools, messages=base,
                                        system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}])
    assert first.usage.cache_creation_input_tokens > 0
    added = base + [{"role": "assistant", "content": first.content},
                    {"role": "user", "content": "go on"},
                    {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "book_field_service"}}]}]
    second = client.beta.messages.create(model=OPUS, max_tokens=500, tools=tools, messages=added,
                                         system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
                                         betas=["mid-conversation-tool-changes-2026-07-01"])
    assert second.usage.cache_read_input_tokens > 0, "the cached prefix survives a tool_addition"
    req = MockRequest({"model": OPUS, "max_tokens": 1, "tools": tools, "messages": added})
    assert "book_field_service" in req.loaded_tool_names
    with pytest.raises(anthropic.BadRequestError, match="mid-conversation-tool-changes"):
        client.beta.messages.create(model=OPUS, max_tokens=500, tools=tools, messages=added, system="s")


# ----------------------------------------------------------------------------- code execution + PTC
@scenario("test.ptc", match=lambda r: "<test_ptc>" in r.system_text, priority=50)
def _ptc_policy(req: MockRequest):
    if req.completed_code is not None:
        return say("Done: " + req.completed_code["content"]["stdout"].strip())
    if req.code_results:
        return say("already ran")
    code = ("import json\n"
            "rows = [json.loads(await get_order({'order_id': oid})) for oid in ['SO-1', 'SO-2']]\n"
            "print(sum(r['total'] for r in rows))\n")
    return Reply(content=[run_code(code)])


def test_programmatic_tool_calling_pauses_and_resumes(client):
    tools = [CODE, _tool("get_order", "Get an order", allowed_callers=["code_execution_20260120"])]
    msgs = [{"role": "user", "content": "Total of SO-1 and SO-2?"}]
    first = client.beta.messages.create(model=OPUS, max_tokens=4000, system="<test_ptc>", tools=tools, messages=msgs)
    assert first.stop_reason == "tool_use" and first.container is not None
    calls = [b for b in first.content if b.type == "tool_use"]
    assert calls and all(c.caller.type == "code_execution_20260120" for c in calls)
    srv = next(b for b in first.content if b.type == "server_tool_use")
    assert calls[0].caller.tool_id == srv.id
    msgs += [{"role": "assistant", "content": first.content},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id,
                                           "content": json.dumps({"total": 10})} for c in calls]}]
    with pytest.raises(anthropic.BadRequestError, match="container"):
        client.beta.messages.create(model=OPUS, max_tokens=4000, system="<test_ptc>", tools=tools, messages=msgs)
    second = client.beta.messages.create(model=OPUS, max_tokens=4000, system="<test_ptc>", tools=tools, messages=msgs,
                                         container=first.container.id)
    if second.stop_reason == "tool_use":          # the second order's call came in a later pause
        calls2 = [b for b in second.content if b.type == "tool_use"]
        msgs += [{"role": "assistant", "content": second.content},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id,
                                               "content": json.dumps({"total": 10})} for c in calls2]}]
        second = client.beta.messages.create(model=OPUS, max_tokens=4000, system="<test_ptc>", tools=tools, messages=msgs,
                                             container=first.container.id)
    result = next(b for b in second.content if b.type == "code_execution_tool_result")
    assert result.content.stdout.strip() == "20" and result.content.return_code == 0
    assert "Done: 20" in "".join(b.text for b in second.content if b.type == "text")


def test_allowed_callers_needs_a_code_execution_tool(client):
    with pytest.raises(anthropic.BadRequestError, match="allowed_callers"):
        client.beta.messages.create(model=OPUS, max_tokens=100,
                                    tools=[_tool("get_order", "x", allowed_callers=["code_execution_20260120"])],
                                    messages=[{"role": "user", "content": "hi"}])


# ----------------------------------------------------------------------------- files
def test_files_api_round_trip_and_document_by_file_id(client):
    uploaded = client.files.upload(file=("notes.txt", b"The KP-250 seal kit is MS-250.", "text/plain"))
    assert uploaded.id.startswith("file_mock_") and uploaded.size_bytes == 30
    assert any(f.id == uploaded.id for f in client.files.list().data)
    assert client.files.download(uploaded.id).read() == b"The KP-250 seal kit is MS-250."
    req = MockRequest({"model": OPUS, "max_tokens": 1, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": uploaded.id}, "title": "notes"}]}]})
    assert "MS-250" in req.documents[0]["text"]
    client.files.delete(uploaded.id)
    with pytest.raises(anthropic.NotFoundError):
        client.files.retrieve_metadata(uploaded.id)


# ----------------------------------------------------------------------------- effort, clear_at, budgets
def test_per_message_effort_and_clear_at_rules(client):
    msgs = [{"role": "user", "content": "Plan."}, {"role": "assistant", "content": "Plan: ..."},
            {"role": "system", "content": [], "output_config": {"effort": "low"}},
            {"role": "user", "content": "Now rename the file."}]
    with pytest.raises(anthropic.BadRequestError, match="mid-conversation-output-config"):
        client.beta.messages.create(model=OPUS, max_tokens=500, messages=msgs)
    ok = client.beta.messages.create(model=OPUS, max_tokens=500, messages=msgs,
                                     betas=["mid-conversation-output-config-2026-07-01"])
    assert ok.stop_reason == "end_turn"
    assert MockRequest({"model": OPUS, "max_tokens": 1, "messages": msgs}).effort == "low"
    with pytest.raises(anthropic.BadRequestError, match="per-turn effort"):     # Fable 5: system messages yes, per-turn effort no
        client.beta.messages.create(model="claude-fable-5", max_tokens=500, messages=msgs,
                                    betas=["mid-conversation-output-config-2026-07-01"])
    # A turn-scoped reminder goes after the user message it applies to (results first, reminders after them) and
    # stays in the transcript; once a later user message exists it renders nothing.
    reminder = [{"role": "user", "content": "Run it."},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                {"role": "user", "content": "It ran."},
                {"role": "system", "content": "Check the inbox first.", "clear_at": "next_user_message"}]
    with pytest.raises(anthropic.BadRequestError, match="clear_at"):
        client.beta.messages.create(model=OPUS, max_tokens=300, messages=reminder)
    ok = client.beta.messages.create(model=OPUS, max_tokens=300, messages=reminder,
                                     betas=["mid-conversation-system-clear-at-2026-08-21"])
    assert ok.stop_reason == "end_turn"
    assert MockRequest({"model": OPUS, "max_tokens": 1, "messages": reminder}).system_messages == ["Check the inbox first."]
    later = reminder + [{"role": "assistant", "content": ok.content}, {"role": "user", "content": "Next."}]
    assert MockRequest({"model": OPUS, "max_tokens": 1, "messages": later}).system_messages == []
    with pytest.raises(anthropic.BadRequestError, match="followed by an assistant"):
        client.beta.messages.create(model=OPUS, max_tokens=300, betas=["mid-conversation-system-clear-at-2026-08-21"],
                                    messages=reminder + [{"role": "user", "content": "Next."}])


def test_task_budget_rules(client):
    msgs = [{"role": "user", "content": "hi"}]
    with pytest.raises(anthropic.BadRequestError, match="task-budgets"):
        client.beta.messages.create(model=OPUS, max_tokens=300, messages=msgs,
                                    output_config={"task_budget": {"type": "tokens", "total": 50000}})
    with pytest.raises(anthropic.BadRequestError, match="20000"):
        client.beta.messages.create(model=OPUS, max_tokens=300, messages=msgs, betas=["task-budgets-2026-03-13"],
                                    output_config={"task_budget": {"type": "tokens", "total": 5000}})
    ok = client.beta.messages.create(model=OPUS, max_tokens=300, messages=msgs, betas=["task-budgets-2026-03-13"],
                                     output_config={"task_budget": {"type": "tokens", "total": 50000}})
    assert ok.stop_reason == "end_turn"


# ----------------------------------------------------------------------------- preserved thinking
def _turn(client, model, messages, **kw):
    return client.beta.messages.create(model=model, max_tokens=1500, messages=messages, system="You are terse.", **kw)


def test_editing_history_invalidates_later_thinking_on_binding_models(client):
    msgs = [{"role": "user", "content": "Name a pump."}]
    first = _turn(client, FABLE, msgs)
    assert first.content[0].type == "thinking"
    msgs += [{"role": "assistant", "content": first.content}, {"role": "user", "content": "And a valve."}]
    _turn(client, FABLE, msgs)                                   # append-only: fine
    edited = [{"role": "user", "content": "Name a pump, please."}] + msgs[1:]
    with pytest.raises(anthropic.BadRequestError, match="bound to a different conversation") as exc:
        _turn(client, FABLE, edited)
    assert "thinking-binding-controls" in str(exc.value)
    dropped = _turn(client, FABLE, edited, betas=["thinking-binding-controls-2026-08-01"],
                    thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}})
    assert dropped.input_transformations and dropped.input_transformations[0].reason == "prefix_binding_mismatch"
    assert dropped.input_transformations[0].path == "messages.1.content.0"
    clean = _turn(client, FABLE, msgs, betas=["thinking-binding-controls-2026-08-01"])
    assert clean.input_transformations == []


def test_recording_model_only_enforces_when_asked(client):
    """Opus 5.5 in the mock behaves like an account created before 2026-08-31: the mismatch is recorded, listed
    with the beta header, and enforced only when the request sets prefix_mismatch_behavior (any value)."""
    model = "claude-opus-5-5"
    msgs = [{"role": "user", "content": "Name a pump."}]
    first = _turn(client, model, msgs)
    edited = [{"role": "user", "content": "Name a pump, please."},
              {"role": "assistant", "content": first.content}, {"role": "user", "content": "And a valve."}]
    assert _turn(client, model, edited).stop_reason == "end_turn"      # recorded, not enforced
    listed = _turn(client, model, edited, betas=["thinking-binding-controls-2026-08-01"])
    assert [t.type for t in listed.input_transformations] == ["thinking_mismatch_allowed"]
    with pytest.raises(anthropic.BadRequestError, match="Extra inputs"):
        _turn(client, model, edited, thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}})
    with pytest.raises(anthropic.BadRequestError, match="bound to a different"):   # any value opts in to enforcement
        _turn(client, model, edited, betas=["thinking-binding-controls-2026-08-01"],
              thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}})
    # Opus 5 does not run the conversation check at all.
    first5 = _turn(client, OPUS, msgs)
    edited5 = [{"role": "user", "content": "Name a pump, please."},
               {"role": "assistant", "content": first5.content}, {"role": "user", "content": "And a valve."}]
    assert _turn(client, OPUS, edited5, betas=["thinking-binding-controls-2026-08-01"]).input_transformations == []


def test_model_binding_drops_blocks_the_target_cannot_read(client):
    msgs = [{"role": "user", "content": "Name a pump."}]
    first = _turn(client, "claude-opus-5-5", msgs)
    msgs += [{"role": "assistant", "content": first.content}, {"role": "user", "content": "And a valve."}]
    switched = _turn(client, OPUS, msgs, betas=["thinking-binding-controls-2026-08-01"])
    assert [t.reason for t in switched.input_transformations] == ["model_binding_mismatch"]
    assert _turn(client, FABLE, msgs, betas=["thinking-binding-controls-2026-08-01"]).input_transformations == []


# ----------------------------------------------------------------------------- cache engineering
def test_prewarm_with_max_tokens_zero_and_concurrent_writes(client):
    system = [{"type": "text", "text": "K" * 4000, "cache_control": {"type": "ephemeral"}}]
    warm = client.messages.create(model=OPUS, max_tokens=0, system=system, messages=[{"role": "user", "content": "x"}])
    assert warm.content == [] and warm.stop_reason == "max_tokens" and warm.usage.cache_creation_input_tokens > 0
    hit = client.messages.create(model=OPUS, max_tokens=50, system=system, messages=[{"role": "user", "content": "x"}])
    assert hit.usage.cache_read_input_tokens > 0

    mock_api().cache.clear()
    mock_api().cache.ready_delay = 0.5
    system2 = [{"type": "text", "text": "Q" * 4000, "cache_control": {"type": "ephemeral"}}]
    results = []

    def fire():
        results.append(client.messages.create(model=OPUS, max_tokens=50, system=system2,
                                              messages=[{"role": "user", "content": "x"}]).usage)
    threads = [threading.Thread(target=fire) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(u.cache_read_input_tokens == 0 for u in results), "parallel requests all write, none reads"
    mock_api().cache.ready_delay = 0.0


# ----------------------------------------------------------------------------- managed agents
@scenario("test.managed", match=lambda r: "<test_managed>" in r.system_text, priority=50)
def _managed_policy(req: MockRequest):
    if req.has_tool("lookup_order") and not req.called("lookup_order"):
        return use_tools(tool("lookup_order", order_id="SO-10303"))
    if req.called("lookup_order"):
        return say("Order status: " + (req.calls("lookup_order")[0].result or ""))
    return say("hello from the agent")


def test_managed_agents_session_with_custom_tool_round_trip(client):
    agent = client.beta.agents.create(name="Desk", model=OPUS, system="<test_managed>",
                                      tools=[{"type": "custom", "name": "lookup_order", "description": "Look up an order",
                                              "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}}}])
    assert agent.version == 1
    env = client.beta.environments.create(name="lab")
    session = client.beta.sessions.create(agent=agent.id, environment_id=env.id, title="t1")
    assert session.status == "idle"
    sent = client.beta.sessions.events.send(session.id, events=[{"type": "user.message",
                                                                 "content": [{"type": "text", "text": "Where is SO-10303?"}]}])
    assert sent.data[0].type == "user.message"
    events = list(client.beta.sessions.events.list(session.id))
    idle = [e for e in events if e.type == "session.status_idle"][-1]
    assert idle.stop_reason.type == "requires_action"
    use = next(e for e in events if e.type == "agent.custom_tool_use")
    assert use.name == "lookup_order" and idle.stop_reason.event_ids == [use.id]
    client.beta.sessions.events.send(session.id, events=[{"type": "user.custom_tool_result", "custom_tool_use_id": use.id,
                                                          "content": [{"type": "text", "text": "shipped"}]}])
    streamed = list(client.beta.sessions.events.stream(session.id))
    last_msg = [e for e in streamed if e.type == "agent.message"][-1]
    assert "shipped" in last_msg.content[0].text
    assert [e for e in streamed if e.type == "session.status_idle"][-1].stop_reason.type == "end_turn"
    fetched = client.beta.sessions.retrieve(session.id)
    assert fetched.usage.input_tokens > 0 and fetched.usage.list_cost.currency == "USD"


def test_managed_agents_coordinator_threads_and_budget(client):
    worker = client.beta.agents.create(name="worker", model=OPUS, system="<test_managed>")
    lead = client.beta.agents.create(name="lead", model=OPUS, system="<test_coordinator>",
                                     multiagent={"type": "coordinator", "agents": [worker.id, {"type": "self"}]})

    @scenario("test.coordinator", match=lambda r: "<test_coordinator>" in r.system_text, priority=50)
    def _lead(req: MockRequest):
        if not req.called("send_to_agent"):
            return use_tools(tool("send_to_agent", agent="worker", message="report in"))
        return say("worker said: " + (req.calls("send_to_agent")[0].result or ""))

    env = client.beta.environments.create(name="lab")
    session = client.beta.sessions.create(agent=lead.id, environment_id=env.id,
                                          initial_events=[{"type": "user.message", "content": [{"type": "text", "text": "go"}]}],
                                          budget={"type": "limit", "max_list_cost": {"amount": "2500", "currency": "USD"}})
    events = list(client.beta.sessions.events.list(session.id))
    kinds = [e.type for e in events]
    assert "session.thread_created" in kinds and "agent.thread_message_sent" in kinds and "agent.thread_message_received" in kinds
    threads = list(client.beta.sessions.threads.list(session.id))
    assert len(threads) == 2 and any(t.parent_thread_id for t in threads)
    final = [e for e in events if e.type == "agent.message"][-1]
    assert "hello from the agent" in final.content[0].text
    with pytest.raises(anthropic.BadRequestError, match="one level"):
        client.beta.agents.create(name="grand", model=OPUS, multiagent={"type": "coordinator", "agents": [lead.id]})


def test_managed_agents_versions_mounts_budget_resume_and_cached_history(client):
    import anthropic
    from labkit import runs_dir
    long_system = "<test_managed>\n" + ("Kestrel recall context line. " * 220)         # well over the cacheable minimum
    agent = client.beta.agents.create(name="Desk", model=OPUS, system=long_system,
                                      tools=[{"type": "custom", "name": "lookup_order", "description": "Look up an order",
                                              "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}}}])
    v2 = client.beta.agents.update(agent.id, description="second version")
    assert v2.version == 2 and len(list(client.beta.agents.versions.list(agent.id))) == 2
    with pytest.raises(anthropic.ConflictError):                                   # an update based on a stale version
        client.beta.agents.update(agent.id, description="stale", extra_body={"version": 1})
    env = client.beta.environments.create(name="lab")
    upload = client.files.upload(file=("units.json", b'[{"serial": "KP250-2608-0002"}]', "application/json"))
    session = client.beta.sessions.create(agent={"type": "agent", "id": agent.id, "version": 1}, environment_id=env.id,
                                          resources=[{"type": "file", "file_id": upload.id, "mount_path": "/workspace/units.json"}],
                                          budget={"type": "limit", "max_list_cost": {"amount": "1", "currency": "USD"}})
    assert session.agent.version == 1                                              # pinned to the requested version
    assert (runs_dir("mock_sessions", session.id) / "workspace" / "units.json").read_bytes().startswith(b"[{")
    assert session.resources[0].file_id == upload.id
    client.beta.sessions.events.send(session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "Where is SO-10303?"}]}])
    use = next(e for e in client.beta.sessions.events.list(session.id) if e.type == "agent.custom_tool_use")
    client.beta.sessions.events.send(session.id, events=[{"type": "user.custom_tool_result", "custom_tool_use_id": use.id,
                                                          "content": [{"type": "text", "text": "shipped"}]}])
    idle = [e for e in client.beta.sessions.events.list(session.id) if e.type == "session.status_idle"][-1]
    assert idle.stop_reason.type == "budget_reached"                               # one cent does not buy a second call
    with pytest.raises(anthropic.BadRequestError, match="budget"):
        client.beta.sessions.events.send(session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "more"}]}])
    resumed = client.beta.sessions.update(session.id, budget={"type": "limit", "max_list_cost": {"amount": "5000", "currency": "USD"}})
    final = [e for e in client.beta.sessions.events.list(session.id) if e.type == "session.status_idle"][-1]
    assert final.stop_reason.type == "end_turn" and resumed.usage.cache_read_input_tokens > 0     # the history was cached


def test_programmatic_tool_calls_are_not_billed_as_model_tokens(client):
    from labkit.mock.cache import render_positions
    from labkit.mock.render import build_message
    from labkit.mock.cache import CacheResult
    called = {"type": "tool_use", "id": "toolu_x", "name": "get_order", "input": {"order_id": "SO-1"},
              "caller": {"type": "code_execution_20260120", "tool_id": "srvtoolu_1"}}
    direct = {"type": "tool_use", "id": "toolu_y", "name": "get_order", "input": {"order_id": "SO-1"}}
    body = {"model": OPUS, "max_tokens": 10, "messages": [
        {"role": "user", "content": "total?"},
        {"role": "assistant", "content": [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "code_execution", "input": {"code": "x"}}, called]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "{\"total\": 10}" * 50}]}]}
    positions = render_positions(body)
    assert positions[-1][1] == 0 and positions[-2][1] == 0                    # the code-called use and its result cost nothing
    body["messages"][1]["content"][1] = direct
    body["messages"][2]["content"][0]["tool_use_id"] = "toolu_y"
    assert render_positions(body)[-1][1] > 0                                  # a model-issued call is billed as usual
    req = MockRequest({"model": OPUS, "max_tokens": 4000, "messages": [{"role": "user", "content": "hi"}]})
    empty = CacheResult(positions=[], hits=0, read_tokens=0, write_tokens_5m=0, write_tokens_1h=0, uncached_tokens=5) \
        if "positions" in CacheResult.__dataclass_fields__ else mock_api().cache.process(req.body, req.spec)
    paused = build_message(req, Reply(content=[called], stop_reason="tool_use", complexity=0.0), empty)
    assert paused["usage"]["output_tokens"] == 0 and paused["content"][0]["type"] == "tool_use"   # no thinking, no output charge
