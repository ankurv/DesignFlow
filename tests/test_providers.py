import unittest
from unittest.mock import patch, MagicMock
from backend.agents.base import AgentConfig
from backend.agents.providers import create_agent, discover_models, AWSBedrockAgent

class TestProviders(unittest.TestCase):
    @patch("boto3.client")
    def test_discover_models_aws_bedrock(self, mock_boto):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.list_inference_profiles.return_value = {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": "us.anthropic.claude-3-sonnet-20240229-v1:0",
                    "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0"}],
                },
                {
                    "inferenceProfileId": "us.amazon.titan-text-express-v1",
                    "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-text-express-v1"}],
                }
            ]
        }
        
        config = AgentConfig(name="test", kind="aws-bedrock", api_key="dummy_token", extra={"aws_region": "us-east-1"})
        models = discover_models(config)
        
        self.assertEqual(models, ["us.anthropic.claude-3-sonnet-20240229-v1:0"])
        mock_boto.assert_called_with("bedrock", region_name="us-east-1")
        mock_client.list_inference_profiles.assert_called_once_with(
            typeEquals="SYSTEM_DEFINED", maxResults=100,
        )

    @patch("backend.agents.providers.AWSBedrockAgent._configure_client")
    def test_aws_bedrock_agent_creation(self, mock_configure):
        config = AgentConfig(name="test", kind="aws-bedrock", api_key="dummy_token", extra={"aws_region": "us-west-2"})
        agent = create_agent(config)
        
        self.assertIsInstance(agent, AWSBedrockAgent)
        mock_configure.assert_called_once()

if __name__ == "__main__":
    unittest.main()
