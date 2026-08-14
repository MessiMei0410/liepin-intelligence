from __future__ import annotations

import unittest
from pathlib import Path
from _local import env_path, skip_unless_local


BUILDER_PATH = env_path("ASA_BUILDER_PATH", Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py"))


@skip_unless_local(BUILDER_PATH, "build_talent_workbench.py 脚本")
class FlowQueueContinuationTest(unittest.TestCase):
    def test_overview_queue_opens_candidate_batch_workspace(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")

        self.assertIn("function openCandidateBatch(client, job, queue)", source)
        self.assertIn("return openCandidateBatch(client, job, queue);", source)
        self.assertIn("activateWorkbenchTab('candidates');\n  renderCandidates();", source)
        self.assertIn('id=\"candidateQueueRows\"', source)
        self.assertIn("function candidateQueueTalents()", source)
        self.assertIn("function candidateQueueFlowForTalent(talent)", source)
        self.assertIn("action.type === 'p0' && action.operation === 'flow'", source)
        self.assertIn("openCandidateBatch(action.client || '', action.job || '', action.queue || '有效人选')", source)
        self.assertIn("stats.waitingReply ? '全部待回复' : '有效人选'", source)
        self.assertIn("queue === '全部回复' ? 'reply'", source)
        self.assertIn("queue === '全部待回复' ? 'waiting'", source)

    def test_review_resumes_candidate_queue_at_next_candidate(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")

        self.assertIn("const FLOW_QUEUE_CONTINUATION_KEY", source)
        self.assertIn("function saveFlowQueueContinuation(candidate, result)", source)
        self.assertIn("sessionStorage.setItem(FLOW_QUEUE_CONTINUATION_KEY", source)
        self.assertIn("continueCandidateBatchAfterReview(c, result);", source)
        self.assertIn("function restoreFlowQueueContinuation()", source)
        self.assertIn("sessionStorage.removeItem(FLOW_QUEUE_CONTINUATION_KEY)", source)
        self.assertIn("surface: candidateQueueFocus ? 'candidates' : 'flow'", source)
        self.assertIn("if (state.surface === 'candidates')", source)
        self.assertIn("const nextFlow = candidateQueueFlows().find(c =>", source)
        self.assertIn("String(c.jobCandidateId) !== String(state.completedJobCandidateId)", source)
        self.assertIn("row?.classList.add('queue-next-row')", source)
        self.assertIn("renderAll();\nif (!restoreCandidateBatchSession()) restoreFlowQueueContinuation();", source)

    def test_review_does_not_reload_between_candidates(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        review_source = source.split("function openReviewAction(id, result)", 1)[1].split(
            "function wireFlowActions", 1
        )[0]

        self.assertNotIn("location.reload()", review_source)
        self.assertIn("continueCandidateBatchAfterReview(c, result);", review_source)
        self.assertIn("function continueCandidateBatchAfterReview(candidate, result)", source)

    def test_effective_batch_excludes_candidates_completed_in_current_batch(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")

        self.assertIn("let candidateQueueCompletedIds = new Set();", source)
        self.assertIn("candidateQueueCompletedIds = new Set();", source)
        self.assertIn("!candidateQueueCompletedIds.has(String(c.jobCandidateId))", source)
        self.assertIn("candidateQueueCompletedIds.add(String(candidate.jobCandidateId));", source)

    def test_candidate_detail_exposes_queue_actions(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")

        self.assertIn("const queueFlow = candidateQueueFlowForTalent(c);", source)
        self.assertIn("批次处理中", source)
        self.assertIn("${{flowActionButtons(queueFlow)}}", source)
        self.assertIn("wireFlowActions(detail);", source)
        self.assertIn("data-flow-evidence", source)
        self.assertIn("data-flow-correction", source)

    def test_legacy_reply_task_uses_unique_masked_name_with_profile_evidence(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")

        self.assertIn('"candidateTitle": clean(row["source_candidate_title"])', source)
        self.assertIn("candidate_replies source_reply", source)
        self.assertIn("title: task.candidateTitle", source)
        self.assertIn("function candidateTargetNamesCorrespond", source)
        self.assertIn("const evidenceMatches = Boolean(", source)
        self.assertIn("return safeMatches.length === 1 ? safeMatches[0] : null;", source)

    def test_queue_continuation_survives_duplicate_live_refresh(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        restore_source = source.split("function restoreFlowQueueContinuation()", 1)[1].split(
            "function activateQueueItem", 1
        )[0]

        self.assertNotIn("sessionStorage.removeItem(FLOW_QUEUE_CONTINUATION_KEY)", restore_source)
        self.assertIn("function clearFlowQueueContinuation()", source)
        self.assertIn("if (!['flow', 'candidates'].includes(btn.dataset.tab))", source)

    def test_last_candidate_finishes_batch_and_returns_to_overview(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")

        candidate_restore = source.split("if (state.surface === 'candidates')", 1)[1].split(
            "flowFocus =", 1
        )[0]
        self.assertIn("if (!nextFlow) {{", candidate_restore)
        self.assertIn("clearFlowQueueContinuation();", candidate_restore)
        self.assertIn("candidateQueueFocus = null;", candidate_restore)
        self.assertIn("activateWorkbenchTab('overview');", candidate_restore)
        self.assertIn("window.scrollTo({{top: 0, behavior: 'smooth'}});", source)


if __name__ == "__main__":
    unittest.main()
