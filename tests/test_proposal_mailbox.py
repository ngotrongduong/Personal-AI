from __future__ import annotations

import threading
import unittest

from agent.proposal_mailbox import ProposalMailbox, SkillProposal


def _proposal(name: str = "type_x", generation: int = 1) -> SkillProposal:
    return SkillProposal(name, "because", created_at=10.0, generation=generation)


class ProposalMailboxTests(unittest.TestCase):
    def test_starts_empty(self) -> None:
        mailbox = ProposalMailbox()

        self.assertFalse(mailbox.occupied)
        self.assertIsNone(mailbox.take())

    def test_post_then_take_once(self) -> None:
        mailbox = ProposalMailbox()
        proposal = _proposal()

        self.assertTrue(mailbox.post(proposal))
        self.assertTrue(mailbox.occupied)
        self.assertEqual(mailbox.take(), proposal)
        self.assertIsNone(mailbox.take())
        self.assertTrue(mailbox.occupied, "a taken proposal keeps the slot until release")

    def test_second_post_is_refused_until_release(self) -> None:
        mailbox = ProposalMailbox()
        mailbox.post(_proposal("a"))

        self.assertFalse(mailbox.post(_proposal("b")))
        mailbox.take()
        self.assertFalse(mailbox.post(_proposal("b")), "still occupied after take")

        mailbox.release()
        self.assertFalse(mailbox.occupied)
        self.assertTrue(mailbox.post(_proposal("b")))
        self.assertEqual(mailbox.take().skill_name, "b")

    def test_clear_drops_and_frees(self) -> None:
        mailbox = ProposalMailbox()
        proposal = _proposal()
        mailbox.post(proposal)

        self.assertEqual(mailbox.clear(), proposal)
        self.assertFalse(mailbox.occupied)
        self.assertIsNone(mailbox.take())
        self.assertIsNone(mailbox.clear())

    def test_concurrent_posts_admit_exactly_one(self) -> None:
        mailbox = ProposalMailbox()
        barrier = threading.Barrier(8)
        results: list[bool] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            barrier.wait()
            posted = mailbox.post(_proposal(f"s{index}"))
            with lock:
                results.append(posted)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(results), 8)


if __name__ == "__main__":
    unittest.main()
