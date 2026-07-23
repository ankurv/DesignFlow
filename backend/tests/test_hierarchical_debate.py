"""Unit tests for hierarchical 2-tier debate models and component-level contract extraction."""
from __future__ import annotations

import unittest
from backend.workflow.models import ProposalComponent, ExpertProposal
from backend.orchestration import _extract_markdown_proposal


class TestHierarchicalDebate(unittest.TestCase):

    def test_proposal_component_deep_fields(self):
        comp = ProposalComponent(
            name="AuthService",
            responsibility="Handles user authentication and JWT tokens",
            interfaces=["IAuth"],
            packaging="microservice",
            communication_protocol="protobuf_grpc",
            data_store="PostgreSQL",
            api_contracts=["rpc Authenticate(AuthRequest) returns (AuthResponse)"],
        )
        self.assertEqual(comp.packaging, "microservice")
        self.assertEqual(comp.communication_protocol, "protobuf_grpc")
        self.assertEqual(comp.data_store, "PostgreSQL")
        self.assertEqual(len(comp.api_contracts), 1)

    def test_markdown_component_packaging_and_protocol_extraction(self):
        markdown_text = """
        # High-Level Architecture Design
        
        ## Components
        - **UserGateway**: Microservice handling external HTTP REST endpoints.
        - **NotificationEngine**: Shared library for dispatching email and push alerts with gRPC Protobuf.
        - **CacheModule**: In-process module for memory caching.

        ## Key Decisions
        - **Protocol**: Use Protobuf for internal gRPC communication.

        ## Risks
        - **Latency**: Network overhead between microservices. Mitigation: Use persistent gRPC channels.
        """
        parsed = _extract_markdown_proposal(markdown_text)
        proposal = ExpertProposal.model_validate(parsed)
        self.assertEqual(len(proposal.components), 3)
        
        # UserGateway should be parsed as microservice and rest_json
        gw = next(c for c in proposal.components if "UserGateway" in c.name)
        self.assertEqual(gw.packaging, "microservice")
        self.assertEqual(gw.communication_protocol, "rest_json")
        
        # NotificationEngine should be parsed as library and protobuf_grpc
        ne = next(c for c in proposal.components if "NotificationEngine" in c.name)
        self.assertEqual(ne.packaging, "library")
        self.assertEqual(ne.communication_protocol, "protobuf_grpc")


if __name__ == "__main__":
    unittest.main()
