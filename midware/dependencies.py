"""Shared runtime services used by the current compatibility implementation."""

from midware import runtime

feature_gate = runtime.runtime_manager
model_broker = runtime.model_broker
output_clients = runtime.ws_clients
telemetry_service = runtime.telemetry_service
bot_status_service = runtime.bot_status_service
