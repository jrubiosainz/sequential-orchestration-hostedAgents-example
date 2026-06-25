# Sequential orchestration — Foundry Hosted Agent example

A minimal, deploy-ready example of a **Microsoft Agent Framework** sequential
orchestration (`Researcher -> Writer -> Reviewer`) packaged as a **Foundry
Hosted Agent**. The **Researcher searches the web with Grounding with Bing
Search**, so answers are current and sourced. Clone it, set three environment
variables, click **Deploy** in the AI Toolkit / Foundry extension for VS Code,
and the agent shows up in your Foundry project and streams live in the Playground.

This repo is a hardened fork of
[`dsanchor/sequential-orchestration-writer`](https://github.com/dsanchor/sequential-orchestration-writer).
See [What was fixed](#what-was-fixed) for the details.

## Prerequisites

1. Sign in with the **Azure** extension in VS Code.
2. Set a **default project** in the Foundry extension.
3. You have a **`gpt-5` model deployment** in that Foundry project. The
   Researcher uses the **Bing Grounding** tool, which is only available through
   the **Responses API**; `gpt-5` supports it. (The streaming design below keeps
   the Playground connection alive during the model's "thinking" time, so a
   reasoning model is fine.)
4. You have a **Grounding with Bing Search** connection in the project, and you
   know its full connection id (see [Configure](#configure)).

> Agent definitions are **not** required for this example — the orchestrator
> drives the model deployment directly. The steps (`Researcher`, `Writer`,
> `Reviewer`) are **local, in-memory** `Agent` objects that each call your model
> deployment; they are **not** created in your Foundry *Agents* list and are
> **not** references to portal-defined agents, so the names of the agents
> already in your project do not matter here. The only thing that appears in
> Foundry is the hosted-agent container that wraps the orchestration.

## Configure

Create a `.env` file in the repo root (it is git-ignored and is **not** baked
into the image — you provide it locally). Copy `.env.example`:

```env
AZURE_AI_PROJECT_ENDPOINT=<your-foundry-project-endpoint>
AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-5
BING_GROUNDING_CONNECTION_ID=<your-bing-grounding-connection-id>
```

> **Bing grounding (so the Researcher can search the web).**
> `BING_GROUNDING_CONNECTION_ID` is the **full ARM id** of your *Grounding with
> Bing Search* connection. Find it in the Foundry portal → your project →
> **Management center** → **Connected resources** → your Bing connection. It
> looks like
> `/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>/connections/<name>`.
> If you leave it empty the agent still runs, but the Researcher answers from the
> model's **training data only** (stale — e.g. it returns the old World Cup
> top-scorer record instead of the current one).

> **Why streaming doesn't time out with a reasoning model.** `gpt-5` doing a
> Bing search can stay silent ~45-50s before its first token. The orchestrator
> sends an immediate first byte and a tiny whitespace **heartbeat** every few
> seconds during any silent gap, so the Foundry **Playground**'s live connection
> never drops with a **"network error"**. (See [What was fixed](#what-was-fixed).)

> **How these reach the running container.** The hosted runtime does **not**
> read your `.env` file. `agent.yaml` declares an `environment_variables:` block
> whose `${...}` references point at the three keys above. At deploy time the
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
| **The Researcher answered from stale training data** (e.g. the old FIFA World Cup top-scorer record) and never searched the web, even though the project has a *Grounding with Bing Search* connection. The original code attached **no** web-search tool. | The Researcher now gets a **Bing Grounding** tool. Two details were the actual fix: (1) it must be attached through the **Responses API** (`AzureOpenAIResponsesClient`) — `gpt-5` rejects the classic Agent-Service tool with *"This model only supports Responses API compatible tools"*; (2) the Responses-API tool shape uses `project_connection_id` (not `connection_id`): `{"type":"bing_grounding","bing_grounding":{"search_configurations":[{"project_connection_id":"<full id>"}]}}`. Set `BING_GROUNDING_CONNECTION_ID` to enable it. |
| **The Playground showed "network error" / "Session not started"** even though the agent deployed and ran. The original used a `WorkflowBuilder`; the workflow runner executes each step as a Pregel **superstep** and only flushes that step's output when the superstep **finishes**, so the streaming Playground received **no bytes** until the slow (Bing-powered) Researcher completed ~50-60s in, and the live connection timed out. | Replaced the workflow with a custom **`SequentialOrchestratorAgent(BaseAgent)`** that runs the three agents itself. A `BaseAgent` is served through the AIAgent adapter, which forwards `run(stream=True)` updates **update-by-update with no buffering**. The orchestrator streams each agent with `stream=True` and emits an immediate first byte plus a whitespace **heartbeat** every few seconds during silent gaps, so the connection stays warm even while `gpt-5` runs its Bing search. |
| The agent crashed at startup with `ValueError: AZURE_AI_PROJECT_ENDPOINT environment variable is required`, so readiness never returned 200 and the agent showed `session_not_ready`. The hosted runtime does **not** read the local `.env` — env vars must be declared in `agent.yaml`. | Added an `environment_variables:` block to `agent.yaml` that injects `AZURE_AI_PROJECT_ENDPOINT`, `AZURE_AI_MODEL_DEPLOYMENT_NAME` and `BING_GROUNDING_CONNECTION_ID` into the container (resolved from your `.env` at deploy time). |
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
| `main.py` | Builds the `Researcher -> Writer -> Reviewer` orchestrator (with Bing grounding + live streaming) and serves it via `from_agent_framework(...)`. |
| `observability.py` | Best-effort Azure Monitor / Application Insights tracing. |
| `requirements.txt` | Minimal, pinned dependency set (the key deployment fix). |
| `Dockerfile` | Small, deterministic image; listens on `:8088`. |
| `.dockerignore` | Keeps the build context / ACR upload small. |
| `agent.yaml` | Hosted-agent name, protocol, compute (CPU/memory), and the `environment_variables:` block that feeds config into the container. |
