"""Routing decisions: resolve what the user said to an agent (router, embeddings),
check tool calls before they run (gate), and choose connect vs dispatch,
indirect routing, notify and wake rules (policy). Pure Python; no Pipecat."""
