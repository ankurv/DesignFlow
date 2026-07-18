import unittest
from unittest.mock import patch, MagicMock
from backend.agents.base import AgentConfig
from backend.agents.providers import create_agent, discover_models, AWSBedrockAgent

class TestProviders(unittest.TestCase):
    @patch("boto3.client")
    def test_discover_models_aws_bedrock(self, mock_boto):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.list_foundation_models.return_value = {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-3-sonnet-20240229-v1:0",
                    "modelLifecycle": {"status": "ACTIVE"},
                    "providerName": "Anthropic"
                },
                {
                    "modelId": "amazon.titan-text-express-v1",
                    "modelLifecycle": {"status": "ACTIVE"},
                    "providerName": "Amazon"
                }
            ]
        }
        
        config = AgentConfig(name="test", kind="aws-bedrock", api_key="dummy_token", extra={"aws_region": "us-east-1"})
        models = discover_models(config)
        
        # Should only return active Anthropic models from Bedrock
        self.assertEqual(models, ["anthropic.claude-3-sonnet-20240229-v1:0"])
        mock_boto.assert_called_with("bedrock", region_name="us-east-1")

    @patch("backend.agents.providers.AWSBedrockAgent._configure_client")
    def test_aws_bedrock_agent_creation(self, mock_configure):
        config = AgentConfig(name="test", kind="aws-bedrock", api_key="dummy_token", extra={"aws_region": "us-west-2"})
        agent = create_agent(config)
        
        self.assertIsInstance(agent, AWSBedrockAgent)
        mock_configure.assert_called_once()

if __name__ == "__main__":
    unittest.main()
