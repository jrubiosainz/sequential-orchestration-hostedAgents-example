# Copyright (c) Microsoft. All rights reserved.

import asyncio
import os
from typing import Never

from agent_framework import (
    Agent,
    Message,
    Executor,
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

This workflow orchestrates three steps in sequence:
1. Researcher: processes the initial user message.
2. Writer: takes the researcher's output and generates content.
3. Reviewer: reviews and finalizes the content.

It is wrapped with `from_agent_framework(...)` so the Foundry Hosted Agent
runtime can serve it (the agent server listens on port 8088).

Prerequisites (set via a local .env file, NOT committed):
- AZURE_AI_PROJECT_ENDPOINT       -> your Foundry project endpoint
- AZURE_AI_MODEL_DEPLOYMENT_NAME  -> a model deployment in that project
"""


async def create_client_for_agent(
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
    async def handle(self, message: Message | list[Message], ctx: WorkflowContext[list[Message]]) -> None:
        messages = message if isinstance(message, list) else [message]

        response = await self.agent.run(messages)

        print("\n[Researcher] output:")
        text = response.messages[-1].text if response.messages else ""
        print(f"{text[:500]}..." if len(text) > 500 else text)

        messages.extend(response.messages)
        await ctx.send_message(messages)


class WriterExecutor(Executor):
    """Second step: receive research output and generate content."""

    agent: Agent

    def __init__(self, agent: Agent, id: str = "Writer"):
        self.agent = agent
        super().__init__(id=id)

    @handler
    async def handle(self, messages: list[Message], ctx: WorkflowContext[list[Message]]) -> None:
        response = await self.agent.run(messages)

        print("\n[Writer] output:")
        text = response.messages[-1].text if response.messages else ""
        print(f"{text[:500]}..." if len(text) > 500 else text)

        messages.extend(response.messages)
        await ctx.send_message(messages)


class ReviewerExecutor(Executor):
    """Final step: review the content and yield the workflow output."""

    agent: Agent

    def __init__(self, agent: Agent, id: str = "Reviewer"):
        self.agent = agent
        super().__init__(id=id)

    @handler
    async def handle(self, messages: list[Message], ctx: WorkflowContext[Never, list[Message]]) -> None:
        response = await self.agent.run(messages)

        print("\n[Reviewer] output:")
        text = response.messages[-1].text if response.messages else ""
        print(f"{text[:500]}..." if len(text) > 500 else text)

        messages.extend(response.messages)
        await ctx.yield_output(messages)


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
            researcher_client = await create_client_for_agent(project_client)
            writer_client = await create_client_for_agent(project_client)
            reviewer_client = await create_client_for_agent(project_client)
            print("All agents loaded successfully\n")

            researcher = Agent(
                name="Researcher",
                description="Collects relevant information",
                client=researcher_client,
            )

            writer = Agent(
                name="Writer",
                description="Creates well-structured content based on research",
                client=writer_client,
            )

            reviewer = Agent(
                name="Reviewer",
                description="Evaluates content quality and provides feedback",
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
