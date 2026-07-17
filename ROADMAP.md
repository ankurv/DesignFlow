# 🗺️ DesignFlow Product Roadmap & Review

This document tracks the current state of DesignFlow, evaluating its core strengths, identifying its weakest points, and outlining the roadmap for the next phases of development.

## 🌟 Strong Points

1. **State Recovery & Transactional Persistence**
   The shift to SQLite-backed structured checkpoints and decision ledgers is a massive win. Restarting the server no longer destroys the user's run state or token metrics. The application now behaves like a true, durable backend system.

2. **Enterprise-Grade Observability**
   With the introduction of the `AuditLog` and the `DebugObserver`, DesignFlow is uniquely positioned for enterprise deployment. It silently scrubs PII and API keys while maintaining a cryptographic chain of custody for state-changing actions. The failover mechanism for the `_adaptive_discovery_question` ensures that LLM provider outages don't halt the entire pipeline.

3. **Focused, Clean UI/UX**
   By removing the overly complex "Global Agents" architecture and centralizing configuration down to the project level, the UX is much more intuitive. The visual design flow (rendering Mermaid diagrams while hiding prompt clutter) makes the dashboard feel like a premium tool rather than a raw terminal dump.

4. **Multi-Agent Orchestration Engine**
   The core debate loop (Architect Alpha vs Beta + Specialists) is incredibly strong. It forces rigorous constraints (like the max token failsafe) and genuinely evaluates architectural trade-offs rather than blindly agreeing with the user's first prompt.

---

## ⚠️ Weak Points & Technical Debt

1. **Scalability Limitations (Single-Node Bound)**
   Currently, DesignFlow stores sessions in memory (`AuthManager`), uses local SQLite databases per project, and relies on local filesystem directories (`backend/workspace.py`) for I/O. This means the backend **cannot be load-balanced horizontally** across multiple servers. If you deploy this to AWS or GCP, you are restricted to a single monolithic instance.

2. **MCP Execution Is Deliberately Read-Oriented Today**
   DesignFlow now exposes a standard Streamable HTTP MCP server for coding agents. It provides scoped implementation context, validation, activity, and constrained implementation-report write-back. DesignFlow's own debating agents still do not execute arbitrary workspace commands such as `npm audit`, source searches, or cloud API calls. Adding those capabilities requires an explicit permission and sandbox model rather than widening the current safe MCP boundary.

3. **No Monetization or Multi-Tenant SaaS Isolation**
   While we have user login and roles, there is no infrastructure for paywalls, subscription tiers, or strict ephemeral sandboxing (preventing one tenant's project from accessing another tenant's files on the host OS).

4. **Synchronous LLM Bottlenecks**
   Some phases of the orchestrator run sequentially. If multiple specialists are reviewing a design, they block each other. Async streaming and parallel specialist evaluation could vastly reduce the latency of a planning run.

---

## 🚀 Proposed Roadmap (Future Initiatives)

These are the primary options for the next major development focus.

### Track 1: The SaaS & Scalability Overhaul
*Goal: Transition DesignFlow from a local tool to a scalable web platform.*
- [ ] Replace in-memory `AuthManager` with Postgres / Redis.
- [ ] Transition `ProjectStore` SQLite databases to a unified Postgres instance with Row-Level Security (RLS) for multi-tenant isolation.
- [ ] Implement ephemeral, isolated sandbox environments for project files (e.g., using Docker or a remote blob store).

### Track 2: Monetization & Distribution
*Goal: Prepare DesignFlow for commercial release.*
- [ ] Integrate Stripe or LemonSqueezy for API token quota management and paywalls.
- [ ] Finalize the VS Code extension packaging.
- [ ] Create a deployment pipeline (Docker Compose / Helm charts) for enterprise self-hosting.

### Track 3: Deep MCP Integration (Agentic Execution)
*Goal: Make the Virtual Company smarter by giving them active tools.*
- [x] Expose project status, scoped implementation context, validation, and recent activity through Streamable HTTP MCP.
- [x] Add constrained coding-agent write-back for implementation evidence, mismatches, and questions.
- [ ] Surface and resolve implementation reports in the DesignFlow dashboard and decision workflow.
- [ ] Allow the Red Team agent to run security linters on the user's workspace.
- [ ] Allow the Cloud Architect to query live AWS environments to check existing infrastructure constraints.
- [ ] Integrate local bash execution capabilities for the AI to auto-generate scaffolding based on its plans.
