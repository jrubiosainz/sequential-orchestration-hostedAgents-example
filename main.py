# Copyright (c) Microsoft. All rights reserved.

import asyncio
import contextlib
import os
from typing import Any, AsyncIterator

from agent_framework import (
    Agent,
    AgentResponse,
    AgentResponseUpdate,
    BaseAgent,
    Content,
    Message,
    ResponseStream,
)
from agent_framework.azure import AzureOpenAIResponsesClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from observability import configure_azure_monitor_tracing
from azure.ai.agentserver.agentframework import from_agent_framework


"""
Sample: Sequential orchestration hosted on Microsoft Foundry as a Hosted Agent.

Sequential orchestration: Researcher -> Writer -> Reviewer

This agent orchestrates three steps in sequence:
1. Researcher: searches the web with Bing Grounding and gathers facts.
2. Writer: takes the researcher's output and generates content.
3. Reviewer: reviews and finalizes the content.

NOTE ON "WHERE THE AGENTS LIVE"
-------------------------------
The Researcher / Writer / Reviewer ``Agent`` objects below are **local,
in-memory** objects. They are NOT created, registered, or modified in your
Foundry project's *Agents* list. Each one simply calls your **model
deployment** through the **Responses API** (via ``AzureOpenAIResponsesClient``).
The only thing that appears in Foundry is the *hosted agent* container that
wraps this whole workflow.

BING GROUNDING (why the Researcher can search the web)
------------------------------------------------------
The Researcher gets a ``bing_grounding`` tool that points at the **Grounding
with Bing Search** connection in your Foundry project. Without that tool the
agent can only answer from the foundational model's training data (which is
stale), which is why an earlier version returned out-of-date facts.

Two details matter and were the actual fix:

* The tool MUST be attached through the **Responses API**
  (``AzureOpenAIResponsesClient``). gpt-5 rejects the *classic* Agent-Service
  ``bing_grounding`` tool ("This model only supports Responses API compatible
  tools"), so a Foundry-Agent-Service client does not work here.
* The Responses-API tool shape uses ``project_connection_id`` (not the classic
  ``connection_id``):

      {
        "type": "bing_grounding",
        "bing_grounding": {
          "search_configurations": [
            {"project_connection_id": "<full connection id>"}
          ]
        }
      }

Set ``BING_GROUNDING_CONNECTION_ID`` to the full ARM id of your Bing connection
(Foundry portal -> your project -> Management center / Connected resources ->
your "Grounding with Bing Search" connection -> the connection id, which looks
like ``/subscriptions/.../connections/<name>``). If the variable is not set the
agent still runs, but the Researcher answers without web search.

STREAMING (why a custom orchestrator + heartbeats)
--------------------------------------------------
This sample deliberately does NOT use ``WorkflowBuilder``. A workflow runs each
executor as a Pregel "superstep" and only drains that step's yielded output when
the superstep *finishes*, so the Playground would receive nothing until the
(slow, Bing-powered) Researcher completed - ~50-60s of silence that can trip the
streaming connection ("network error").

Instead, ``SequentialOrchestratorAgent`` is a plain ``BaseAgent`` that runs the
three agents itself. The Hosted Agent runtime serves a ``BaseAgent`` through the
AIAgent adapter, which forwards ``run(stream=True)`` updates to the client
*update-by-update* with no buffering. The orchestrator runs each inner agent with
``stream=True`` and forwards every token live. To survive the ~50s of silence
while gpt-5 performs a Bing search, ``_stream_agent_with_heartbeat`` emits an
immediate first byte and a tiny whitespace keepalive every few seconds during any
silent gap (heartbeats are tagged so they never leak into the conversation passed
to the next agent). We also request a low reasoning effort for reasoning-capable
models (see ``_reasoning_kwargs``).

It is wrapped with ``from_agent_framework(...)`` so the Foundry Hosted Agent
runtime can serve it (the agent server listens on port 8088).

Prerequisites (set via a local .env file, NOT committed):
- AZURE_AI_PROJECT_ENDPOINT       -> your Foundry project endpoint
- AZURE_AI_MODEL_DEPLOYMENT_NAME  -> a model deployment in that project (gpt-5)
- BING_GROUNDING_CONNECTION_ID    -> full id of your Bing grounding connection
"""


def create_client_for_agent(
    project_client: AIProjectClient,
) -> AzureOpenAIResponsesClient:
    """Create an AzureOpenAIResponsesClient backed by the Foundry project."""
    model_deployment = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    if not model_deployment:
        raise ValueError(
            "AZURE_AI_MODEL_DEPLOYMENT_NAME environment variable is required")

    return AzureOpenAIResponsesClient(
        project_client=project_client,
        deployment_name=model_deployment,
    )


def _bing_grounding_tool() -> dict[str, Any] | None:
    """Build the Responses-API Bing grounding tool, or None if not configured.

    The connection id is read from ``BING_GROUNDING_CONNECTION_ID``. It must be
    the *full* connection id, e.g.::

        /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Cognitive
        Services/accounts/<account>/projects/<project>/connections/<name>
    """
    connection_id = os.environ.get("BING_GROUNDING_CONNECTION_ID", "").strip()
    if not connection_id:
        print(
            "BING_GROUNDING_CONNECTION_ID is not set; the Researcher will run "
            "WITHOUT Bing web search and may return outdated information."
        )
        return None

    return {
        "type": "bing_grounding",
        "bing_grounding": {
            "search_configurations": [
                {"project_connection_id": connection_id}
            ]
        },
    }


def _reasoning_kwargs() -> dict[str, Any]:
    """Request a low reasoning effort for reasoning-capable models.

    gpt-5 and the o-series are reasoning models: by default they spend extra
    time "thinking" before emitting any token, which adds latency to every step
    of the sequential workflow. A low effort keeps the Playground responsive.
    Non-reasoning models (e.g. gpt-4o, gpt-4.1) do not accept this parameter, so
    we only send it when the deployment name looks like a reasoning model.
    """
    model = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "").lower()
    is_reasoning = model.startswith(("gpt-5", "o1", "o3", "o4"))
    if is_reasoning:
        return {"reasoning": {"effort": "low"}}
    return {}


# How often to emit a keepalive while an agent is "thinking" and producing no
# output. The hosted runtime serves the Playground over a streaming HTTP
# connection; a reasoning model doing a Bing search can stay silent for ~50s
# before its first token, which can trip an idle/gateway timeout ("network
# error"). A small whitespace heartbeat keeps bytes flowing during those gaps.
HEARTBEAT_INTERVAL_S = 8.0
_HEARTBEAT_KEY = "_seqorch_heartbeat"


def _heartbeat_update(text: str = " ") -> AgentResponseUpdate:
    """A tiny whitespace update, tagged so we can tell it apart from real text."""
    return AgentResponseUpdate(
        contents=[Content.from_text(text)],
        role="assistant",
        additional_properties={_HEARTBEAT_KEY: True},
    )


def _is_heartbeat(update: AgentResponseUpdate) -> bool:
    props = getattr(update, "additional_properties", None) or {}
    return bool(props.get(_HEARTBEAT_KEY))


def _finalize_response(updates: "list[AgentResponseUpdate]") -> AgentResponse:
    """Build the final AgentResponse, ignoring heartbeat keepalives."""
    real = [u for u in updates if not _is_heartbeat(u)]
    return AgentResponse.from_updates(real)


def _normalize_messages(messages: Any) -> "list[Message]":
    if messages is None:
        return []
    if isinstance(messages, str):
        return [Message(role="user", contents=[Content.from_text(messages)])]
    if isinstance(messages, Message):
        return [messages]
    result: list[Message] = []
    for item in messages:
        if isinstance(item, str):
            result.append(Message(role="user", contents=[Content.from_text(item)]))
        else:
            result.append(item)
    return result


async def _stream_agent_with_heartbeat(
    agent: Agent,
    messages: Any,
    run_kwargs: dict[str, Any],
) -> AsyncIterator[AgentResponseUpdate]:
    """Run one agent with ``stream=True`` and yield its updates in real time,
    injecting a whitespace heartbeat whenever the agent stays silent for longer
    than ``HEARTBEAT_INTERVAL_S`` (e.g. while it performs a Bing search).

    A background task pumps the agent's stream into a queue while the consumer
    polls with a timeout; on timeout we emit a heartbeat. Heartbeats are tagged
    (see ``_heartbeat_update``) so the caller can forward them to the client
    without feeding them into the next agent's conversation.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    done = object()

    async def _produce() -> None:
        try:
            async for update in agent.run(messages, stream=True, **run_kwargs):
                await queue.put(update)
        except Exception as exc:  # surface to the consumer
            await queue.put(exc)
        finally:
            await queue.put(done)

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_S)
            except asyncio.TimeoutError:
                yield _heartbeat_update()
                continue
            if item is done:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer


class SequentialOrchestratorAgent(BaseAgent):
    """Researcher -> Writer -> Reviewer, hand-rolled so the output streams live.

    Why not a ``WorkflowBuilder``? The workflow runner buffers each executor's
    yielded output until that executor's superstep *finishes*, so the Foundry
    Playground would see nothing until the (slow, Bing-powered) Researcher
    completed - ~50-60s of silence that can trip the streaming connection
    ("network error"). This orchestrator runs the three agents itself and
    forwards their tokens (plus heartbeats) to the client in real time, exactly
    like the portal agents stream.

    It is a plain ``BaseAgent`` so the Hosted Agent runtime serves it directly
    (``from_agent_framework`` -> AIAgent adapter), which streams ``run()`` output
    update-by-update with no superstep buffering.
    """

    def __init__(
        self,
        researcher: Agent,
        writer: Agent,
        reviewer: Agent,
        run_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name="SequentialResearchWriter",
            description="Research -> Write -> Review sequential orchestration",
            **kwargs,
        )
        self._researcher = researcher
        self._writer = writer
        self._reviewer = reviewer
        self._run_kwargs = run_kwargs or {}

    def run(
        self,
        messages: Any = None,
        *,
        stream: bool = False,
        session: Any = None,
        **kwargs: Any,
    ):
        conversation = _normalize_messages(messages)
        if stream:
            return ResponseStream(self._run_stream(conversation), finalizer=_finalize_response)
        return self._run_collect(conversation)

    async def _run_stream(self, conversation: "list[Message]") -> AsyncIterator[AgentResponseUpdate]:
        # Immediate first byte: warm the streaming connection right away.
        yield _heartbeat_update()

        conversation = list(conversation)

        # 1) Researcher: search the web with Bing and gather sourced facts.
        research_updates: list[AgentResponseUpdate] = []
        async for update in _stream_agent_with_heartbeat(self._researcher, conversation, self._run_kwargs):
            yield update
            if not _is_heartbeat(update):
                research_updates.append(update)
        conversation.extend(AgentResponse.from_updates(research_updates).messages)

        # 2) Writer: turn the research notes into a draft.
        writer_updates: list[AgentResponseUpdate] = []
        async for update in _stream_agent_with_heartbeat(self._writer, conversation, self._run_kwargs):
            yield update
            if not _is_heartbeat(update):
                writer_updates.append(update)
        conversation.extend(AgentResponse.from_updates(writer_updates).messages)

        # 3) Reviewer: polish the draft and emit the final version.
        async for update in _stream_agent_with_heartbeat(self._reviewer, conversation, self._run_kwargs):
            yield update

    async def _run_collect(self, conversation: "list[Message]") -> AgentResponse:
        updates: list[AgentResponseUpdate] = []
        async for update in self._run_stream(conversation):
            updates.append(update)
        return _finalize_response(updates)


def build_agent(project_client: AIProjectClient) -> SequentialOrchestratorAgent:
    """Build the Researcher -> Writer -> Reviewer orchestrator.

    The Researcher is given a Bing grounding tool (when configured) so it can
    search the web; the Writer and Reviewer use the model only. These ``Agent``
    objects are LOCAL, in-memory objects: ``instructions`` only sets each step's
    prompt and nothing is created or registered in your Foundry project.
    """
    run_kwargs = _reasoning_kwargs()
    bing_tool = _bing_grounding_tool()

    researcher = Agent(
        name="Researcher",
        description="Searches the web with Bing and collects relevant information",
        instructions=(
            "You are a research analyst with access to web search. ALWAYS use "
            "the Bing Grounding Search tool to find current, accurate, "
            "well-sourced information about the user's topic. Produce a concise, "
            "well-organized set of the most important facts, angles and talking "
            "points as short bullet points (under ~200 words) and cite your "
            "sources. Do not write the final article."
        ),
        client=create_client_for_agent(project_client),
        tools=[bing_tool] if bing_tool else None,
    )

    writer = Agent(
        name="Writer",
        description="Creates well-structured content based on research",
        instructions=(
            "You are a writer. Using the research notes already in the "
            "conversation, write a clear, engaging draft of a few short "
            "paragraphs (under ~300 words)."
        ),
        client=create_client_for_agent(project_client),
    )

    reviewer = Agent(
        name="Reviewer",
        description="Evaluates content quality and returns the final piece",
        instructions=(
            "You are an editor. Review the draft in the conversation for "
            "clarity, flow and correctness, fix any issues, and return the "
            "final polished version. Output only the final text."
        ),
        client=create_client_for_agent(project_client),
    )

    return SequentialOrchestratorAgent(researcher, writer, reviewer, run_kwargs)


async def main() -> None:
    """Build the sequential orchestrator and serve it as a Hosted Agent."""

    if not os.environ.get("AZURE_AI_PROJECT_ENDPOINT"):
        raise ValueError(
            "AZURE_AI_PROJECT_ENDPOINT environment variable is required")

    async with DefaultAzureCredential() as credential:
        async with AIProjectClient(
            endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
            credential=credential,
        ) as project_client:

            # Best-effort observability. Tracing is nice to have, but it must
            # NEVER prevent the agent server from starting. The original sample
            # returned early here when Application Insights was not configured,
            # which left the container without a running server and the agent
            # never showed up in Foundry ("Found 0 hosted agents").
            try:
                await configure_azure_monitor_tracing(project_client)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"Observability not configured, continuing without tracing: {exc}")

            print("Building sequential orchestrator...")
            agent = build_agent(project_client)
            print("Orchestrator ready\n")

            # Serve the orchestrator directly (listens on :8088). Because it is a
            # plain BaseAgent (not a workflow), the runtime streams its output
            # update-by-update with no superstep buffering, so the Playground
            # sees tokens and heartbeats in real time.
            await from_agent_framework(agent).run_async()


if __name__ == "__main__":
    asyncio.run(main())
