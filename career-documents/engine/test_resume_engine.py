"""Unit and integration tests for the Autonomous ATS Resume Engine."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
CAREER_DOCS_DIR = ENGINE_DIR.parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import resume_engine as engine  # noqa: E402


class TestEscapingAndSanitization(unittest.TestCase):
    def test_escape_latex_special_chars_single_pass(self):
        raw = r"R&D 100% $50 #1 snake_case {braces} ~tilde ^hat \slash"
        escaped = engine.escape_latex(raw)
        self.assertIn(r"R\&D", escaped)
        self.assertIn(r"100\%", escaped)
        self.assertIn(r"\$50", escaped)
        self.assertIn(r"\#1", escaped)
        self.assertIn(r"snake\_case", escaped)
        self.assertIn(r"\{braces\}", escaped)
        self.assertIn(r"\textasciitilde{}tilde", escaped)
        self.assertIn(r"\textasciicircum{}hat", escaped)
        self.assertIn(r"\textbackslash{}slash", escaped)
        self.assertNotIn(r"\textbackslash\{\}", escaped)

    def test_escape_latex_bullet_markdown_bold(self):
        raw = "Built **FastAPI & PyTorch** pipeline with 100% uptime."
        rendered = engine.escape_latex_bullet(raw)
        self.assertEqual(
            rendered,
            r"Built \textbf{FastAPI \& PyTorch} pipeline with 100\% uptime.",
        )

    def test_sanitize_url_allows_safe_schemes_only(self):
        self.assertEqual(
            engine.sanitize_url("https://github.com/alexrivera-example"),
            "https://github.com/alexrivera-example",
        )
        self.assertEqual(
            engine.sanitize_url("mailto:alex.rivera@example.com"),
            "mailto:alex.rivera@example.com",
        )
        self.assertEqual(
            engine.sanitize_url("tel:+91 9000012345"),
            "tel:+919000012345",
        )
        self.assertEqual(engine.sanitize_url("javascript:alert(1)"), "")

    def test_naming_convention_prevents_master_overwrite(self):
        stem = engine.build_resume_stem("Google", "Software Engineering Intern")
        self.assertEqual(stem, "Google_Software_Engineering_Intern_Resume")
        with self.assertRaises(ValueError):
            engine.build_resume_stem("MASTER", "RESUME")


class TestIntegrityValidation(unittest.TestCase):
    def setUp(self):
        self.profile = engine.load_master_profile(engine.EXAMPLE_PROFILE_JSON_PATH)
        self.valid_spec = {
            "company": "Datrics",
            "role": "AI Engineer",
            "template": "modern-ats",
            "section_order": ["summary", "education", "projects", "skills"],
            "summary": (
                "Third-year B.Tech Computer Science (AI) undergraduate at Example Institute of Technology "
                "(graduating July 2027) with Python, backend, and ML skills."
            ),
            "projects": ["sample-rag", "sample-api", "sample-vision"],
            "skills": [
                {"category": "Programming Languages", "items": ["Python", "SQL"]},
                {"category": "AI / ML", "items": ["PyTorch", "FAISS", "OpenAI Whisper"]},
                {"category": "Backend & Tools", "items": ["FastAPI", "Docker", "Git"]},
            ],
        }

    def test_valid_spec_passes_integrity(self):
        report = engine.validate_spec_integrity(self.valid_spec, self.profile)
        self.assertTrue(report["passed"], msg=str(report))
        self.assertEqual(report["violations"], [])

    def test_blocks_wrong_cgpa_or_unverified_certs(self):
        bad_spec = dict(self.valid_spec)
        bad_spec["summary"] += " AWS Certified Solutions Architect with 9.9/10 CGPA."
        report = engine.validate_spec_integrity(bad_spec, self.profile)
        self.assertFalse(report["passed"])
        joined = " ".join(report["violations"])
        self.assertIn("9.9/10", joined)
        self.assertIn("AWS Certified Solutions Architect", joined)

    def test_blocks_invented_experience(self):
        bad_spec = dict(self.valid_spec)
        bad_spec["experience"] = [
            {"company": "Fake Corp", "role": "ML Intern", "dates": "2025", "bullets": ["Did ML"]}
        ]
        report = engine.validate_spec_integrity(bad_spec, self.profile)
        self.assertFalse(report["passed"])

    def test_blocks_unverified_skills(self):
        bad_spec = json.loads(json.dumps(self.valid_spec))
        bad_spec["skills"].append({"category": "Cloud", "items": ["Kubernetes", "Terraform"]})
        report = engine.validate_spec_integrity(bad_spec, self.profile)
        self.assertFalse(report["passed"])
        self.assertTrue(any("Kubernetes" in v for v in report["violations"]))

    def test_blocks_unknown_project_id(self):
        bad_spec = json.loads(json.dumps(self.valid_spec))
        bad_spec["projects"].append("fake-project-xyz")
        report = engine.validate_spec_integrity(bad_spec, self.profile)
        self.assertFalse(report["passed"])


class TestRenderCompileAndQA(unittest.TestCase):
    def setUp(self):
        self.profile = engine.load_master_profile(engine.EXAMPLE_PROFILE_JSON_PATH)

    def test_end_to_end_render_compile_and_qa_custom_section_order(self):
        spec = {
            "company": "UnitTestCo",
            "role": "Python Backend Intern",
            "template": "modern-ats",
            "section_order": ["summary", "skills", "projects", "education"],
            "summary": (
                "Computer Science (AI) undergraduate at Example Institute of Technology (graduating July 2027) "
                "building Python FastAPI microservices, FAISS semantic search APIs, and tested CLI tools."
            ),
            "projects": ["sample-rag", "sample-api", "sample-vision"],
            "skills": [
                {"category": "Languages", "items": ["Python", "JavaScript", "SQL"]},
                {"category": "Backend & APIs", "items": ["FastAPI", "Uvicorn", "Pydantic", "REST APIs", "Node.js"]},
                {"category": "AI & Data", "items": ["PyTorch", "FAISS", "OpenAI Whisper", "Sentence-Transformers"]},
                {"category": "Tools", "items": ["Docker", "Git", "GitHub Actions", "Pytest", "SQLite"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            tex_path = tmp_dir / "UnitTestCo_Python_Backend_Intern_Resume.tex"
            pdf_path = tmp_dir / "UnitTestCo_Python_Backend_Intern_Resume.pdf"

            tex_text = engine.render_resume_tex(spec, self.profile)
            tex_path.write_text(tex_text, encoding="utf-8")

            compile_res = engine.compile_tex_to_pdf(tex_path, pdf_path)
            self.assertTrue(compile_res["compiled"], msg=str(compile_res))
            self.assertTrue(pdf_path.exists())

            qa_res = engine.validate_pdf_qa(
                pdf_path=pdf_path,
                tex_text=tex_text,
                expected_sections=spec["section_order"],
                profile=self.profile,
                compile_info=compile_res,
                expected_pages=1,
            )
            self.assertTrue(qa_res["passed"], msg=str(qa_res))
            self.assertEqual(qa_res["formatting"]["page_count"], 1)
            self.assertIn("mailto:alex.rivera@example.com", qa_res["formatting"]["links"])
            self.assertIn("https://github.com/alexrivera-example", qa_res["formatting"]["links"])
            self.assertTrue(qa_res["ats"]["reading_order_valid"])
            self.assertEqual(qa_res["ats"]["pdftotext_stderr"], "")

    def test_compact_template_render_compile_and_qa(self):
        spec = {
            "company": "CompactTestCo",
            "role": "ML Intern",
            "template": "compact-ats",
            "section_order": ["summary", "education", "projects", "skills"],
            "summary": (
                "Computer Science (AI) undergraduate at Example Institute of Technology (graduating July 2027) "
                "focused on PyTorch, FastAPI, and semantic retrieval systems."
            ),
            "projects": ["sample-rag", "sample-api", "sample-vision"],
            "skills": [
                {"category": "Languages", "items": ["Python", "SQL"]},
                {"category": "AI / ML", "items": ["PyTorch", "FAISS"]},
                {"category": "Backend & DevOps", "items": ["FastAPI", "Docker", "Git", "Pytest"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            tex_path = tmp_dir / "CompactTestCo_ML_Intern_Resume.tex"
            pdf_path = tmp_dir / "CompactTestCo_ML_Intern_Resume.pdf"

            tex_text = engine.render_resume_tex(spec, self.profile)
            tex_path.write_text(tex_text, encoding="utf-8")

            compile_res = engine.compile_tex_to_pdf(tex_path, pdf_path)
            self.assertTrue(compile_res["compiled"], msg=str(compile_res))
            qa_res = engine.validate_pdf_qa(
                pdf_path=pdf_path,
                tex_text=tex_text,
                expected_sections=spec["section_order"],
                profile=self.profile,
                compile_info=compile_res,
                expected_pages=1,
            )
            self.assertTrue(qa_res["passed"], msg=str(qa_res))


class TestKeywordCoverageAndSelect(unittest.TestCase):
    def test_keyword_coverage_distinguishes_present_and_untruthful(self):
        extracted_text = (
            "Alex Rivera Python FastAPI PyTorch FAISS Docker "
            "Example Institute of Technology B.Tech Computer Science July 2027"
        )
        jd_info = {
            "required_keywords": ["Python", "FastAPI", "PyTorch", "Kubernetes", "Spark"],
            "preferred_keywords": ["Docker", "FAISS", "AWS"],
            "partial_evidence": ["AWS (listed in older project READMEs, unconfirmed cert)"],
            "cannot_include_truthfully": ["Kubernetes", "Spark", "3+ years experience"],
        }
        cov = engine.evaluate_keyword_coverage(extracted_text, jd_info)
        self.assertEqual(cov["required_present"], ["Python", "FastAPI", "PyTorch"])
        self.assertEqual(cov["required_missing"], ["Kubernetes", "Spark"])
        self.assertIn("Kubernetes", cov["cannot_include_truthfully"])
        self.assertEqual(cov["preferred_present"], ["Docker", "FAISS"])

    def test_select_resume_enforces_exact_company_role_and_qa_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tailored = root / "tailored"
            audits = root / "audits"
            tailored.mkdir()
            audits.mkdir()

            stem = engine.build_resume_stem("AcmeAI", "ML Intern")
            pdf_path = tailored / f"{stem}.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            audit_path = audits / stem.replace("_Resume", "_Audit.md")
            audit_path.write_text(
                "- **Company:** AcmeAI\n- **Job Title:** ML Intern\n"
                "- **QA Validation:** PASS\n- **Content Integrity:** PASS\n",
                encoding="utf-8",
            )

            selected = engine.select_resume_for_application(
                "AcmeAI", "ML Intern", tailored_dir=tailored, audits_dir=audits
            )
            self.assertTrue(selected["verified"])
            self.assertEqual(selected["pdf_path"], str(pdf_path))

            with self.assertRaises(FileNotFoundError):
                engine.select_resume_for_application(
                    "OtherCo", "ML Intern", tailored_dir=tailored, audits_dir=audits
                )

            audit_path.write_text(
                "- **Company:** AcmeAI\n- **Job Title:** ML Intern\n"
                "- **QA Validation:** FAIL\n- **Content Integrity:** PASS\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                engine.select_resume_for_application(
                    "AcmeAI", "ML Intern", tailored_dir=tailored, audits_dir=audits
                )


if __name__ == "__main__":
    unittest.main()
