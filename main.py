# Copyright (c) Microsoft. All rights reserved.

import asyncio
import os

from agent_framework import Agent, WorkflowBuilder
from agent_framework.azure import AzureOpenAIResponsesClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from observability import configure_azure_monitor_tracing
from azure.ai.agentserver.agentframework import from_agent_framework


"""
Sample: Sequential workflow hosted on Microsoft Foundry as a Hosted Agent.

Sequential Workflow: Researcher -> Writer -> Reviewer

This workflow orchestrates three agents in sequence:
1. Researcher: gathers the key facts and angles for the user's topic.
2. Writer: turns the research into a well-structured draft.
3. Reviewer: polishes the draft and returns the final piece.

The agents are wired *directly* into the WorkflowBuilder. The framework
auto-wraps each agent in a streaming ``AgentExecutor`` that emits incremental
``AgentRunUpdateEvent`` updates while the model is still generating. Those
updates are forwarded by ``from_agent_framework(...)`` as Server-Sent-Event
deltas, which keeps the Foundry Playground connection alive during long runs.

(The earlier version used custom ``Executor`` subclasses that called the
blocking ``agent.run(...)`` and only yielded once at the very end. That
produced no streamed bytes until the whole workflow finished, so the
Playground's streaming client timed out with a "network error".)

The result is wrapped with ``from_agent_framework(...)`` so the Foundry Hosted
Agent runtime can serve it (the agent server listens on port 8088).

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

            print("Creating agents...")

            researcher = Agent(
                name="Researcher",
                description="Collects relevant information",
                instructions=(
                    "You are a researcher. Given the user's topic, produce a "
                    "concise, well-organized set of the most important facts, "
                    "angles and talking points. Use short bullet points and "
                    "keep it under ~200 words. Do not write the final article."
                ),
                client=create_client_for_agent(project_client),
            )

            writer = Agent(
                name="Writer",
                description="Creates well-structured content based on research",
                instructions=(
                    "You are a writer. Using the research notes provided in the "
                    "conversation, write a clear, engaging draft (a few short "
                    "paragraphs). Keep it focused and under ~300 words."
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

            # Wire the agents directly as executors. WorkflowBuilder auto-wraps
            # each one in a streaming AgentExecutor, so updates are emitted
            # incrementally as the workflow runs (keeping SSE alive).
            workflow = (
                WorkflowBuilder(
                    name="SequentialResearchWorkflow",
                    description="Research -> Write -> Review sequential workflow",
                    start_executor=researcher,
                    output_executors=[reviewer],
                )
                .add_edge(researcher, writer)
                .add_edge(writer, reviewer)
                .build()
            )

            print("Workflow built. Starting agent server on :8088...")

            # Turn the workflow into an agent and serve it (listens on :8088).
            agentwf = workflow.as_agent()
            await from_agent_framework(agentwf).run_async()


if __name__ == "__main__":
    asyncio.run(main())
