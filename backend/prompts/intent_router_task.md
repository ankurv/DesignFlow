Classify this DesignFlow request by intended outcome, not keyword matching.

Return exactly: {"kind":"chat|artifact_edit|planning_workflow","target_artifacts":["DESIGN.md|PLAN.md|DECISIONS.md"],"reason":"short explanation"}

Use chat for explanation or advice without canonical changes. Use artifact_edit only for a narrowly bounded change to explicitly identified files. Use planning_workflow for broad creation, refinement, reconciliation, completion, or review. When artifacts fail validation and improvement is requested, select planning_workflow.

Request: ${request}
Artifact presence: ${artifact_state}
Validation failures: ${validation_errors}
