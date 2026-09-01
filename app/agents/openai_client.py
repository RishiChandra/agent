"""Chat-completion adapter kept for backward compatibility with ``call_openai``.

Historically this module wrapped Azure OpenAI and read ``AZURE_OPENAI_*`` from
the environment at import time, which made the whole app crash at boot when
those keys were absent. It is now a thin adapter over ``agents.gemini_client``
(``GEMINI_TEXT_MODEL``, ``GOOGLE_API_KEY``/``GEMINI_API_KEY``) and touches the
environment only when a function is actually called.

The public surface is unchanged:

* ``call_openai(messages, tools=None)`` -> object exposing
  ``response.choices[0].message.content`` and
  ``response.choices[0].message.tool_calls[i].function.{name, arguments}``.
  ``messages`` is the OpenAI ``{"role", "content"}`` list; ``tools`` is the
  OpenAI ``{"type": "function", "function": {...}}`` list. When ``tools`` is
  given a tool call is forced (the legacy ``tool_choice="required"``).
* ``get_openai_client()``, ``get_deployment_name()``, ``get_api_version()``.

Optional escape hatch: ``LLM_PROVIDER=azure_openai`` (with
``AZURE_OPENAI_ENDPOINT`` and ``AZURE_OPENAI_API_KEY``) routes calls to Azure
OpenAI instead. The ``openai`` SDK is imported lazily, so it is only needed
when that provider is explicitly selected.
"""
import os

from . import gemini_client

PROVIDER_GEMINI = "gemini"
PROVIDER_AZURE_OPENAI = "azure_openai"

# Azure OpenAI settings — used only when LLM_PROVIDER=azure_openai.
AZURE_DEPLOYMENT = "gpt-4.1-nano"
AZURE_API_VERSION = "2024-12-01-preview"


def get_provider():
    """Active LLM provider: ``gemini`` (default) or ``azure_openai``."""
    return (os.environ.get("LLM_PROVIDER") or PROVIDER_GEMINI).strip().lower()


def call_openai(messages, tools=None):
    """Run a chat completion and return an OpenAI-shaped response object.

    With ``tools`` a tool call is forced, matching the legacy
    ``tool_choice="required"``; without ``tools`` the model answers in text.
    """
    if get_provider() == PROVIDER_AZURE_OPENAI:
        return _call_azure_openai(messages, tools)

    # Gemini: tool_choice="any" forces a function call, like OpenAI's "required".
    # call_gemini ignores tool_choice when no tools are supplied.
    response = gemini_client.call_gemini(messages, tools, tool_choice="any")
    return gemini_client.gemini_response_to_openai_like(response)


def get_openai_client():
    """Return a configured client for the active provider.

    Raises:
        ValueError: If the active provider's credentials are not set.
    """
    if get_provider() == PROVIDER_AZURE_OPENAI:
        return _get_azure_openai_client()
    return gemini_client.get_gemini_client()


def get_deployment_name():
    """Model/deployment name used by ``call_openai`` for the active provider."""
    if get_provider() == PROVIDER_AZURE_OPENAI:
        return AZURE_DEPLOYMENT
    return gemini_client.get_model_name()


def get_api_version():
    """Azure OpenAI API version (only meaningful for LLM_PROVIDER=azure_openai)."""
    return AZURE_API_VERSION


# ---------------------------------------------------------------------------
# Azure OpenAI (opt-in)
# ---------------------------------------------------------------------------

def _get_azure_openai_client():
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not endpoint or not api_key:
        raise ValueError(
            "LLM_PROVIDER=azure_openai requires AZURE_OPENAI_ENDPOINT and "
            "AZURE_OPENAI_API_KEY environment variables"
        )
    # Lazy import: the openai SDK is optional and only needed for this provider.
    from openai import AzureOpenAI

    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=AZURE_API_VERSION,
    )


def _call_azure_openai(messages, tools=None):
    client = _get_azure_openai_client()
    kwargs = {"model": AZURE_DEPLOYMENT, "messages": messages}
    if tools is not None:
        kwargs.update(tools=tools, tool_choice="required")
    return client.chat.completions.create(**kwargs)
