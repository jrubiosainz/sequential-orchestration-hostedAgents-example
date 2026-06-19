# Copyright (c) Microsoft. All rights reserved.

import logging

from azure.monitor.opentelemetry import configure_azure_monitor
from azure.ai.projects.aio import AIProjectClient
from agent_framework.observability import create_resource, enable_instrumentation

logger = logging.getLogger(__name__)


async def configure_azure_monitor_tracing(project_client: AIProjectClient) -> bool:
    """
    Configure Azure Monitor tracing for the application.

    This enables tracing and sends telemetry to the Application Insights instance
    attached to the Foundry project. It is intentionally best-effort: any failure
    (no Application Insights connected, API mismatch, network issue, ...) is logged
    and the function returns False instead of raising, so the agent server can
    still start.

    Args:
        project_client: The AIProjectClient used to read the connection string.

    Returns:
        True if tracing was configured successfully, False otherwise.
    """
    try:
        conn_string = await project_client.telemetry.get_application_insights_connection_string()

        configure_azure_monitor(
            connection_string=conn_string,
            enable_live_metrics=True,
            resource=create_resource(),
            enable_performance_counters=False,
        )
        # Not required if the environment variable ENABLE_INSTRUMENTATION=true is set.
        enable_instrumentation(enable_sensitive_data=True)

        return True
    except Exception:
        logger.warning(
            "Application Insights tracing was not configured. Ensure Application "
            "Insights is connected to your Foundry project if you want telemetry. "
            "Continuing without tracing.",
            exc_info=True,
        )
        return False
