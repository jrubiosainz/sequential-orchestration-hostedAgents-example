# Copyright (c) Microsoft. All rights reserved.

import asyncio
import os
from typing import Never

from agent_framework import (
    Agent,
    AgentResponse,
    AgentResponseUpdate,
    Executor,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)
from agent_framework.azure import AzureOpenAIResponsesClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from observability import configure_azure_monitor_tracing
from azure.ai.agentserver.agentframework import from_agent_framework


"""
Sample: Sequential workflow hosted on Microsoft Foundry as a Hosted Agent.

Sequential Workflow: Researcher -> Writer -> Reviewer

This workflow orchestrates three steps in sequence, each implemented as its own
``Executor``:
1. Researcher: processes the initial user message.
2. Writer: takes the researcher's output and generates content.
3. Reviewer: reviews and finalizes the content.

NOTE ON "WHERE THE AGENTS LIVE"
-------------------------------
The Researcher / Writer / Reviewer ``Agent`` objects below are **local,
in-memory** objects. They are NOT created, registered, or modified in your
Foundry project's *Agents* list. Each one simply calls your **model
deployment** (via ``AzureOpenAIResponsesClient``). The only thing that appears
in Foundry is the *hosted agent* container that wraps this whole workflow.
(If you instead want to invoke agents you have already defined in the Foundry
portal, that needs a different client -- ``AzureAIAgentClient`` referencing each
agent by id -- which is not what this sample does.)

STREAMING (why each Executor streams)
-------------------------------------
Each handler runs its agent with ``stream=True`` and forwards every incremental
update via ``ctx.yield_output(update)``. The Foundry Playground opens a
*streaming* connection, so emitting updates as the model generates keeps that
connection alive from the very first token of the Researcher onward. The
earlier version called the blocking ``agent.run(...)`` and only emitted once, at
the end, so the Playground received no bytes for the whole run and timed out
with a "network error".

It is wrapped with ``from_agent_framework(...)`` so the Foundry Hosted Agent
runtime can serve it (the agent server listens on port 8088).

Prerequisites (set via a local .env file, NOT committed):
- AZURE_AI_PROJECT_ENDPOINT       -> your Foundry project endpoint
- AZURE_AI_MODEL_DEPLOYMENT_NAME  -> a model deployment in that project
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


class ResearcherExecutor(Executor):
    """First step: process the initial message and forward the conversation."""

    agent: Agent

    def __init__(self, agent: Agent, id: str = "Researcher"):
        self.agent = agent
        super().__init__(id=id)

    @handler
    async def handle(
        self,
        message: Message | list[Message],
        ctx: WorkflowContext[list[Message], AgentResponseUpdate],
    ) -> None:
        messages = message if isinstance(message, list) else [message]

        # Stream the agent's output so the Playground connection stays alive.
        updates: list[AgentResponseUpdate] = []
        async for update in self.agent.run(messages, stream=True):
            updates.append(update)
            await ctx.yield_output(update)

        response = AgentResponse.from_updates(updates)
        messages.extend(response.messages)
        await ctx.send_message(messages)


class WriterExecutor(Executor):
    """Second step: receive research output and generate content."""

    agent: Agent

    def __init__(self, agent: Agent, id: str = "Writer"):
        self.agent = agent
        super().__init__(id=id)

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext[list[Message], AgentResponseUpdate],
    ) -> None:
        updates: list[AgentResponseUpdate] = []
        async for update in self.agent.run(messages, stream=True):
            updates.append(update)
            await ctx.yield_output(update)

        response = AgentResponse.from_updates(updates)
        messages.extend(response.messages)
        await ctx.send_message(messages)


class ReviewerExecutor(Executor):
    """Final step: review the content and stream the final output."""

    agent: Agent

    def __init__(self, agent: Agent, id: str = "Reviewer"):
        self.agent = agent
        super().__init__(id=id)

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext[Never, AgentResponseUpdate],
    ) -> None:
        async for update in self.agent.run(messages, stream=True):
            await ctx.yield_output(update)


async def main() -> None:
    """Build the sequential workflow and serve it as a Hosted Agent."""

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

            print("Loading agents from deployment...")
            researcher_client = create_client_for_agent(project_client)
            writer_client = create_client_for_agent(project_client)
            reviewer_client = create_client_for_agent(project_client)
            print("All agents loaded successfully\n")

            # These Agent objects are LOCAL. They are not created in Foundry.
            # `instructions` only sets each local step's prompt; it does not
            # register or change anything in your Foundry project.
            researcher = Agent(
                name="Researcher",
                description="Collects relevant information",
                instructions=(
                    "You are a researcher. Given the user's topic, produce a "
                    "concise, well-organized set of the most important facts, "
                    "angles and talking points as short bullet points "
                    "(under ~200 words). Do not write the final article."
                ),
                client=researcher_client,
            )

            writer = Agent(
                name="Writer",
                description="Creates well-structured content based on research",
                instructions=(
                    "You are a writer. Using the research notes already in the "
                    "conversation, write a clear, engaging draft of a few short "
                    "paragraphs (under ~300 words)."
                ),
                client=writer_client,
            )

            reviewer = Agent(
                name="Reviewer",
                description="Evaluates content quality and returns the final piece",
                instructions=(
                    "You are an editor. Review the draft in the conversation for "
                    "clarity, flow and correctness, fix any issues, and return the "
                    "final polished version. Output only the final text."
                ),
                client=reviewer_client,
            )

            researcher_executor = ResearcherExecutor(researcher)
            writer_executor = WriterExecutor(writer)
            reviewer_executor = ReviewerExecutor(reviewer)

            workflow = (
                WorkflowBuilder(
                    name="SequentialResearchWorkflow",
                    description="Research -> Write -> Review sequential workflow",
                    start_executor=researcher_executor,
                )
                .add_edge(researcher_executor, writer_executor)
                .add_edge(writer_executor, reviewer_executor)
                .build()
            )

            # Turn the workflow into an agent and serve it (listens on :8088).
            agentwf = workflow.as_agent()
            await from_agent_framework(agentwf).run_async()


if __name__ == "__main__":
    asyncio.run(main())
