Refine canonical artifacts using unresolved critiques and current user steering.
Human steering: ${steering}
Capability catalog:
${capabilities}

Return only the affected complete H2 sections under ## DESIGN_UPDATE, ## PLAN_UPDATE, and/or ## DECISIONS_UPDATE. Every returned section must begin with its exact original `## Heading`. **NEVER rename or alter the text of an existing heading**, as this creates duplicates in the database. Omitted sections remain unchanged in SQLite. Reconcile directives and contradictions, preserve optionality, map every requirement to bounded work and acceptance evidence, and split overloaded tasks. Never complete with Pending choices or references to nonexistent checkpoints. If a material user choice remains, emit exactly one ## DECISION_CHECKPOINT.

The returned H2 sections are typed database upserts, not complete documents and not an append-only recap. Do not repeat unaffected sections. A checkpoint must ask one explicit question, give 2-3 options with concrete consequences, and avoid unsupported claims that one authentication channel or technology is inherently more secure.

Audit `## Capability Behavioral Contracts`. Each selected `### capability.id` must name and resolve every required decision dimension and contain explicit `Decisions`, `Failure states`, `Implementation`, and `Acceptance` labels. Its ID must appear in PLAN.md Requirement Traceability. Repair shallow statements such as “use authentication” that omit lifecycle semantics.

Deterministic quality feedback:
${quality_feedback}
