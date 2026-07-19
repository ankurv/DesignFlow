Return exactly: {"kind":"answer|planning_workflow|recovery","reason":"short explanation","answer":"direct project-aware answer or empty string"}

Use answer for questions, explanations, project status, summaries, advice, and discussion that does not require canonical changes, and provide the complete user-facing response in `answer`. The answer must be plain user-facing Markdown with no JSON, routing commentary, or protocol wrapper.

Use planning_workflow whenever the requested outcome creates, reviews, improves, reconciles, or otherwise changes canonical design or planning state, with an empty `answer`. Missing information does not turn a planning request into an answer: the planning workflow owns discovery and user checkpoints.

Use recovery only to continue an existing nonterminal workflow, with an empty `answer`. If routing is uncertain and no canonical change is requested, use answer. Ground answers only in supplied evidence; identify unknowns instead of inventing facts.

Request: ${request}
Current project and workflow state: ${artifact_state}
Known validation state: ${validation_errors}
