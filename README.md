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
3. You have a **model deployment** in that Foundry project (any chat model).

> Agent definitions are **not** required for this example — the workflow drives
> the model deployment directly. The executor steps (`Researcher`, `Writer`,
> `Reviewer`) are workflow stages, not references to portal-defined agents, so
> the names of the agents already in your project do not matter here.

## Configure

Create a `.env` file in the repo root (it is git-ignored and is **not** baked
into the image — you provide it locally). Copy `.env.example`:

```env
AZURE_AI_PROJECT_ENDPOINT=<your-foundry-project-endpoint>
AZURE_AI_MODEL_DEPLOYMENT_NAME=<your-model-deployment-name>
```

## Deploy

1. Open this folder in VS Code.
2. In the **Foundry** extension, click **Deploy** (Hosted Agent).
3. When prompted, keep the build context at the repo root and use the
   `Dockerfile` in the root.

The extension builds the image in Azure Container Registry, pushes it, and
creates the hosted agent. With the slimmed dependencies (see below) the ACR
build completes well within the extension's build-polling window.

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
| `agent.yaml` | Hosted-agent name and compute (CPU/memory). |
