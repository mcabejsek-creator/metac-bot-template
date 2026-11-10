"""Pinned, bounded model configuration for automatic forecasting.

Construction performs no model request. The sponsored-credit guard remains the
caller's responsibility; this factory never chooses another model or API key.
"""

from forecasting_tools import GeneralLlm


AUTOMATIC_MODEL_VERSION = "vrchcab-gpt41-v1"
AUTOMATIC_MODEL_ID = "openrouter/openai/gpt-4.1"
AUTOMATIC_MODEL_MAX_TOKENS = 8_192


def build_automatic_model() -> GeneralLlm:
    """Build the verified candidate without invoking it or enabling fallback."""
    return GeneralLlm(
        model=AUTOMATIC_MODEL_ID,
        temperature=0.3,
        timeout=90,
        allowed_tries=1,
        max_tokens=AUTOMATIC_MODEL_MAX_TOKENS,
        # This non-reasoning model preserves the proven V2 temperature.
        # It needs no provider-specific reasoning options. A finite completion
        # cap and one SDK attempt bound work when the provider is unavailable.
    )
