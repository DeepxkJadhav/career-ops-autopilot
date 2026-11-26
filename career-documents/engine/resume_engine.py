"""Autonomous ATS Resume Engine: build, compile, validate, audit, and select tailored LaTeX resumes."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader

CAREER_DOCS_DIR = Path(__file__).resolve().parent.parent
PROFILE_JSON_PATH = CAREER_DOCS_DIR / "master_profile.json"
EXAMPLE_PROFILE_JSON_PATH = CAREER_DOCS_DIR / "master_profile.example.json"
TEMPLATES_DIR = CAREER_DOCS_DIR / "templates"
TAILORED_DIR = CAREER_DOCS_DIR / "tailored-resumes"
JD_ARCHIVES_DIR = CAREER_DOCS_DIR / "job-description-archives"
AUDITS_DIR = CAREER_DOCS_DIR / "resume-audits"

_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_LATEX_ESCAPE_RE = re.compile("|".join(re.escape(k) for k in _LATEX_ESCAPES))
_MARKDOWN_BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")

SECTION_HEADERS = {
    "summary": "Professional Summary",
    "education": "Education",
    "experience": "Experience",
    "projects": "Projects",
    "skills": "Technical Skills",
    "certifications": "Certifications",
    "awards": "Awards & Honors",
}


def load_master_profile(path: Path = PROFILE_JSON_PATH) -> dict[str, Any]:
    """Load the verified candidate profile JSON (or instruct setup from example)."""
    if not path.exists():
        raise FileNotFoundError(
            f"Candidate profile not found at {path}. "
            f"Copy {EXAMPLE_PROFILE_JSON_PATH.name} to master_profile.json and fill in your verified details."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def escape_latex(text: str) -> str:
    """Escape LaTeX special characters in a single pass."""
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_ESCAPES[m.group(0)], str(text))


def escape_latex_bullet(text: str) -> str:
    """Escape LaTeX specials first, then turn markdown **bold** spans into \\textbf{}."""
    escaped = escape_latex(text)
    return _MARKDOWN_BOLD_RE.sub(lambda m: rf"\textbf{{{m.group(1)}}}", escaped)


def sanitize_url(url: str) -> str:
    """Allow only http(s), mailto, and tel URLs; normalize tel whitespace."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith("tel:"):
        digits = re.sub(r"\s+", "", raw[4:])
        return f"tel:{digits}" if digits else ""
    if lower.startswith("mailto:"):
        addr = raw[7:].strip()
        return f"mailto:{addr}" if "@" in addr else ""
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw
    return ""


def _escape_url_for_href(url: str) -> str:
    """Escape only % and # inside LaTeX \\href URL argument."""
    return url.replace("%", r"\%").replace("#", r"\#")


def _slugify_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip())
    return re.sub(r"_+", "_", cleaned).strip("_")


def build_resume_stem(company: str, role: str) -> str:
    """Build canonical Company_Role_Resume filename stem and protect MASTER_RESUME."""
    comp_part = _slugify_part(company)
    role_part = _slugify_part(role)
    if not comp_part or not role_part:
        raise ValueError("Both company and role must contain alphanumeric characters.")
    stem = f"{comp_part}_{role_part}_Resume"
    if stem.upper().startswith("MASTER_"):
        raise ValueError("Cannot use MASTER_ prefix for tailored application resumes.")
    return stem


def _normalize_skill_token(skill: str) -> str:
    """Strip trailing parenthetical qualifier if checking base skill name."""
    s = skill.strip()
    return re.sub(r"\s*\([^)]*\)\s*$", "", s).strip().lower()


def validate_spec_integrity(spec: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Check tailored resume specification strictly against verified MASTER_PROFILE rules."""
    violations: list[str] = []

    if spec.get("experience") and not profile.get("experience"):
        violations.append("Cannot include Work Experience when master_profile.experience is empty.")
    if spec.get("certifications") and not profile.get("certifications"):
        violations.append("Cannot include Certifications when master_profile.certifications is empty.")

    # Check forbidden unverified patterns and unresolved template placeholders across spec
    serialized = json.dumps(spec, ensure_ascii=False)
    for pattern in profile.get("forbidden_unverified_patterns", []):
        if pattern.lower() in serialized.lower():
            violations.append(f"Forbidden/unverified claim detected: '{pattern}'")
    if re.search(r"\{\{[A-Z0-9_]+\}\}", serialized):
        violations.append("Unresolved template placeholder detected in spec.")

    # Verify projects exist in master_profile
    known_projects = {p["id"]: p for p in profile.get("projects", [])}
    for proj_entry in spec.get("projects", []):
        if isinstance(proj_entry, str):
            if proj_entry not in known_projects:
                violations.append(f"Unknown project ID '{proj_entry}' not in master_profile.projects.")
        elif isinstance(proj_entry, dict):
            pid = proj_entry.get("id", "")
            if pid not in known_projects:
                violations.append(f"Unknown project object ID '{pid}' not in master_profile.projects.")
            # Verify any custom stack items are verified skills
            verified_set = {s.lower() for s in profile.get("verified_skills", [])}
            for tech in proj_entry.get("stack", []):
                if tech.lower() not in verified_set and _normalize_skill_token(tech) not in verified_set:
                    violations.append(f"Unverified stack item '{tech}' in project '{pid}'.")
        else:
            violations.append("Invalid project entry type in spec.")

    # Verify skills against verified_skills
    verified_skills = {s.lower() for s in profile.get("verified_skills", [])}
    for cat in spec.get("skills", []):
        items = cat.get("items", [])
        if isinstance(items, str):
            items = [x.strip() for x in items.split(",") if x.strip()]
        for item in items:
            if item.lower() not in verified_skills and _normalize_skill_token(item) not in verified_skills:
                violations.append(f"Unverified skill '{item}' is not present in master_profile.verified_skills.")

    return {"passed": len(violations) == 0, "violations": violations}


def validate_tex_integrity(tex_text: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Verify compiled .tex contains required identity/education facts and zero forbidden strings."""
    violations: list[str] = []
    personal = profile["personal"]
    edu = profile["education"][0]

    for pattern in profile.get("forbidden_unverified_patterns", []):
        if pattern in tex_text:
            violations.append(f"Forbidden/unverified pattern found in .tex: '{pattern}'")
    unresolved = re.findall(r"\{\{[A-Z0-9_]+\}\}", tex_text)
    if unresolved:
        violations.append(f"Unresolved template placeholder(s) in .tex: {sorted(set(unresolved))}")

    required_facts = [
        personal["full_name"],
        personal["email"],
        edu["institution"],
    ]
    for opt_field in (personal.get("phone"), personal.get("github_display"), edu.get("cgpa"), edu.get("dates")):
        if opt_field:
            required_facts.append(str(opt_field))
    for fact in required_facts:
        escaped_fact = escape_latex(fact)
        if fact not in tex_text and escaped_fact not in tex_text:
            violations.append(f"Required verified fact missing from .tex: '{fact}'")

    return {"passed": len(violations) == 0, "violations": violations}


def _build_contact_line(personal: dict[str, Any], include_portfolio: bool = True) -> str:
    parts: list[str] = []
    email_url = sanitize_url(f"mailto:{personal['email']}")
    if email_url:
        parts.append(rf"\href{{{_escape_url_for_href(email_url)}}}{{{escape_latex(personal['email'])}}}")
    phone_url = sanitize_url(f"tel:{personal['phone']}")
    if phone_url:
        parts.append(rf"\href{{{_escape_url_for_href(phone_url)}}}{{{escape_latex(personal['phone'])}}}")
    gh_url = sanitize_url(personal["github_url"])
    if gh_url:
        parts.append(rf"\href{{{_escape_url_for_href(gh_url)}}}{{{escape_latex(personal['github_display'])}}}")
    if include_portfolio and personal.get("portfolio_url"):
        port_url = sanitize_url(personal["portfolio_url"])
        if port_url:
            parts.append(rf"\href{{{_escape_url_for_href(port_url)}}}{{{escape_latex(personal['portfolio_display'])}}}")
    if personal.get("linkedin_url") and personal.get("linkedin_display"):
        li_url = sanitize_url(personal["linkedin_url"])
        if li_url:
            parts.append(rf"\href{{{_escape_url_for_href(li_url)}}}{{{escape_latex(personal['linkedin_display'])}}}")
    parts.append(escape_latex(personal["location"]))
    return " ~$|$~ ".join(parts)


def _render_section_summary(spec: dict[str, Any]) -> str:
    summary = spec.get("summary", "").strip()
    if not summary:
        return ""
    return (
        f"\\section{{{SECTION_HEADERS['summary']}}}\n"
        f"\\small{{{escape_latex_bullet(summary)}}}\n"
    )


def _render_section_education(spec: dict[str, Any], profile: dict[str, Any]) -> str:
    edu_list = profile.get("education", [])
    if not edu_list:
        return ""
    include_coursework = spec.get("include_coursework", True)
    selected_coursework = spec.get("coursework")
    lines = [f"\\section{{{SECTION_HEADERS['education']}}}", "\\resumeSubHeadingListStart"]
    for edu in edu_list:
        degree_line = f"{edu['degree']}; CGPA: {edu['cgpa']}"
        lines.append(
            "  \\resumeSubheading\n"
            f"    {{{escape_latex(edu['institution'])}}}{{{escape_latex(edu['location'])}}}\n"
            f"    {{{escape_latex(degree_line)}}}{{{escape_latex(edu['dates'])}}}"
        )
        courses = selected_coursework if selected_coursework is not None else edu.get("coursework", [])
        if include_coursework and courses:
            course_str = ", ".join(escape_latex(c) for c in courses)
            lines.append(
                "    \\resumeItemListStart\n"
                f"      \\resumeItem{{\\textbf{{Relevant Coursework:}} {course_str}}}\n"
                "    \\resumeItemListEnd"
            )
    lines.append("\\resumeSubHeadingListEnd\n")
    return "\n".join(lines)


def _render_section_projects(spec: dict[str, Any], profile: dict[str, Any]) -> str:
    known_projects = {p["id"]: p for p in profile.get("projects", [])}
    selected = spec.get("projects", [])
    if not selected:
        return ""
    lines = [f"\\section{{{SECTION_HEADERS['projects']}}}", "\\resumeSubHeadingListStart"]
    for entry in selected:
        if isinstance(entry, str):
            base = known_projects[entry]
            name = base["name"]
            url = base["url"]
            dates = base["dates"]
            stack = base["stack"]
            bullets = base["bullets"]
        else:
            base = known_projects[entry["id"]]
            name = entry.get("name") or base["name"]
            url = base["url"]
            dates = entry.get("dates") or base["dates"]
            stack = entry.get("stack") or base["stack"]
            bullets = entry.get("bullets") or base["bullets"]

        safe_url = sanitize_url(url)
        title_tex = (
            rf"\href{{{_escape_url_for_href(safe_url)}}}{{\textbf{{{escape_latex(name)}}}}}"
            if safe_url
            else rf"\textbf{{{escape_latex(name)}}}"
        )
        stack_tex = rf" $|$ \emph{{{escape_latex(', '.join(stack))}}}" if stack else ""
        lines.append(f"  \\resumeProjectHeading\n    {{{title_tex}{stack_tex}}}{{{escape_latex(dates)}}}")
        lines.append("    \\resumeItemListStart")
        for b in bullets:
            lines.append(f"      \\resumeItem{{{escape_latex_bullet(b)}}}")
        lines.append("    \\resumeItemListEnd")
    lines.append("\\resumeSubHeadingListEnd\n")
    return "\n".join(lines)


def _render_section_skills(spec: dict[str, Any]) -> str:
    cats = spec.get("skills", [])
    if not cats:
        return ""
    lines = [
        f"\\section{{{SECTION_HEADERS['skills']}}}",
        "\\vspace{-4pt}",
        "\\begin{itemize}[leftmargin=0.12in, label={}]",
        "  \\small{\\item{",
    ]
    row_lines = []
    for c in cats:
        items = c.get("items", [])
        items_str = ", ".join(items) if isinstance(items, list) else str(items)
        cat_name = c.get("category", "").strip()
        prefix = f"\\textbf{{{escape_latex(cat_name)}}}: " if cat_name else ""
        row_lines.append(f"    {prefix}{escape_latex(items_str)} \\\\")
    if row_lines:
        row_lines[-1] = row_lines[-1].removesuffix(" \\\\")
    lines.extend(row_lines)
    lines.extend(["  }}", "\\end{itemize}\n"])
    return "\n".join(lines)


def render_resume_tex(spec: dict[str, Any], profile: dict[str, Any]) -> str:
    """Render a complete, ATS-safe .tex document from template and verified spec."""
    integrity = validate_spec_integrity(spec, profile)
    if not integrity["passed"]:
        raise ValueError("Integrity check failed: " + "; ".join(integrity["violations"]))

    template_name = spec.get("template", "modern-ats")
    template_path = TEMPLATES_DIR / f"{template_name}.tex"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    template = template_path.read_text(encoding="utf-8")

    section_order = spec.get("section_order", ["summary", "education", "projects", "skills"])
    rendered_blocks: list[str] = []
    for sec in section_order:
        if sec == "summary":
            block = _render_section_summary(spec)
        elif sec == "education":
            block = _render_section_education(spec, profile)
        elif sec == "projects":
            block = _render_section_projects(spec, profile)
        elif sec == "skills":
            block = _render_section_skills(spec)
        else:
            block = ""
        if block:
            rendered_blocks.append(block)

    personal = profile["personal"]
    contact_line = _build_contact_line(personal, include_portfolio=spec.get("include_portfolio", True))

    tex_text = (
        template.replace("{{CANDIDATE_NAME}}", escape_latex(personal["full_name"]))
        .replace("{{CONTACT_LINE}}", contact_line)
        .replace("{{BODY_SECTIONS}}", "\n".join(rendered_blocks).strip())
    )

    tex_integrity = validate_tex_integrity(tex_text, profile)
    if not tex_integrity["passed"]:
        raise ValueError("Rendered .tex integrity failed: " + "; ".join(tex_integrity["violations"]))
    return tex_text


def compile_tex_to_pdf(tex_path: Path, pdf_path: Path) -> dict[str, Any]:
    """Compile .tex to .pdf in an isolated temp folder using pdflatex and parse log warnings."""
    pdflatex_bin = shutil.which("pdflatex")
    if not pdflatex_bin:
        return {
            "compiled": False,
            "engine": None,
            "error": "pdflatex not found on PATH; use authenticated Overleaf browser compile fallback.",
            "overleaf_fallback_required": True,
        }

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        work_tex = tmp_dir / tex_path.name
        work_tex.write_text(tex_path.read_text(encoding="utf-8"), encoding="utf-8")
        cmd = [
            pdflatex_bin,
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={tmp_dir}",
            str(work_tex),
        ]
        # Run twice so hyperref outlines/references stabilize
        proc1 = subprocess.run(cmd, cwd=tmp_dir, capture_output=True, text=True, timeout=120)
        proc2 = subprocess.run(cmd, cwd=tmp_dir, capture_output=True, text=True, timeout=120)

        log_path = tmp_dir / f"{tex_path.stem}.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        overfull_hbox = re.findall(r"Overfull \\hbox \(([0-9.]+)pt too wide\)", log_text)
        overfull_vbox = re.findall(r"Overfull \\vbox \(([0-9.]+)pt too high\)", log_text)
        missing_chars = re.findall(r"Missing character:.*", log_text)
        latex_errors = [line for line in log_text.splitlines() if line.startswith("!")]

        built_pdf = tmp_dir / f"{tex_path.stem}.pdf"
        if proc2.returncode != 0 or not built_pdf.exists():
            return {
                "compiled": False,
                "engine": "pdflatex",
                "error": "\n".join(latex_errors) or proc2.stderr or proc2.stdout,
                "overfull_hbox_pts": [float(x) for x in overfull_hbox],
                "overfull_vbox_pts": [float(x) for x in overfull_vbox],
            }

        shutil.copy2(built_pdf, pdf_path)
        return {
            "compiled": True,
            "engine": "pdflatex",
            "pdf_size_bytes": pdf_path.stat().st_size,
            "overfull_hbox_pts": [float(x) for x in overfull_hbox],
            "overfull_vbox_pts": [float(x) for x in overfull_vbox],
            "missing_chars": missing_chars,
            "latex_errors": latex_errors,
            "first_pass_rc": proc1.returncode,
            "second_pass_rc": proc2.returncode,
        }


def _extract_pdf_links(reader: PdfReader) -> list[str]:
    uris: list[str] = []
    for page in reader.pages:
        annots = page.get("/Annots") or []
        for annot in annots:
            obj = annot.get_object()
            action = obj.get("/A")
            if action and "/URI" in action:
                uris.append(str(action["/URI"]))
    return uris


def _extract_pdftotext(pdf_path: Path) -> tuple[str, str, int]:
    pdftotext_bin = shutil.which("pdftotext")
    if not pdftotext_bin:
        return "", "pdftotext not found", 127
    proc = subprocess.run(
        [pdftotext_bin, str(pdf_path), "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return proc.stdout, proc.stderr.strip(), proc.returncode


def validate_pdf_qa(
    pdf_path: Path,
    tex_text: str,
    expected_sections: list[str],
    profile: dict[str, Any],
    compile_info: dict[str, Any],
    expected_pages: int = 1,
) -> dict[str, Any]:
    """Run Content, Formatting, and ATS Validation checks on a compiled PDF."""
    issues: list[str] = []

    # 1. Content & Integrity Check
    tex_integrity = validate_tex_integrity(tex_text, profile)
    if not tex_integrity["passed"]:
        issues.extend(tex_integrity["violations"])

    # 2. Formatting Validation
    if not compile_info.get("compiled") or not pdf_path.exists():
        issues.append("PDF compilation failed or PDF file missing.")
        return {"passed": False, "issues": issues, "content": tex_integrity, "formatting": {}, "ats": {}}

    overfull_h = [pt for pt in compile_info.get("overfull_hbox_pts", []) if pt > 2.0]
    overfull_v = [pt for pt in compile_info.get("overfull_vbox_pts", []) if pt > 2.0]
    if overfull_h:
        issues.append(f"Text overflow detected (Overfull hbox > 2pt): {overfull_h}")
    if overfull_v:
        issues.append(f"Vertical overflow detected (Overfull vbox > 2pt): {overfull_v}")
    if compile_info.get("missing_chars"):
        issues.append(f"Missing font characters: {compile_info['missing_chars']}")

    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    if page_count != expected_pages:
        issues.append(f"Expected {expected_pages} page(s), got {page_count} page(s).")

    links = _extract_pdf_links(reader)
    personal = profile["personal"]
    edu = profile["education"][0]
    expected_mail = f"mailto:{personal['email']}"
    if expected_mail not in links:
        issues.append(f"Missing clickable email link: {expected_mail}")
    if personal.get("github_url") and personal["github_url"] not in links:
        issues.append(f"Missing clickable GitHub link: {personal['github_url']}")

    # 3. ATS Text Extraction & Reading Order Validation
    pdftotext_out, pdftotext_err, pdftotext_rc = _extract_pdftotext(pdf_path)
    pypdf_text = "\n".join((p.extract_text() or "") for p in reader.pages)
    extracted = pdftotext_out if pdftotext_rc == 0 and pdftotext_out.strip() else pypdf_text

    if pdftotext_err:
        issues.append(f"pdftotext warning/error (possible glyph issue): {pdftotext_err}")
    if "\ufffd" in extracted or "Ó" in extracted or "‡" in extracted:
        issues.append("Garbled/icon glyphs detected in extracted ATS text.")
    if not extracted.strip():
        issues.append("Extracted PDF text is empty — document may not be machine-readable.")

    # Verify required contact + education strings survive PDF extraction
    tokens = [personal["full_name"], personal["email"], edu["institution"]]
    for opt in (personal.get("github_display"), edu.get("cgpa")):
        if opt:
            tokens.append(str(opt))
    for token in tokens:
        if token not in extracted:
            issues.append(f"ATS extraction lost critical text: '{token}'")
    phone_digits = re.sub(r"\D+", "", str(personal.get("phone", "")))[-10:]
    if len(phone_digits) >= 7 and phone_digits not in re.sub(r"\D+", "", extracted):
        issues.append(f"ATS extraction lost phone digits ending in '{phone_digits}'")

    # Verify reading order matches expected_sections
    last_idx = -1
    reading_order_valid = True
    detected_positions: dict[str, int] = {}
    for sec in expected_sections:
        header = SECTION_HEADERS.get(sec)
        if not header:
            continue
        match = re.search(rf"(?im)^\s*{re.escape(header)}\s*$", extracted)
        if not match:
            match = re.search(re.escape(header), extracted, re.IGNORECASE)
        if not match:
            issues.append(f"ATS section heading not found in extracted text: '{header}'")
            reading_order_valid = False
            continue
        pos = match.start()
        detected_positions[header] = pos
        if pos <= last_idx:
            issues.append(f"ATS reading order out of sequence at section '{header}'")
            reading_order_valid = False
        last_idx = pos

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "content": tex_integrity,
        "formatting": {
            "page_count": page_count,
            "pdf_size_bytes": pdf_path.stat().st_size,
            "overfull_hbox_pts": compile_info.get("overfull_hbox_pts", []),
            "overfull_vbox_pts": compile_info.get("overfull_vbox_pts", []),
            "links": links,
        },
        "ats": {
            "pdftotext_rc": pdftotext_rc,
            "pdftotext_stderr": pdftotext_err,
            "reading_order_valid": reading_order_valid,
            "section_positions": detected_positions,
            "extracted_text": extracted,
        },
    }


def _keyword_in_text(keyword: str, text: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def evaluate_keyword_coverage(extracted_text: str, jd_info: dict[str, Any]) -> dict[str, Any]:
    """Compute truthful keyword coverage across required, preferred, partial, and unclaimable terms."""
    required = jd_info.get("required_keywords", [])
    preferred = jd_info.get("preferred_keywords", [])

    req_present = [kw for kw in required if _keyword_in_text(kw, extracted_text)]
    req_missing = [kw for kw in required if not _keyword_in_text(kw, extracted_text)]
    pref_present = [kw for kw in preferred if _keyword_in_text(kw, extracted_text)]
    pref_missing = [kw for kw in preferred if not _keyword_in_text(kw, extracted_text)]

    return {
        "required_present": req_present,
        "required_missing": req_missing,
        "preferred_present": pref_present,
        "preferred_missing": pref_missing,
        "partial_representation": jd_info.get("partial_evidence", []),
        "cannot_include_truthfully": jd_info.get("cannot_include_truthfully", []),
    }


def write_jd_archive(stem: str, jd_info: dict[str, Any], out_dir: Path = JD_ARCHIVES_DIR) -> Path:
    """Save complete archived JD analysis and Requirement Mapping Matrix."""
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_name = stem.replace("_Resume", "_JD.md")
    path = out_dir / archive_name

    matrix_rows = "\n".join(
        f"| {r['requirement']} | {r['classification']} | {r['evidence']} |"
        for r in jd_info.get("requirement_matrix", [])
    )
    content = (
        f"# Job Description Archive — {jd_info['company']} | {jd_info['role']}\n\n"
        f"- **Job URL:** {jd_info.get('url', 'N/A')}\n"
        f"- **Posted Date / Freshness:** {jd_info.get('posted_date', 'Not listed')}\n"
        f"- **Location:** {jd_info.get('location', 'Not specified')}\n"
        f"- **Compensation:** {jd_info.get('compensation', 'Not listed')}\n"
        f"- **Archived Date:** {jd_info.get('archived_date', '')}\n\n"
        f"## Extracted Requirements\n"
        f"- **Responsibilities:** {'; '.join(jd_info.get('responsibilities', []))}\n"
        f"- **Mandatory Skills:** {', '.join(jd_info.get('required_keywords', []))}\n"
        f"- **Preferred Skills:** {', '.join(jd_info.get('preferred_keywords', []))}\n"
        f"- **Education:** {jd_info.get('education_req', 'Not specified')}\n"
        f"- **Experience:** {jd_info.get('experience_req', 'Not specified')}\n"
        f"- **Soft Skills:** {', '.join(jd_info.get('soft_skills', []))}\n\n"
        f"## Job Requirement Mapping Matrix\n\n"
        f"| Requirement | Classification | Verified Evidence in MASTER_PROFILE |\n"
        f"|---|---|---|\n"
        f"{matrix_rows}\n\n"
        f"## Full Raw / Guarded Posting Text\n\n"
        f"```text\n{jd_info.get('raw_text', '').strip()}\n```\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def write_audit_report(
    stem: str,
    spec: dict[str, Any],
    jd_info: dict[str, Any],
    qa_res: dict[str, Any],
    coverage: dict[str, Any],
    tex_path: Path,
    pdf_path: Path,
    jd_path: Path,
    out_dir: Path = AUDITS_DIR,
) -> Path:
    """Save comprehensive per-application Resume Audit Report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_path = out_dir / stem.replace("_Resume", "_Audit.md")

    status = "PASS" if qa_res["passed"] else "FAIL"
    integrity_status = "PASS" if qa_res["content"]["passed"] else "FAIL"
    skills_str = ", ".join(
        f"{s['category']}: {', '.join(s['items']) if isinstance(s['items'], list) else s['items']}"
        for s in spec.get("skills", [])
    )
    projects_str = ", ".join(
        p if isinstance(p, str) else p["id"] for p in spec.get("projects", [])
    )

    content = (
        f"# Resume Audit Report — {spec['company']} | {spec['role']}\n\n"
        f"- **Company:** {spec['company']}\n"
        f"- **Job Title:** {spec['role']}\n"
        f"- **Job Description URL:** {jd_info.get('url', 'N/A')}\n"
        f"- **Resume Version:** `{stem}` (`{spec.get('template', 'modern-ats')}`)\n"
        f"- **QA Validation:** {status}\n"
        f"- **Content Integrity:** {integrity_status}\n\n"
        f"## Tailoring Decisions\n"
        f"- **Section Order:** `{' -> '.join(spec.get('section_order', []))}`\n"
        f"- **Selected Projects:** {projects_str}\n"
        f"- **Selected Skills:** {skills_str}\n"
        f"- **Rationale:** {spec.get('tailoring_notes', 'Prioritized verified projects and stack aligned with JD.')}\n\n"
        f"## Keyword Coverage Report\n"
        f"> Note: Keyword coverage measures observed term alignment only — no ATS pass score or guarantee is implied.\n\n"
        f"- **Required Keywords Present ({len(coverage['required_present'])}/{len(jd_info.get('required_keywords', []))}):** {', '.join(coverage['required_present']) or 'None'}\n"
        f"- **Preferred Keywords Present ({len(coverage['preferred_present'])}/{len(jd_info.get('preferred_keywords', []))}):** {', '.join(coverage['preferred_present']) or 'None'}\n"
        f"- **Partially Represented:** {'; '.join(coverage['partial_representation']) or 'None'}\n"
        f"- **Missing Keywords:** {', '.join(coverage['required_missing'] + coverage['preferred_missing']) or 'None'}\n"
        f"- **Excluded (Cannot Truthfully Include):** {', '.join(coverage['cannot_include_truthfully']) or 'None'}\n\n"
        f"## Validation Checks\n"
        f"- **Factual Validation:** {integrity_status} (Violations: {qa_res['content'].get('violations', [])})\n"
        f"- **LaTeX / PDF Quality:** Pages={qa_res['formatting'].get('page_count')}, Size={qa_res['formatting'].get('pdf_size_bytes')}B, OverfullH={qa_res['formatting'].get('overfull_hbox_pts')}\n"
        f"- **Clickable Links:** {len(qa_res['formatting'].get('links', []))} verified URIs\n"
        f"- **ATS Text Extraction (`pdftotext`):** rc={qa_res['ats'].get('pdftotext_rc')}, reading_order_valid={qa_res['ats'].get('reading_order_valid')}, stderr='{qa_res['ats'].get('pdftotext_stderr')}'\n"
        f"- **QA Issues:** {qa_res.get('issues') or 'None'}\n\n"
        f"## Final Document Paths\n"
        f"- **LaTeX Source:** `{tex_path}`\n"
        f"- **Validated PDF:** `{pdf_path}`\n"
        f"- **Archived JD:** `{jd_path}`\n"
    )
    audit_path.write_text(content, encoding="utf-8")
    return audit_path


def select_resume_for_application(
    company: str,
    role: str,
    tailored_dir: Path = TAILORED_DIR,
    audits_dir: Path = AUDITS_DIR,
) -> dict[str, Any]:
    """Locate and verify the exact job-specific validated PDF prior to upload."""
    stem = build_resume_stem(company, role)
    pdf_path = tailored_dir / f"{stem}.pdf"
    audit_path = audits_dir / stem.replace("_Resume", "_Audit.md")

    if pdf_path.name.upper().startswith("MASTER_"):
        raise ValueError("Refusing to select MASTER_RESUME for an individual application.")
    if not pdf_path.exists():
        raise FileNotFoundError(f"Tailored resume PDF not found: {pdf_path}")
    if not audit_path.exists():
        raise FileNotFoundError(f"Audit report missing for {pdf_path.name}; refusing unverified PDF.")

    audit_text = audit_path.read_text(encoding="utf-8")
    if "- **QA Validation:** PASS" not in audit_text or "- **Content Integrity:** PASS" not in audit_text:
        raise ValueError(f"Audit report {audit_path.name} did not PASS QA/Integrity checks.")
    if f"- **Company:** {company}" not in audit_text or f"- **Job Title:** {role}" not in audit_text:
        raise ValueError(f"Audit metadata mismatch in {audit_path.name}.")

    return {
        "verified": True,
        "company": company,
        "role": role,
        "stem": stem,
        "pdf_path": str(pdf_path),
        "audit_path": str(audit_path),
    }


def run_tailoring_pipeline(
    spec_path: Path,
    jd_path_json: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run full tailoring pipeline: validate -> render .tex -> compile PDF -> QA -> archive JD -> write audit."""
    profile = load_master_profile()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    stem = build_resume_stem(spec["company"], spec["role"])

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    tex_path = TAILORED_DIR / f"{stem}.tex"
    pdf_path = TAILORED_DIR / f"{stem}.pdf"

    if not overwrite and (tex_path.exists() or pdf_path.exists()):
        raise FileExistsError(f"Tailored files already exist for {stem}; pass --overwrite to replace intentionally.")

    tex_text = render_resume_tex(spec, profile)
    tex_path.write_text(tex_text, encoding="utf-8")

    compile_info = compile_tex_to_pdf(tex_path, pdf_path)
    qa_res = validate_pdf_qa(
        pdf_path=pdf_path,
        tex_text=tex_text,
        expected_sections=spec.get("section_order", ["summary", "education", "projects", "skills"]),
        profile=profile,
        compile_info=compile_info,
        expected_pages=spec.get("expected_pages", 1),
    )

    result: dict[str, Any] = {
        "stem": stem,
        "tex_path": str(tex_path),
        "pdf_path": str(pdf_path),
        "compiled": compile_info.get("compiled", False),
        "qa_passed": qa_res["passed"],
        "qa_issues": qa_res["issues"],
    }

    if jd_path_json and jd_path_json.exists():
        jd_info = json.loads(jd_path_json.read_text(encoding="utf-8"))
        jd_md = write_jd_archive(stem, jd_info)
        coverage = evaluate_keyword_coverage(qa_res["ats"].get("extracted_text", ""), jd_info)
        audit_md = write_audit_report(stem, spec, jd_info, qa_res, coverage, tex_path, pdf_path, jd_md)
        result["jd_archive_path"] = str(jd_md)
        result["audit_path"] = str(audit_md)
        result["keyword_coverage"] = coverage

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous ATS Resume Engine CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tailor = sub.add_parser("tailor", help="Generate, compile, QA, and audit a tailored resume")
    p_tailor.add_argument("--spec", required=True, type=Path)
    p_tailor.add_argument("--jd", type=Path)
    p_tailor.add_argument("--overwrite", action="store_true")

    p_select = sub.add_parser("select", help="Verify and return the exact tailored PDF for an application")
    p_select.add_argument("--company", required=True)
    p_select.add_argument("--role", required=True)

    args = parser.parse_args()
    if args.command == "tailor":
        out = run_tailoring_pipeline(args.spec, args.jd, overwrite=args.overwrite)
        print(json.dumps(out, indent=2))
        if not out["qa_passed"]:
            raise SystemExit(1)
    elif args.command == "select":
        out = select_resume_for_application(args.company, args.role)
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
