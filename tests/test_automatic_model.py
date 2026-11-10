"""Offline checks for the pinned automatic model and its wire token limit."""

import unittest
from unittest.mock import patch, sentinel

from litellm.llms.openrouter.chat.transformation import OpenrouterConfig
from litellm.utils import get_optional_params

import automatic_model


class AutomaticModelTests(unittest.TestCase):
    def test_factory_pins_model_and_bounds_one_paid_attempt(self):
        with patch.object(automatic_model, "GeneralLlm", return_value=sentinel.model) as sdk:
            self.assertIs(automatic_model.build_automatic_model(), sentinel.model)
        sdk.assert_called_once_with(
            model="openrouter/openai/gpt-4.1",
            temperature=0.3,
            timeout=90,
            allowed_tries=1,
            max_tokens=8_192,
        )
        self.assertEqual(automatic_model.AUTOMATIC_MODEL_VERSION, "vrchcab-gpt41-v2")

    def test_locked_sdk_keeps_completion_cap_in_openrouter_request(self):
        model = automatic_model.build_automatic_model()
        self.assertEqual(model.model, automatic_model.AUTOMATIC_MODEL_ID)
        kwargs = dict(model.litellm_kwargs)
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("api_key", kwargs)
        self.assertNotIn("fallbacks", kwargs)
        self.assertEqual(kwargs["max_tokens"], 8_192)

        # Exercise LiteLLM's actual offline parameter mapping and request
        # construction, even with an unknown model in its older local catalog.
        # No completion or HTTP transport is called.
        with patch("litellm.supports_reasoning", return_value=False):
            optional = get_optional_params(
                model="openai/gpt-4.1",
                custom_llm_provider="openrouter",
                max_tokens=kwargs["max_tokens"],
                temperature=kwargs["temperature"],
                drop_params=True,
            )
            body = OpenrouterConfig().transform_request(
                model="openai/gpt-4.1",
                messages=[{"role": "user", "content": "Synthetic offline request."}],
                optional_params=optional,
                litellm_params={},
                headers={},
            )
        self.assertEqual(body["max_tokens"], 8_192)
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("models", body)


if __name__ == "__main__":
    unittest.main()
