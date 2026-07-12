# 🚀 DesignFlow: The AI Architecture Design Studio

## What is DesignFlow?
DesignFlow is a premium, interactive **AI Architect Dashboard**. Instead of letting AI blindly generate codebases, DesignFlow focuses exclusively on the critical initial step of software development: **Architecture, Strategy, and Planning**. 

You feed it an idea, and it orchestrates a multi-model debate among specialized AI personas to produce a structured technical planning baseline. You can export that baseline to a coding agent such as Cursor, Codex, or Windsurf, then refine the plan as implementation reveals new constraints and opportunities.

---

## 🏗️ Core Architecture & Implemented Features

### 1. The Dynamic Agent Factory (Virtual Company)
Gone are the days of manually configuring 15 different agents. The DesignFlow backend acts as a dynamic factory. By providing just a single Base Provider API Key, the Orchestrator instantly spawns an entire **15-person Virtual Company** containing highly specialized personas:
- **Architect Alpha & Beta**: Two distinct brains forced to propose competing architectures and debate trade-offs.
- **Red Team**: Hunts for edge cases, security flaws, and race conditions.
- **UX Simplifier**: Fiercely advocates for the external user, fighting to simplify complex UI flows.
- **Product Manager**: Enforces strict MVP constraints and fights scope-creep ("YAGNI").
- **Cloud & Data Architects**: Obsess over schema design, DB normalization, and AWS/GCP infrastructure.
- **Sales & Marketing (Alpha/Beta)**: Competing strategists pitching aggressive viral loops vs. calculated B2B sales funnels.
- **Silent Researcher**: Reads your existing codebase in the background to ensure the other models don't hallucinate APIs.

### 2. Cross-Model Debates (Model Agnostic)
DesignFlow natively supports cross-model intelligence. If you provide multiple API keys (e.g., Claude, OpenAI, Groq, Gemini), the backend distributes specialized personas across your configured providers.
This means you can watch **Claude** (playing the UX Simplifier) challenge **OpenAI** (playing the Red Team) to uncover trade-offs and strengthen the planning baseline.

For multi-agent planning runs, DesignFlow requires contributions from up to three distinct relevant specialists before the planning baseline can complete. Material choices must compare credible alternatives, and approval-enabled runs require at least one user-confirmed decision checkpoint so the team does not silently lock in a high-impact assumption.

When a provider key is configured, DesignFlow can query its live model catalog. The selected model remains preferred, while generated specialists rotate across other compatible discovered models to increase reasoning diversity within the same provider.

### 3. Exhaustive Tree-Based Planning
The Orchestrator forces the Virtual Company through a rigorous two-tiered design phase:
1. **High-Level Strategy**: Debating the architecture, tech stack, and scalability, outputting a beautiful `DESIGN.md` complete with Mermaid.js flowcharts.
2. **Implementation Starting Plan**: The Orchestrator expands the high-level direction into sequenced, checkable work, acceptance criteria, known unknowns, and discovery checkpoints. `PLAN.md` is intentionally a strong starting point rather than a claim that implementation discovery is finished.

### 4. Enterprise API Routing
DesignFlow supports enterprise-grade connections out of the box:
- **Foundry / Custom Endpoints**: Configurable `base_url` for OpenAI-compatible endpoints.
- **AWS Bedrock & GCP Vertex AI**: Native Anthropic SDK overrides to securely route Claude traffic through enterprise cloud platforms without manual proxying.

### 5. Developer-Focused UI/UX
- **Monaco Editor Integration**: The internal text editor is powered by Monaco (the engine behind VS Code) for a premium, syntax-highlighted project viewing experience.
- **Streamlined Debate Controls**: Complex agent settings were replaced with a simple "Debate Level" slider to control the depth of the plan.
- **Max Token Failsafe**: A hard Token Limit input allows the Orchestrator to monitor `input + output` tokens across all 15 agents and pull the emergency brake if a debate spirals, saving API costs.
- **1-Click Context Export**: A dedicated button bundles `DESIGN.md`, `PLAN.md`, `DECISIONS.md`, and unresolved questions into a planning-baseline package for a coding agent.

---

## 🎨 Extending the Virtual Company (Custom Personas)

DesignFlow is designed to be easily extensible. If your project requires a highly specific domain expert (e.g., a *Legal Compliance Officer* or a *Game Economy Balancer*), you can easily inject them into the Virtual Company.

### How to add custom personas:
1. Open `backend/orchestrator.py`.
2. Locate the `SPECIALIZED_PERSONAS` dictionary at the top of the file.
3. Add a new key for your role and write a strict, focused system prompt.
   ```python
   SPECIALIZED_PERSONAS = {
       # ... existing roles ...
       "legal_officer": "You are the LEGAL COMPLIANCE OFFICER. Your sole job is to review the architecture for GDPR, HIPAA, and CCPA violations. Aggressively flag any data storage designs that expose PII."
   }
   ```
4. **That's it!** The Agent Factory will automatically pick up your new role, assign it a Base Provider (via the round-robin distribution), and make it available for the AI Coordinator to summon during the debate phase.
