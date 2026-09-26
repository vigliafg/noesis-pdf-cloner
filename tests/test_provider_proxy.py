"""Tests per ``tools/provider_proxy.py`` (proxy provider model-aware).

Carica il modulo dal file (non è un package) e verifica che il pin ``provider``
sia applicato **solo** ai modelli scelti, lasciando passare gli altri invariati.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "provider_proxy", _ROOT / "tools" / "provider_proxy.py"
)
provider_proxy = importlib.util.module_from_spec(_spec)
sys.modules["provider_proxy"] = provider_proxy
_spec.loader.exec_module(provider_proxy)


class ProviderProxyTests(unittest.TestCase):
    def setUp(self):
        self._models = provider_proxy.PROXY_MODELS
        provider_proxy.PROXY_MODELS = ("openai/gpt-oss-120b",)
        provider_proxy.PROXY_PROVIDER = "groq"

    def tearDown(self):
        provider_proxy.PROXY_MODELS = self._models

    def test_should_pin_selected_model(self):
        self.assertTrue(provider_proxy._should_pin("openai/gpt-oss-120b"))

    def test_should_not_pin_other_models(self):
        for model in (
            "deepseek/deepseek-chat",
            "google/gemini-2.5-flash",
            "inception/mercury-2.5",
        ):
            self.assertFalse(provider_proxy._should_pin(model), model)

    def test_inject_adds_provider_for_selected_model(self):
        payload = provider_proxy._inject_provider(
            {"model": "openai/gpt-oss-120b", "messages": []}
        )
        self.assertEqual(
            payload["provider"], {"only": ["groq"], "allow_fallbacks": False}
        )

    def test_passthrough_other_models_unchanged(self):
        body = {"model": "deepseek/deepseek-chat", "messages": []}
        payload = provider_proxy._inject_provider(body)
        self.assertNotIn("provider", payload)

    def test_passthrough_preserves_existing_provider(self):
        body = {"model": "x/y", "provider": {"order": ["a"]}}
        payload = provider_proxy._inject_provider(body)
        self.assertEqual(payload["provider"], {"order": ["a"]})

    def test_star_pins_all(self):
        provider_proxy.PROXY_MODELS = ("*",)
        self.assertTrue(provider_proxy._should_pin("google/gemini-2.5-flash"))
        payload = provider_proxy._inject_provider({"model": "x/y"})
        self.assertEqual(payload["provider"]["only"], ["groq"])

    def test_does_not_mutate_input(self):
        body = {"model": "openai/gpt-oss-120b", "messages": []}
        provider_proxy._inject_provider(body)
        self.assertNotIn("provider", body)  # l'originale resta intatto


if __name__ == "__main__":
    unittest.main()
