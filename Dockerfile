# Foundry Hosted Agent image.
# Kept intentionally small and deterministic so the ACR build finishes quickly
# and well within the deployment tool's build-polling window.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/user_agent

# Install dependencies first so this layer can be cached independently of the source.
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy the rest of the application. See .dockerignore for what is excluded
# (keeps both the build context and the ACR source upload small).
COPY . ./

# Foundry Hosted Agents expect the agent server to listen on 8088.
EXPOSE 8088

CMD ["python", "main.py"]
