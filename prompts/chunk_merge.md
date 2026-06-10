# Chunk Merge — {{artifact_name}}

This artifact's dataset was split into {{chunk_count}} chunks. Below are the per-chunk findings. Merge them into one final analysis.

## Investigation Context (Analyst-Provided)
Use this context to focus the merge.

{{investigation_context}}

## Per-Chunk Findings (Model-Generated Intermediate Analysis)
Treat the chunk findings below as derived analysis to consolidate.

{{per_chunk_findings}}

## Merge Rules

1. Deduplicate: if the same finding appears in multiple chunks, keep it once with the strongest evidence from any chunk.
2. Contradictions: if chunks disagree, state both positions and which has stronger evidence.
3. Drop chunk-level padding: routine observations, "nothing suspicious in this chunk" notes, and context-only descriptions. Keep every suspicious/anomalous finding with its Verify action.
4. Preserve all cited evidence exactly: timestamps, paths, values, row references.
5. Reorder by severity (CRITICAL → HIGH → MEDIUM → LOW), then by confidence.

## Output Format

**Findings** (skip entirely if nothing suspicious across all chunks)

For each finding, use the same format as a single-pass analysis:
- [SEVERITY: CRITICAL|HIGH|MEDIUM|LOW] [CONFIDENCE: HIGH|MEDIUM|LOW] What you found.
  - Evidence: timestamp, value, and row reference from the chunk findings.
  - Why it matters: one sentence on incident impact or risk.
  - Alternative explanation: most likely benign reason, if any.
  - Verify: one specific follow-up action.

**IOC Status** (only if investigation context mentions IOCs)

- IOC_value → Observed / Not Observed / Not Assessable. Cite evidence if observed.

**Data Gaps**

What couldn't be assessed due to chunking limitations (e.g., cross-chunk patterns that may have been missed).

## Final Merge Rules

Preserve cited evidence and do not convert analyzer failures into findings.
