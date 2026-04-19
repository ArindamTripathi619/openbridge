
import unittest
from unittest.mock import patch, MagicMock
from telewatch.analyzers.llm_client import create_llm_client, OpenAIClient, LLMError, ROTATOR_PRESETS

class TestLocalRotatorIntegration(unittest.TestCase):
    """Test integration with local LLM rotator."""

    def test_rotator_presets_exist(self):
        """Test that ROTATOR_PRESETS is defined and has expected keys."""
        self.assertIn("Groq", ROTATOR_PRESETS)
        self.assertIn("Mistral", ROTATOR_PRESETS)
        self.assertIn("OpenRouter", ROTATOR_PRESETS)
        self.assertTrue(len(ROTATOR_PRESETS["Groq"]) >= 7)

    def test_create_local_rotator_with_preset(self):
        """Test creating a client using one of the new presets."""
        preset_model = ROTATOR_PRESETS["Mistral"][0][0] # mistral-large
        config = {
            "provider": "local-rotator",
            "model": preset_model,
            "base_url": "http://localhost:8000/v1"
        }
        
        client = create_llm_client(config)
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.model, preset_model)

    def test_create_local_rotator_client_default(self):
        """Test that local-rotator provider creates an OpenAIClient with correct base_url."""
        config = {
            "provider": "local-rotator",
            "model": "groq-llama",
            "base_url": "http://localhost:8888/v1"
        }
        
        client = create_llm_client(config)
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.model, "groq-llama")
        self.assertEqual(client.base_url, "http://localhost:8888/v1")

    @patch('openai.OpenAI')
    def test_openai_client_custom_base_url(self, mock_openai):
        """Test that OpenAIClient uses the provided base_url."""
        from telewatch.analyzers.llm_client import OpenAIClient
        client = OpenAIClient(api_key="test-key", base_url="http://custom:8000/v1")
        mock_openai.assert_called_once_with(api_key="test-key", base_url="http://custom:8000/v1")

    def test_local_rotator_handles_connection_error(self):
        """Test that local rotator correctly identifies connection errors to its base_url."""
        config = {
            "provider": "local-rotator",
            "base_url": "http://localhost:9999/v1" # Non-existent port
        }
        
        client = create_llm_client(config)
        # Directly mock the completions.create method on the client instance
        client.client.chat.completions.create = MagicMock(side_effect=Exception("Connection refused at localhost:9999"))
        
        with self.assertRaises(LLMError) as cm:
            client.analyze("test")
        
        self.assertIn("CONNECTION_ERROR", str(cm.exception))
        self.assertIn("http://localhost:9999/v1", str(cm.exception))

if __name__ == "__main__":
    unittest.main()
