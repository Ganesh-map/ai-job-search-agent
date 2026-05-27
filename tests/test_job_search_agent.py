import unittest
from datetime import datetime, timedelta

import job_search_agent as agent


class JobSearchAgentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 27, 7, 0, tzinfo=agent.IST)

    def test_extract_experience_rejects_over_three_years(self):
        label, max_years = agent.extract_experience("Required experience: 2-4 years in analytics")
        self.assertEqual(label, "2-4 years")
        self.assertEqual(max_years, 4)

    def test_extract_experience_detects_fresher(self):
        label, max_years = agent.extract_experience("Open to freshers and 0 YOE candidates")
        self.assertEqual(label, "0 years / fresher")
        self.assertEqual(max_years, 0)

    def test_parse_relative_date(self):
        parsed = agent.parse_posted_date("Posted 3 days ago", self.now)
        self.assertEqual(parsed.date(), (self.now - timedelta(days=3)).date())

    def test_naive_absolute_date_uses_ist(self):
        parsed = agent.parse_absolute_date("2026-05-27")
        self.assertEqual(parsed.tzinfo, agent.IST)

    def test_india_regex_does_not_match_common_word_in(self):
        self.assertIsNone(agent.INDIA_RE.search("This role is in Berlin"))

    def test_banned_title_filter(self):
        job = agent.Job(
            title="Senior Business Analyst",
            company="Example",
            location="India",
            experience_required="1-2 years",
            work_mode="Remote",
            match_score=0,
            apply_link="https://example.com/job",
            source_platform="Example",
            date_posted=self.now,
            summary="Analyst role",
            raw_text="Senior Business Analyst India 1-2 years",
        )
        self.assertFalse(agent.passes_filters(job, 2, None, 6, self.now))

    def test_scoring_prefers_remote_fresher_recent_role(self):
        job = agent.Job(
            title="Business Analyst Intern",
            company="Example",
            location="India",
            experience_required="0 years / fresher",
            work_mode="Remote",
            match_score=0,
            apply_link="https://example.com/job",
            source_platform="Example",
            date_posted=self.now - timedelta(days=1),
            summary="Business analyst internship",
            raw_text="Business Analyst Intern India fresher remote",
        )
        self.assertGreaterEqual(agent.calculate_match_score(job, 0, self.now), 9)

    def test_dedupe_keeps_highest_score(self):
        low = agent.Job(
            title="Product Analyst",
            company="Example",
            location="India",
            experience_required="1-2 years",
            work_mode="On-site",
            match_score=7,
            apply_link="https://example.com/a",
            source_platform="A",
            date_posted=self.now,
            summary="",
            raw_text="",
        )
        high = agent.Job(
            title="Product Analyst",
            company="Example",
            location="India",
            experience_required="0 years",
            work_mode="Remote",
            match_score=9,
            apply_link="https://example.com/b",
            source_platform="B",
            date_posted=self.now,
            summary="",
            raw_text="",
        )
        deduped = agent.dedupe_jobs([low, high])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].apply_link, "https://example.com/b")


if __name__ == "__main__":
    unittest.main()
