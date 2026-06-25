# Sequential orchestration — Foundry Hosted Agent example

A minimal, deploy-ready example of a **Microsoft Agent Framework** sequential
workflow (`Researcher -> Writer -> Reviewer`) packaged as a **Foundry Hosted
Agent**. Clone it, set two environment variables, click **Deploy** in the
AI Toolkit / Foundry extension for VS Code, and the agent shows up in your
Foundry project.

This repo is a hardened fork of
[`dsanchor/sequential-orchestration-writer`](https://github.com/dsanchor/sequential-orchestration-writer).
See [What was fixed](#what-was-fixed) for the details.

## Prerequisites

1. Sign in with the **Azure** extension in VS Code.
2. Set a **default project** in the Foundry extension.
3. You have a **model deployment** in that Foundry project. Use a **fast,
   non-reasoning chat model** (e.g. `gpt-4.1-mini`, `gpt-4o-mini`, `gpt-4o`).
   See the note under [Configure](#configure) about why a slow *reasoning*
   model (e.g. `gpt-5`) makes the Playground time out.

> Agent definitions are **not** required for this example — the workflow drives
> the model deployment directly. The executor steps (`Researcher`, `Writer`,
> `Reviewer`) are workflow stages, not references to portal-defined agents, so
> the names of the agents already in your project do not matter here.

## Configure

Create a `.env` file in the repo root (it is git-ignored and is **not** baked
into the image — you provide it locally). Copy `.env.example`:

```env
AZURE_AI_PROJECT_ENDPOINT=<your-foundry-project-endpoint>
AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-4.1-mini
```

> **Use a fast (non-reasoning) model.** The workflow runs three agents in
> sequence and the Foundry **Playground streams** the answer over a live
> connection. A *reasoning* model such as `gpt-5` can spend ~45s per call
> "thinking" before emitting any tokens; across three sequential agents that
> silence makes the Playground connection drop with a **"network error" /
> "Session not started"**. A fast model (`gpt-4.1-mini`, `gpt-4o-mini`,
> `gpt-4o`) keeps the whole run to a few seconds and streams smoothly.

> **How these reach the running container.** The hosted runtime does **not**
> read your `.env` file. `agent.yaml` declares an `environment_variables:` block
> whose `${...}` references point at the two keys above. At deploy time the
> Foundry extension resolves them **from your local `.env`** and bakes the
> resolved values into the new agent version. Keep the variable **names** in
> `.env` exactly as shown so the `${...}` references resolve.

## Deploy

1. Open this folder in VS Code.
2. In the **Foundry** extension, click **Deploy** (Hosted Agent).
3. When prompted, keep the build context at the repo root and use the
   `Dockerfile` in the root.

The extension builds the image in Azure Container Registry, pushes it, and
creates the hosted agent. With the slimmed dependencies (see below) the ACR
build completes well within the extension's build-polling window.

> **Env vars are immutable per version.** If you change a value in `.env`, you
> must **redeploy** — each deployment creates a new immutable agent version with
> the resolved environment variables baked in.

### Grant the agent permission to call the model

After the first deployment, give the agent identity (or, for older setups, the
Foundry **project managed identity**) the **Azure AI User** role (recently
renamed **Foundry User**) on the Foundry project so it can invoke the model.
You can do this from the Azure portal → your Foundry project → **Access control
(IAM)** → **Add role assignment**.

## What was fixed

The original repo failed to deploy with:

```
Build status polling timed out after 15 attempts ...
```

Root cause and fixes:

| Problem | Fix |
| --- | --- |
| **The Playground showed "network error" / "Session not started"** even though the agent deployed and ran. The workflow was built from custom `Executor` subclasses that called the **blocking** `agent.run(...)` and only `yield`ed once, at the very end. The Playground opens a **streaming** connection, so it received **no bytes** for the whole run and the connection timed out. (App Insights confirmed a ~137s run with all updates dumped at the end.) | The three agents are now wired **directly** into `WorkflowBuilder` (`start_executor=researcher … add_edge … output_executors=[reviewer]`). The framework auto-wraps each agent in a streaming `AgentExecutor` that emits incremental updates while the model generates, which `from_agent_framework(...)` forwards as SSE deltas — keeping the connection alive. This matches the official multi-agent hosted-agent sample. Pair it with a **fast model** (see [Configure](#configure)). | (`ValueError: AZURE_AI_PROJECT_ENDPOINT environment variable is required`), so readiness never returned 200 and the agent showed `session_not_ready`. The deploy log said *"No environment variables found in agent.yaml"*. The hosted runtime does **not** read the local `.env` — env vars must be declared in `agent.yaml`. | Added an `environment_variables:` block to `agent.yaml` that injects `AZURE_AI_PROJECT_ENDPOINT` and `AZURE_AI_MODEL_DEPLOYMENT_NAME` into the container (resolved from your `.env` at deploy time). |
| `requirements.txt` pinned the umbrella **`agent-framework==1.0.0rc3`**, which resolves to `agent-framework-core[all]` and installs **every** optional integration (a2a, copilotstudio, devui, redis, mem0, anthropic, ollama, …). The huge install made the ACR build run for minutes and time out. | Depend only on `agent-framework-core`, `agent-framework-azure-ai`, the agent-server adapter, and the observability package — all pinned so pip resolves with no backtracking. |
| The whole repo (`.git`, `images/`, `.devcontainer/`, `.foundry/`, …) was uploaded and `COPY`-ed into the image, bloating the build context. | Added a `.dockerignore` so only the app is uploaded/copied. |
| `main` returned **before starting the server** when Application Insights was not connected, so the container exited and the agent never appeared in Foundry. | Observability is now **best-effort**; the agent server always starts. |
| `azure-ai-projects` / `azure-identity` were imported but not declared. | They are pulled in (pinned) transitively by `agent-framework-core`. |
| Dockerfile rebuilt deps on every source change and kept pip caches. | Dependencies are installed in their own cached layer with `--no-cache-dir` and `pip` upgraded first. |

## Local run (optional)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# populate .env first
python main.py   # serves the agent on http://localhost:8088
```

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Builds the sequential workflow and serves it via `from_agent_framework(...)`. |
| `observability.py` | Best-effort Azure Monitor / Application Insights tracing. |
| `requirements.txt` | Minimal, pinned dependency set (the key deployment fix). |
| `Dockerfile` | Small, deterministic image; listens on `:8088`. |
| `.dockerignore` | Keeps the build context / ACR upload small. |
| `agent.yaml` | Hosted-agent name, protocol, compute (CPU/memory), and the `environment_variables:` block that feeds config into the container. |
