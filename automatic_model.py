"""Pinned, bounded model configuration for automatic forecasting.

Construction performs no model request. The sponsored-credit guard remains the
caller's responsibility; this factory never chooses another model or API key.
"""

from forecasting_tools import GeneralLlm


AUTOMATIC_MODEL_VERSION = "vrchcab-gemini37-v1"
AUTOMATIC_MODEL_ID = "openrouter/google/gemini-3.7-flash"
AUTOMATIC_MODEL_MAX_TOKENS = 16_384


def build_automatic_model() -> GeneralLlm:
    """Build the verified candidate without invoking it or enabling fallback."""
    return GeneralLlm(
        model=AUTOMATIC_MODEL_ID,
        temperature=None,
        timeout=180,
        allowed_tries=1,
        max_tokens=AUTOMATIC_MODEL_MAX_TOKENS,
        # OpenRouter's public model metadata on 2026-11-10 declares mandatory
        # reasoning with default effort "medium". Omit reasoning_effort because
        # the pinned LiteLLM catalog may silently drop it for this newer model.
        # max_tokens caps total completion tokens, including internal reasoning.
    )
