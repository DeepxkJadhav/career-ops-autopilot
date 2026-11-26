"""Unit and integration tests for Job Application Autopilot (Phases 2-8)."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import autopilot  # noqa: E402
import resume_engine  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


class TestFreshnessVerification(unittest.TestCase):
    def test_get_ist_now_has_plus_0530_offset(self):
        now = autopilot.get_ist_now()
        self.assertEqual(now.utcoffset(), timedelta(hours=5, minutes=30))

    def test_iso_timestamp_within_24h_is_verified(self):
        now = datetime(2026, 11, 26, 23, 30, tzinfo=IST)
        published = (now - timedelta(hours=6)).isoformat()
        res = autopilot.verify_24h_freshness(
            published_raw=published,
            evidence_text="API publishedAt timestamp",
            now_ist=now,
        )
        self.assertTrue(res["verified_24h"])
        self.assertAlmostEqual(res["age_hours"], 6.0, places=2)

    def test_iso_timestamp_older_than_24h_fails(self):
        now = datetime(2026, 11, 26, 23, 30, tzinfo=IST)
        published = (now - timedelta(hours=30)).isoformat()
        res = autopilot.verify_24h_freshness(
            published_raw=published,
            evidence_text="API publishedAt timestamp",
            now_ist=now,
        )
        self.assertFalse(res["verified_24h"])

    def test_unknown_or_updated_only_timestamp_is_not_verified_24h(self):
        now = datetime(2026, 11, 26, 23, 30, tzinfo=IST)
        res_unknown = autopilot.verify_24h_freshness("", "Not listed", now)
        self.assertFalse(res_unknown["verified_24h"])

        res_updated = autopilot.verify_24h_freshness(
            "Updated 2 hours ago",
            "Greenhouse updated_at without original created_at",
            now,
            is_updated_only=True,
        )
        self.assertFalse(res_updated["verified_24h"])
        self.assertIn("updated", res_updated["reason"].lower())

    def test_relative_posted_hours_ago_passes_when_explicit(self):
        now = datetime(2026, 11, 26, 23, 30, tzinfo=IST)
        res = autopilot.verify_24h_freshness("Posted 5 hours ago", "Job card badge", now)
        self.assertTrue(res["verified_24h"])
        self.assertAlmostEqual(res["age_hours"], 5.0, places=2)


class TestEligibilityAndScoring(unittest.TestCase):
    def setUp(self):
        self.profile = resume_engine.load_master_profile(resume_engine.EXAMPLE_PROFILE_JSON_PATH)

    def test_rejects_multi_year_experience_requirement(self):
        job = {
            "company": "Datrics",
            "role": "AI Engineer",
            "location": "Remote",
            "job_type": "full-time",
            "experience_text": "3+ years",
            "description": "We seek an AI Engineer with 3+ years experience in Python, NLP, LLMs.",
            "verified_24h": False,
        }
        ev = autopilot.evaluate_candidate_eligibility(job, self.profile)
        self.assertFalse(ev["eligible"])
        self.assertLess(ev["score"], 50)
        self.assertIn("experience", " ".join(ev["disqualifiers"]).lower())

    def test_rejects_completed_degree_immediate_fulltime_for_2027_student(self):
        job = {
            "company": "ImmediateCorp",
            "role": "Junior Python Developer",
            "location": "Bengaluru, India",
            "job_type": "full-time",
            "experience_text": "0-1 years",
            "description": "Immediate joiner required. Bachelor's degree already completed.",
            "verified_24h": True,
        }
        ev = autopilot.evaluate_candidate_eligibility(job, self.profile)
        self.assertFalse(ev["eligible"])
        self.assertTrue(any("2027" in d or "degree" in d.lower() or "immediate" in d.lower() for d in ev["disqualifiers"]))

    def test_rejects_prior_internship_mandatory_for_fresher(self):
        job = {
            "company": "FamPay",
            "role": "Backend Engineering Intern",
            "location": "Bengaluru",
            "job_type": "internship",
            "experience_text": "Must Haves: Previous internship experience in a product-based company or startup",
            "description": "Python backend intern onsite.",
            "verified_24h": True,
        }
        ev = autopilot.evaluate_candidate_eligibility(job, self.profile)
        self.assertFalse(ev["eligible"])
        self.assertEqual(ev["decision"], "Rejected_Ineligible")
        self.assertTrue(any("internship" in d.lower() for d in ev["disqualifiers"]))

    def test_accepts_negated_prior_internship_phrasing(self):
        job = {
            "company": "Worksmith Tech",
            "role": "AI Engineer Intern",
            "location": "Remote, India",
            "workplace_type": "remote",
            "job_type": "internship",
            "source": "NextKnock",
            "experience_text": "Fresher / Internship (no prior internship mandatory)",
            "description": "Fully remote AI Engineer Intern open to India. Python, Pandas, SQL, RAG, FastAPI.",
            "verified_24h": True,
        }
        ev = autopilot.evaluate_candidate_eligibility(job, self.profile)
        self.assertTrue(ev["eligible"])
        self.assertEqual(ev["decision"], "Shortlisted")

    def test_accepts_and_high_scores_india_ai_internship_within_24h(self):
        job = {
            "company": "TestAI India",
            "role": "Generative AI & Agent Developer Intern",
            "location": "Remote (India / Worldwide)",
            "workplace_type": "remote",
            "job_type": "internship",
            "source": "Himalayas",
            "experience_text": "Fresher / Current B.Tech student",
            "description": (
                "Looking for a B.Tech CS/AI student intern skilled in Python, PyTorch, FastAPI, "
                "RAG, FAISS, SQL, and Docker for a paid 6-month fully remote AI internship with mentorship."
            ),
            "verified_24h": True,
        }
        ev = autopilot.evaluate_candidate_eligibility(job, self.profile)
        self.assertTrue(ev["eligible"])
        self.assertGreaterEqual(ev["score"], 75)
        self.assertTrue(len(ev["matching_projects"]) >= 2)

    def test_rejects_excluded_portals_mandatory(self):
        for portal in ("Internshala", "LinkedIn", "Indeed", "Wellfound", "Unstop", "Naukri"):
            job = {
                "company": "SomeStartup",
                "role": "Python Developer Intern",
                "location": "Remote, India",
                "job_type": "internship",
                "source": portal,
                "official_job_url": f"https://{portal.lower()}.com/jobs/123",
                "description": "Remote Python Developer Intern in India.",
                "verified_24h": True,
            }
            ev = autopilot.evaluate_candidate_eligibility(job, self.profile)
            self.assertFalse(ev["eligible"])
            self.assertTrue(ev["excluded_portal"])

    def test_rejects_hybrid_onsite_and_secret_office_attendance(self):
        hybrid_job = {
            "company": "HybridCorp",
            "role": "Machine Learning Intern",
            "location": "Bengaluru, India (Hybrid)",
            "job_type": "internship",
            "description": "Hybrid ML intern, 3 days in office.",
            "verified_24h": True,
        }
        ev1 = autopilot.evaluate_candidate_eligibility(hybrid_job, self.profile)
        self.assertFalse(ev1["eligible"])
        self.assertTrue(ev1["non_remote_or_hybrid"])

        disguised_remote = {
            "company": "SecretOffice",
            "role": "Backend Developer Intern",
            "location": "Remote",
            "job_type": "internship",
            "description": "Remote role initially, then 2 days per week in office and must commute to Gurugram.",
            "verified_24h": True,
        }
        ev2 = autopilot.evaluate_candidate_eligibility(disguised_remote, self.profile)
        self.assertFalse(ev2["eligible"])
        self.assertTrue(ev2["non_remote_or_hybrid"])

    def test_rejects_worldwide_header_when_jd_restricts_to_us_or_eu(self):
        us_only_worldwide = {
            "company": "GlobalLabeledUS",
            "role": "AI Engineer Intern",
            "location": "Remote (Worldwide)",
            "location_restrictions": ["United States"],
            "job_type": "internship",
            "source": "Remotive",
            "description": "Must be located in the United States and authorized to work in the US.",
            "verified_24h": True,
        }
        ev = autopilot.evaluate_candidate_eligibility(us_only_worldwide, self.profile)
        self.assertFalse(ev["eligible"])
        self.assertTrue(ev["country_ineligible"])
        self.assertEqual(ev["decision"], "Rejected_Ineligible")

    def test_rejects_quality_violations_and_verifies_employer_source(self):
        spam_job = {
            "company": "TrainPay",
            "role": "Python Developer Intern",
            "location": "Remote, India",
            "job_type": "internship",
            "description": "Training fee required before joining. Commission-only lead generation internship.",
            "verified_24h": True,
        }
        ev = autopilot.evaluate_candidate_eligibility(spam_job, self.profile)
        self.assertFalse(ev["eligible"])
        self.assertTrue(ev["quality_rejected"])

        now = datetime(2026, 11, 27, 0, 30, tzinfo=IST)
        src_ok = autopilot.verify_employer_source(
            company="RealAI Labs",
            discovery_portal="Alion",
            employer_job_url="https://jobs.lever.co/realai/intern-1",
            application_url="https://jobs.lever.co/realai/intern-1/apply",
            is_open=True,
            original_posted_raw=(now - timedelta(hours=4)).isoformat(),
            evidence_text="Lever createdAt",
            now_ist=now,
        )
        self.assertTrue(src_ok["verified_open"])
        self.assertTrue(src_ok["verified_24h"])
        self.assertTrue(src_ok["is_direct_employer_url"])


class TestPersistentTrackerAndLearning(unittest.TestCase):
    def test_tracker_dedup_validation_and_confirmation_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            json_path = root / "application_tracker.json"
            md_path = root / "APPLICATION_TRACKER.md"

            record = autopilot.new_tracker_record(
                company="TestAI India",
                role="AI / ML Engineering Intern",
                posting_timestamp="2026-11-26T18:00:00+05:30",
                discovery_timestamp="2026-11-26T23:30:00+05:30",
                source="Greenhouse",
                official_job_url="https://boards.greenhouse.io/testai/jobs/12345?gh_src=abc",
                match_score=88,
                eligibility_decision="Shortlisted",
                resume_file="career-documents/tailored-resumes/TestAI_India_AI_ML_Engineering_Intern_Resume.pdf",
                cover_letter_file="career-documents/cover-letters/TestAI_India_AI_ML_Engineering_Intern_CoverLetter.md",
                application_url="https://boards.greenhouse.io/testai/jobs/12345#app",
                status="Ready",
            )
            saved1 = autopilot.upsert_tracker_record(record, json_path=json_path, md_path=md_path)
            self.assertEqual(len(autopilot.load_tracker(json_path)), 1)
            self.assertTrue(md_path.exists())

            # Duplicate URL with tracking params updates existing row instead of duplicating
            dup = dict(saved1)
            dup["official_job_url"] = "https://boards.greenhouse.io/testai/jobs/12345"
            dup["status"] = "Awaiting User Input"
            autopilot.upsert_tracker_record(dup, json_path=json_path, md_path=md_path)
            rows = autopilot.load_tracker(json_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "Awaiting User Input")

            # Cannot mark Submitted without genuine confirmation_evidence
            bad_submit = dict(rows[0])
            bad_submit["status"] = "Submitted"
            bad_submit["confirmation_evidence"] = ""
            with self.assertRaises(ValueError):
                autopilot.upsert_tracker_record(bad_submit, json_path=json_path, md_path=md_path)

            # Invalid status rejected
            bad_status = dict(rows[0])
            bad_status["status"] = "MaybeLater"
            with self.assertRaises(ValueError):
                autopilot.upsert_tracker_record(bad_status, json_path=json_path, md_path=md_path)

    def test_overlap_lock_prevents_concurrent_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".autopilot.lock"
            with autopilot.acquire_autopilot_lock(lock_path):
                with self.assertRaises(RuntimeError):
                    with autopilot.acquire_autopilot_lock(lock_path):
                        pass

    def test_cover_letter_and_learning_and_daily_report_preserve_integrity(self):
        profile = resume_engine.load_master_profile(resume_engine.EXAMPLE_PROFILE_JSON_PATH)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cl_path = autopilot.generate_cover_letter(
                company="TestAI India",
                role="AI ML Intern",
                why_company="Excited to contribute to TestAI India's semantic search backend.",
                highlighted_projects=["sample-rag", "sample-api"],
                profile=profile,
                out_dir=root / "cover-letters",
            )
            cl_text = cl_path.read_text(encoding="utf-8")
            self.assertIn("7.50/10", cl_text)
            self.assertIn("July 2027", cl_text)
            self.assertNotIn("9.9/10", cl_text)
            self.assertNotIn("AWS Certified", cl_text)

            learn_path = autopilot.analyze_outcomes_and_learnings(
                tracker_path=root / "missing.json",
                out_path=root / "LEARNING_REPORT.md",
            )
            self.assertIn("Observed Evidence", learn_path.read_text(encoding="utf-8"))

            # Pre-application Rejected_Ineligible rows must NOT be counted as Verified Submissions
            pre_rej_tracker = root / "pre_rej.json"
            pre_rej_md = root / "PRE_REJ.md"
            autopilot.upsert_tracker_record(
                autopilot.new_tracker_record(
                    company="Rubrik",
                    role="Intern",
                    posting_timestamp="2026-11-26",
                    discovery_timestamp="2026-11-26",
                    source="Greenhouse",
                    official_job_url="https://boards.greenhouse.io/rubrik/jobs/1",
                    match_score=40,
                    eligibility_decision="Rejected_Ineligible",
                    status="Rejected",
                    confirmation_evidence="CGPA mismatch",
                ),
                json_path=pre_rej_tracker,
                md_path=pre_rej_md,
            )
            learn_pre = autopilot.analyze_outcomes_and_learnings(pre_rej_tracker, root / "LEARN_PRE.md")
            self.assertIn("Verified Submissions (with confirmation evidence): 0", learn_pre.read_text(encoding="utf-8"))

            daily_path = autopilot.write_daily_report(
                discovered=[{"company": "TestAI India"}],
                verified_24h=[{"company": "TestAI India"}],
                eligible=[{"company": "TestAI India"}],
                tailored=["TestAI_India_AI_ML_Intern_Resume.pdf"],
                submitted=[],
                awaiting_user=["TestAI India — AI ML Intern"],
                blockers=["Login wall on portal"],
                out_dir=root / "daily-reports",
                portal_stats=[{"group": "A", "portal": "NextKnock", "status": "searched", "listings_found": 4, "verified_24h": 1, "notes": "OK"}],
                duplicates_removed=2,
                expired_postings=1,
                country_ineligible=3,
                relevant_count=2,
            )
            daily_text = daily_path.read_text(encoding="utf-8")
            self.assertIn("Genuine Opportunities Discovered:** 1", daily_text)
            self.assertIn("Duplicates Removed:** 2", daily_text)
            self.assertIn("Country-Ineligible Opportunities:** 3", daily_text)
            self.assertIn("NextKnock", daily_text)


if __name__ == "__main__":
    unittest.main()
