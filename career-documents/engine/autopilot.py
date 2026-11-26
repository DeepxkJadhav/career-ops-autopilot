"""Job Application Autopilot: 24h freshness verification, eligibility scoring, tracker, materials, learning, and locking."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import resume_engine

IST = timezone(timedelta(hours=5, minutes=30))
CAREER_DOCS_DIR = Path(__file__).resolve().parent.parent
CAREER_OPS_ROOT = CAREER_DOCS_DIR.parent
TRACKER_JSON_PATH = CAREER_DOCS_DIR / "application_tracker.json"
TRACKER_MD_PATH = CAREER_DOCS_DIR / "APPLICATION_TRACKER.md"
COVER_LETTERS_DIR = CAREER_DOCS_DIR / "cover-letters"
PACKETS_DIR = CAREER_DOCS_DIR / "application-packets"
DAILY_REPORTS_DIR = CAREER_DOCS_DIR / "daily-reports"
LEARNING_REPORT_PATH = CAREER_DOCS_DIR / "LEARNING_REPORT.md"
DEFAULT_LOCK_PATH = CAREER_DOCS_DIR / "engine" / ".autopilot.lock"

PRIMARY_PORTALS: list[dict[str, str]] = [
    {"id": "nextknock", "name": "NextKnock", "group": "A", "url": "https://www.nextknock.in/jobs/remote-jobs-for-freshers/internship"},
    {"id": "abekus", "name": "Abekus", "group": "A", "url": "https://abekus.ai/internships/remote"},
    {"id": "hellointern", "name": "HelloIntern", "group": "A", "url": "https://hellointern.in/work-from-home-internships"},
    {"id": "reminterns", "name": "REM Interns", "group": "A", "url": "https://reminterns.com/"},
    {"id": "hirastra", "name": "Hirastra", "group": "A", "url": "https://hirastra.com/work-from-home-internships"},
    {"id": "alion", "name": "Alion", "group": "B", "url": "https://alion.io/remote-internships/india"},
    {"id": "himalayas", "name": "Himalayas", "group": "B", "url": "https://himalayas.app/jobs/worldwide/intern"},
    {"id": "remotive", "name": "Remotive", "group": "B", "url": "https://remotive.com/remote-internship-jobs"},
    {"id": "firstjobby", "name": "First Jobby", "group": "B", "url": "https://firstjobby.com/job-boards/remote-internships"},
    {"id": "leadinternships", "name": "LEAD Internships", "group": "B", "url": "https://leadinternships.com/internships/remote"},
]

EXCLUDED_PORTAL_DOMAINS: tuple[str, ...] = (
    "internshala",
    "linkedin",
    "indeed",
    "wellfound",
    "angel.co",
    "unstop",
    "naukri",
)

TARGET_REMOTE_INTERN_ROLES: tuple[str, ...] = (
    "AI Engineer Intern",
    "Machine Learning Intern",
    "Generative AI Intern",
    "AI Agent Developer Intern",
    "Data Science Intern",
    "Data Analyst Intern",
    "Python Developer Intern",
    "Software Developer Intern",
    "Backend Developer Intern",
    "Full Stack Developer Intern",
    "Cloud Engineering Intern",
    "DevOps Intern",
    "Cybersecurity Intern",
    "Research Engineer Intern",
)

CANONICAL_STATUSES = {
    "Discovered",
    "Evaluated",
    "Shortlisted",
    "Tailoring",
    "Ready",
    "Submitted",
    "Failed",
    "Awaiting User Input",
    "Rejected",
    "Interview",
    "Offer",
}

TRACKER_FIELDS = [
    "application_id",
    "company",
    "role",
    "posting_timestamp",
    "discovery_timestamp",
    "source",
    "official_job_url",
    "match_score",
    "eligibility_decision",
    "resume_file",
    "cover_letter_file",
    "application_url",
    "status",
    "submission_timestamp",
    "confirmation_evidence",
    "follow_up_date",
]


def get_ist_now() -> datetime:
    """Return current wall-clock time in Asia/Kolkata (UTC+05:30)."""
    return datetime.now(IST)


def canonicalize_url(url: str) -> str:
    """Normalize a job URL for duplicate detection (strip query, fragment, trailing slash)."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def verify_24h_freshness(
    published_raw: str,
    evidence_text: str,
    now_ist: datetime | None = None,
    is_updated_only: bool = False,
) -> dict[str, Any]:
    """Strictly verify whether a posting was originally published within the preceding 24 hours."""
    now = now_ist or get_ist_now()
    raw = str(published_raw or "").strip()

    if is_updated_only:
        return {
            "verified_24h": False,
            "age_hours": None,
            "normalized_timestamp": None,
            "evidence": evidence_text,
            "reason": "Timestamp reflects updated/modified time only, not verified original publication.",
        }

    if not raw or raw.lower() in {"unknown", "not listed", "n/a", "none"}:
        return {
            "verified_24h": False,
            "age_hours": None,
            "normalized_timestamp": None,
            "evidence": evidence_text,
            "reason": "Publication timestamp missing or unknown; cannot classify as verified 24h.",
        }

    if re.search(r"\bupdated\b|\bmodif", raw, re.IGNORECASE) and not re.search(r"\bposted\b|\bpublished\b", raw, re.IGNORECASE):
        return {
            "verified_24h": False,
            "age_hours": None,
            "normalized_timestamp": None,
            "evidence": evidence_text,
            "reason": "Updated timestamp cannot be treated as newly published.",
        }

    # Try ISO 8601 datetime parsing first
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            # Date-only strings lack hour precision; require <=24h only if today and explicit
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                dt_ist = datetime(dt.year, dt.month, dt.day, 0, 0, tzinfo=IST)
                age_hours = (now - dt_ist).total_seconds() / 3600.0
                verified = 0.0 <= age_hours <= 24.0
                return {
                    "verified_24h": verified,
                    "age_hours": round(age_hours, 2),
                    "normalized_timestamp": dt_ist.isoformat(),
                    "evidence": evidence_text,
                    "reason": "Date-only ISO matches current 24h window." if verified else f"Date-only timestamp is {age_hours:.1f}h old (>24h).",
                }
            dt = dt.replace(tzinfo=IST)
        dt_ist = dt.astimezone(IST)
        age_hours = (now - dt_ist).total_seconds() / 3600.0
        verified = -0.25 <= age_hours <= 24.0
        return {
            "verified_24h": verified,
            "age_hours": round(max(0.0, age_hours), 2),
            "normalized_timestamp": dt_ist.isoformat(),
            "evidence": evidence_text,
            "reason": "Verified within 24 hours." if verified else f"Published {age_hours:.1f}h ago (>24h).",
        }
    except ValueError:
        pass

    # Explicit relative strings ("Posted 5 hours ago", "3h ago", "20 minutes ago", "Just now")
    low = raw.lower()
    if re.search(r"\b(just now|moments? ago|few seconds ago)\b", low):
        return {
            "verified_24h": True,
            "age_hours": 0.1,
            "normalized_timestamp": now.isoformat(),
            "evidence": evidence_text,
            "reason": "Explicit sub-hour relative posting badge.",
        }

    m_min = re.search(r"(\d+)\s*(?:min|minute|minutes|m)\b", low)
    if m_min and "month" not in low:
        mins = int(m_min.group(1))
        hrs = mins / 60.0
        dt_est = now - timedelta(minutes=mins)
        return {
            "verified_24h": hrs <= 24.0,
            "age_hours": round(hrs, 2),
            "normalized_timestamp": dt_est.isoformat(),
            "evidence": evidence_text,
            "reason": f"Relative minutes badge ({mins}m ago).",
        }

    m_hr = re.search(r"(\d+)\s*(?:h|hr|hrs|hour|hours)\b", low)
    if m_hr:
        hrs = float(m_hr.group(1))
        dt_est = now - timedelta(hours=hrs)
        return {
            "verified_24h": hrs <= 24.0,
            "age_hours": hrs,
            "normalized_timestamp": dt_est.isoformat(),
            "evidence": evidence_text,
            "reason": f"Relative hours badge ({hrs:.0f}h ago)." if hrs <= 24.0 else f"{hrs:.0f}h ago exceeds 24h.",
        }

    if re.search(r"\bposted\s+today\b|\btoday\b", low):
        return {
            "verified_24h": True,
            "age_hours": 12.0,
            "normalized_timestamp": now.date().isoformat(),
            "evidence": evidence_text,
            "reason": "Explicit 'Posted today' indicator.",
        }

    return {
        "verified_24h": False,
        "age_hours": None,
        "normalized_timestamp": None,
        "evidence": evidence_text,
        "reason": f"Ambiguous or >=1 day posting age ('{raw}'); not verified <=24h.",
    }


def _extract_min_years(text: str) -> float | None:
    patterns = [
        r"(\d+)\s*\+\s*years?",
        r"minimum(?:\s+of)?\s+(\d+)\s*years?",
        r"at\s+least\s+(\d+)\s*years?",
        r"(\d+)\s*[-–to]+\s*\d+\s*years?",
    ]
    found: list[float] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            found.append(float(m.group(1)))
    return max(found) if found else None


def validate_discovery_source(
    source: str,
    job_url: str = "",
    application_url: str = "",
) -> dict[str, Any]:
    """Enforce mandatory portal exclusions and identify Group A/B primary portal membership."""
    src_low = str(source or "").strip().lower()
    url_blob = f"{job_url} {application_url}".lower()

    for excl in EXCLUDED_PORTAL_DOMAINS:
        if excl in src_low or excl in url_blob:
            return {
                "allowed": False,
                "is_primary_portal": False,
                "portal_group": None,
                "reason": f"Excluded portal detected ('{excl}'): mandatory blocklist forbids Internshala, LinkedIn, Indeed, Wellfound, Unstop, and Naukri.",
            }

    for portal in PRIMARY_PORTALS:
        host = urlparse(portal["url"]).netloc.lower().removeprefix("www.")
        if portal["id"] in src_low or portal["name"].lower() in src_low or (host and host in url_blob):
            return {
                "allowed": True,
                "is_primary_portal": True,
                "portal_group": portal["group"],
                "reason": f"Matched primary portal {portal['name']} (Group {portal['group']}).",
            }

    return {
        "allowed": True,
        "is_primary_portal": False,
        "portal_group": None,
        "reason": "Direct employer or ATS URL allowed after portal discovery.",
    }


def verify_employer_source(
    company: str,
    discovery_portal: str,
    employer_job_url: str,
    application_url: str,
    is_open: bool,
    original_posted_raw: str,
    evidence_text: str,
    now_ist: datetime | None = None,
    is_updated_only: bool = False,
) -> dict[str, Any]:
    """Verify original employer URL, open status, non-excluded source, and 24h freshness."""
    src_check = validate_discovery_source(discovery_portal, employer_job_url, application_url)
    fresh = verify_24h_freshness(
        published_raw=original_posted_raw,
        evidence_text=evidence_text,
        now_ist=now_ist,
        is_updated_only=is_updated_only,
    )
    app_target = str(application_url or employer_job_url or "").strip()
    app_host = urlparse(app_target).netloc.lower().removeprefix("www.")
    portal_hosts = {urlparse(p["url"]).netloc.lower().removeprefix("www.") for p in PRIMARY_PORTALS}
    is_direct_employer_url = bool(app_host) and app_host not in portal_hosts

    issues: list[str] = []
    if not src_check["allowed"]:
        issues.append(src_check["reason"])
    if not is_open:
        issues.append("Posting is closed or expired.")
    if not app_target.startswith(("http://", "https://")):
        issues.append("Missing valid official application URL.")
    if not str(company or "").strip():
        issues.append("Missing verified employer name.")

    return {
        "verified_open": is_open and src_check["allowed"] and not issues,
        "verified_24h": fresh["verified_24h"],
        "is_direct_employer_url": is_direct_employer_url,
        "canonical_application_url": app_target,
        "freshness": fresh,
        "issues": issues,
    }


def evaluate_candidate_eligibility(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Evaluate remote internship eligibility and 0-100 fit from verified MASTER_PROFILE & portal rules."""
    role = str(job.get("role", ""))
    loc = str(job.get("location", ""))
    desc = str(job.get("description", ""))
    exp_text = str(job.get("experience_text", ""))
    job_type = str(job.get("job_type", "")).lower()
    workplace_type = str(job.get("workplace_type", "")).lower()
    loc_restrictions = [str(x).lower() for x in (job.get("location_restrictions") or []) if str(x).strip()]
    source = str(job.get("source", ""))
    job_url = str(job.get("official_job_url") or job.get("url") or "")
    app_url = str(job.get("application_url") or "")
    combined = f"{role}\n{exp_text}\n{desc}"
    low_role = role.lower()
    low_loc = loc.lower()
    low_desc = desc.lower()
    low_combined = combined.lower()

    disqualifiers: list[str] = []
    notes: list[str] = []
    excluded_portal = False
    non_remote_or_hybrid = False
    country_ineligible = False
    quality_rejected = False

    # 0. Excluded portal blocklist enforcement
    if source or job_url or app_url:
        src_check = validate_discovery_source(source, job_url, app_url)
        if not src_check["allowed"]:
            excluded_portal = True
            disqualifiers.append(src_check["reason"])

    # 1. Seniority check
    if re.search(r"\b(senior|staff|principal|lead|director|vp|head of|architect|sr\.?)\b", low_role) or (
        re.search(r"\bmanager\b", low_role) and "intern" not in low_role
    ):
        disqualifiers.append("Seniority mismatch: role title indicates senior/lead/management level.")

    # 2. Experience check (fresher = 0 years)
    min_yrs = _extract_min_years(f"{exp_text}\n{desc}")
    if min_yrs is not None and min_yrs >= 2:
        disqualifiers.append(f"Experience mismatch: requires {int(min_yrs)}+ years (candidate is fresher with 0 internships).")
    elif min_yrs is not None and min_yrs == 1 and "fresher" not in low_combined and "intern" not in low_role:
        notes.append("Mentions 1+ year experience; treat cautiously.")

    if (
        not profile.get("experience")
        and not re.search(r"\b(?:no|without|not)\s+(?:prior|previous)\s+internship", low_combined)
        and re.search(
            r"\b(previous internship experience|prior internship(?: experience)?\s*(?:required|mandatory|needed)|must have prior internship|internship experience mandatory)\b",
            low_combined,
        )
    ):
        disqualifiers.append("Prior internship required (candidate has 0 prior internships).")

    # 3. Student / graduation year check (derived dynamically from profile education)
    edu0 = (profile.get("education") or [{}])[0]
    grad_year = edu0.get("graduation_year")
    if not grad_year:
        years_found = re.findall(r"\b(20\d{2})\b", str(edu0.get("dates", "")))
        grad_year = int(years_found[-1]) if years_found else None
    current_year = get_ist_now().year
    is_future_student = grad_year is not None and grad_year > current_year

    is_intern = "intern" in low_role or "intern" in job_type or "trainee" in low_role
    immediate_ft = bool(re.search(r"\b(immediate joiner|available immediately|already graduated|completed bachelor)\b", low_combined))
    if grad_year:
        prior_batches = "|".join(str(y) for y in range(current_year - 2, int(grad_year)))
        if prior_batches and re.search(rf"\b({prior_batches})\s*(batch|graduate)\b", low_combined):
            immediate_ft = True
    requires_graduated = bool(re.search(r"\b(bachelor'?s degree required|completed b\.?tech|graduate degree completed)\b", low_combined))
    accepts_student = bool(
        re.search(r"\b(currently enrolled|pursuing)\b", low_combined)
        or (grad_year and re.search(rf"\b{grad_year}\s*(batch|graduate)\b|\bgraduating in {grad_year}\b", low_combined))
    )
    if is_future_student and not is_intern and (immediate_ft or requires_graduated or job_type == "full-time"):
        if not accepts_student:
            disqualifiers.append(
                f"Graduation timing mismatch: full-time / completed degree expectation conflicts with {grad_year} graduation."
            )

    # 4. CGPA floor check (derived dynamically from profile education)
    cand_cgpa_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(edu0.get("cgpa", "")))
    cand_cgpa = float(cand_cgpa_match.group(1)) if cand_cgpa_match else None
    m_cgpa = re.search(r"(?:cgpa|gpa)\s*(?:>=|>|of|above|minimum)?\s*([0-9]+\.[0-9]+)", low_combined)
    if m_cgpa and cand_cgpa is not None:
        req_cgpa = float(m_cgpa.group(1))
        if req_cgpa <= 10.0 and req_cgpa > cand_cgpa:
            disqualifiers.append(f"CGPA mismatch: requires {req_cgpa}/10 (candidate has {cand_cgpa:.2f}/10).")

    # 5. Remote-only + secret office attendance + India/Worldwide eligibility check
    is_hybrid_or_onsite = bool(
        re.search(r"\b(hybrid|on-site|onsite|in-office|in office|work from office|wfo|office attendance required)\b", f"{low_loc}\n{workplace_type}\n{low_desc}")
        or re.search(r"\b(\d+\s*days?\s*(?:a|per)\s*week\s*in\s*office|must commute|relocation required|relocate to)\b", low_desc)
    ) and not re.search(r"\b(no office|100%\s*remote|fully remote|no relocation)\b", f"{low_loc}\n{low_desc}")

    has_remote_signal = bool(
        re.search(r"\b(remote|work from home|wfh|worldwide|anywhere|distributed)\b", f"{low_loc}\n{workplace_type}\n{low_role}")
        or bool(job.get("is_remote"))
    )

    if is_hybrid_or_onsite:
        non_remote_or_hybrid = True
        disqualifiers.append("Not fully remote: hybrid, on-site, or office-attendance requirement rejected.")
    elif not has_remote_signal:
        non_remote_or_hybrid = True
        disqualifiers.append(f"Not verified as fully remote: '{loc or 'unspecified'}'.")

    explicit_india_open = bool(
        re.search(r"\b(india|indian residents?|open to india|work from anywhere|anywhere in the world|no geographic restrictions)\b", f"{low_loc} {' '.join(loc_restrictions)} {low_desc}")
    )
    restricted_patterns = (
        r"\b(us[- ]only|u\.s\.?[- ]only|united states[- ]only|usa[- ]only|"
        r"must (?:live|reside|be located|be based) in the (?:us|u\.s\.|united states|uk|united kingdom|eu|europe|canada|australia)|"
        r"us[- ]based candidates only|us citizens? only|green card|permanent resident|"
        r"authorized to work in the (?:us|united states|uk|united kingdom|eu|canada|australia)|"
        r"work authorization in (?:the )?(?:us|united states|uk|eu|canada)|"
        r"uk[- ]only|united kingdom[- ]only|eu[- ]only|eea[- ]only|europe[- ]only|canada[- ]only|australia[- ]only|latam[- ]only|"
        r"restricted to (?:us|usa|uk|eu|canada|australia)|no visa sponsorship)\b"
    )
    has_country_restriction = bool(re.search(restricted_patterns, f"{low_loc} {' '.join(loc_restrictions)} {low_desc}"))

    non_india_list_only = False
    if loc_restrictions:
        allowed_keywords = {"india", "worldwide", "global", "anywhere", "remote"}
        has_india_or_global = any(any(k in entry for k in allowed_keywords) for entry in loc_restrictions)
        if not has_india_or_global:
            non_india_list_only = True

    if non_india_list_only or (has_country_restriction and "india" not in low_loc and not re.search(r"\bindia\b", low_desc)):
        country_ineligible = True
        disqualifiers.append("Country-ineligible: geographic or work-authorization restriction excludes India (even if tagged Worldwide).")

    # 6. Quality & anti-spam rules
    paid_to_join_or_spam = re.search(
        r"\b(registration fee|training fee|course fee|certification fee|pay to apply|pay to join|"
        r"training program sold|security deposit|employment bond|commission[- ]only|100%\s*commission|"
        r"referral[- ]only|campus ambassador|lead[- ]?generation|cold calling|telecalling)\b",
        low_combined,
    )
    unpaid_signal = bool(re.search(r"\b(unpaid|no stipend|without stipend)\b", low_combined))
    real_eng_work = bool(
        re.search(r"\b(python|pytorch|fastapi|api|backend|frontend|full stack|ml|machine learning|llm|rag|sql|docker|cloud|devops|cybersecurity|codebase|pull request|production|research)\b", low_combined)
    )
    if paid_to_join_or_spam:
        quality_rejected = True
        disqualifiers.append("Blocked by quality policy: fee-based training, commission-only, lead-gen, referral-only, or campus ambassador role.")
    elif unpaid_signal and not real_eng_work:
        quality_rejected = True
        disqualifiers.append("Blocked by quality policy: unpaid role without meaningful technical engineering/R&D scope.")
    elif unpaid_signal:
        notes.append("Unpaid role with technical scope; deprioritized relative to paid internships.")

    # Score breakdown (0-100)
    # Role/Domain (0-30) covering all 14 target remote intern titles
    domain_score = 0
    if re.search(r"\b(ai|ml|machine learning|generative ai|genai|ai agent|agentic|data scien|data analy|python|backend|research engineer|applied ai|llm|nlp|computer vision)\b", low_role):
        domain_score = 30
    elif re.search(r"\b(software|developer|full stack|fullstack|cloud|devops|cybersecurity|security engineer|engineer)\b", low_role):
        domain_score = 24
    elif is_intern:
        domain_score = 12
    else:
        domain_score = 6

    # Stack / Project alignment (0-35)
    verified_skills = profile.get("verified_skills", [])
    matched_skills = [s for s in verified_skills if re.search(rf"(?<![A-Za-z0-9_]){re.escape(s)}(?![A-Za-z0-9_])", combined, re.IGNORECASE)]
    stack_score = min(35, len(set(s.lower() for s in matched_skills)) * 6)

    # Match best verified projects
    project_scores: list[tuple[int, str]] = []
    for proj in profile.get("projects", []):
        overlap = sum(1 for tech in proj.get("stack", []) if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tech)}(?![A-Za-z0-9_])", combined, re.IGNORECASE))
        domain_hit = sum(1 for d in proj.get("domains", []) if d.replace("-", " ") in low_combined or d in low_role)
        project_scores.append((overlap * 2 + domain_hit, proj["id"]))
    project_scores.sort(reverse=True)
    fallback_projects = [p["id"] for p in profile.get("projects", [])[:3] if isinstance(p, dict) and "id" in p]
    matching_projects = [pid for sc, pid in project_scores[:4] if sc > 0] or fallback_projects

    # Eligibility fit (0-15)
    elig_score = 15 if (is_intern and not disqualifiers) else (10 if not disqualifiers else 0)

    # Freshness (0-10)
    fresh_score = 10 if job.get("verified_24h") else 2

    # Location (0-10)
    if non_remote_or_hybrid or country_ineligible:
        loc_score = 0
    elif explicit_india_open or re.search(r"\b(worldwide|global|anywhere)\b", low_loc):
        loc_score = 10
    else:
        loc_score = 8

    # Quality adjustment
    if not unpaid_signal and re.search(r"\b(stipend|paid|salary|inr|usd|\$|₹|mentorship|ppo|full-time conversion)\b", low_combined):
        total_bonus = 3
    elif unpaid_signal:
        total_bonus = -8
    else:
        total_bonus = 0

    total = max(0, min(100, domain_score + stack_score + elig_score + fresh_score + loc_score + total_bonus))
    if disqualifiers:
        total = min(total, 40)

    eligible = len(disqualifiers) == 0 and is_intern and total >= 55
    decision = "Shortlisted" if (eligible and total >= 70) else ("Evaluated" if eligible else "Rejected_Ineligible")

    return {
        "eligible": eligible,
        "score": total,
        "decision": decision,
        "disqualifiers": disqualifiers,
        "country_ineligible": country_ineligible,
        "non_remote_or_hybrid": non_remote_or_hybrid,
        "excluded_portal": excluded_portal,
        "quality_rejected": quality_rejected,
        "notes": notes,
        "matched_skills": sorted(set(matched_skills)),
        "matching_projects": matching_projects,
    }


def _make_app_id(company: str, role: str, url: str) -> str:
    key = f"{company.strip().lower()}|{role.strip().lower()}|{canonicalize_url(url)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8].upper()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", company.strip()).strip("-")[:12].upper()
    return f"APP-{slug}-{digest}"


def new_tracker_record(
    company: str,
    role: str,
    posting_timestamp: str,
    discovery_timestamp: str,
    source: str,
    official_job_url: str,
    match_score: int,
    eligibility_decision: str,
    resume_file: str = "",
    cover_letter_file: str = "",
    application_url: str = "",
    status: str = "Discovered",
    submission_timestamp: str = "",
    confirmation_evidence: str = "",
    follow_up_date: str = "",
) -> dict[str, Any]:
    """Create a schema-complete Phase 6 application tracker record."""
    return {
        "application_id": _make_app_id(company, role, official_job_url),
        "company": company.strip(),
        "role": role.strip(),
        "posting_timestamp": posting_timestamp,
        "discovery_timestamp": discovery_timestamp,
        "source": source,
        "official_job_url": official_job_url.strip(),
        "match_score": int(match_score),
        "eligibility_decision": eligibility_decision,
        "resume_file": resume_file,
        "cover_letter_file": cover_letter_file,
        "application_url": application_url or official_job_url.strip(),
        "status": status,
        "submission_timestamp": submission_timestamp,
        "confirmation_evidence": confirmation_evidence,
        "follow_up_date": follow_up_date,
    }


def _validate_tracker_record(record: dict[str, Any]) -> None:
    for f in TRACKER_FIELDS:
        if f not in record:
            raise ValueError(f"Tracker record missing required field: {f}")
    if record["status"] not in CANONICAL_STATUSES:
        raise ValueError(f"Invalid status '{record['status']}'; must be one of {sorted(CANONICAL_STATUSES)}")
    if record["status"] == "Submitted":
        ev = str(record.get("confirmation_evidence", "")).strip()
        if len(ev) < 8 or ev.lower() in {"clicked submit", "submitted", "done", "ok"}:
            raise ValueError("Cannot mark status='Submitted' without genuine server-side confirmation_evidence.")
        if not str(record.get("submission_timestamp", "")).strip():
            raise ValueError("Cannot mark status='Submitted' without submission_timestamp.")


def load_tracker(json_path: Path = TRACKER_JSON_PATH) -> list[dict[str, Any]]:
    if not json_path.exists():
        return []
    return json.loads(json_path.read_text(encoding="utf-8"))


def render_tracker_markdown(records: list[dict[str, Any]]) -> str:
    header = (
        "# Persistent Application Tracker\n\n"
        "> Canonical statuses: `Discovered`, `Evaluated`, `Shortlisted`, `Tailoring`, `Ready`, `Submitted`, `Failed`, `Awaiting User Input`, `Rejected`, `Interview`, `Offer`\n\n"
        "| ID | Company | Role | Posted | Discovered | Source | Score | Decision | Status | Resume | Cover Letter | Job URL | App URL | Submitted | Confirmation | Follow-Up |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for r in records:
        cols = [
            r["application_id"],
            r["company"],
            r["role"],
            r["posting_timestamp"] or "Unknown",
            r["discovery_timestamp"],
            r["source"],
            f"{r['match_score']}/100",
            r["eligibility_decision"],
            r["status"],
            Path(r["resume_file"]).name if r["resume_file"] else "—",
            Path(r["cover_letter_file"]).name if r["cover_letter_file"] else "—",
            f"[Job]({r['official_job_url']})" if r["official_job_url"] else "—",
            f"[Apply]({r['application_url']})" if r["application_url"] else "—",
            r["submission_timestamp"] or "—",
            r["confirmation_evidence"] or "—",
            r["follow_up_date"] or "—",
        ]
        safe_cols = [str(c).replace("|", "\\|").replace("\n", " ") for c in cols]
        rows.append("| " + " | ".join(safe_cols) + " |")
    return header + "\n".join(rows) + ("\n" if rows else "")


def upsert_tracker_record(
    record: dict[str, Any],
    json_path: Path = TRACKER_JSON_PATH,
    md_path: Path = TRACKER_MD_PATH,
) -> dict[str, Any]:
    """Insert or update a tracker record with URL + (company, role) deduplication."""
    _validate_tracker_record(record)
    records = load_tracker(json_path)

    norm_url = canonicalize_url(record["official_job_url"])
    norm_pair = (record["company"].strip().lower(), record["role"].strip().lower())

    matched_idx = None
    for idx, existing in enumerate(records):
        if norm_url and canonicalize_url(existing.get("official_job_url", "")) == norm_url:
            matched_idx = idx
            break
        if (existing["company"].strip().lower(), existing["role"].strip().lower()) == norm_pair:
            matched_idx = idx
            break

    if matched_idx is not None:
        record["application_id"] = records[matched_idx]["application_id"]
        records[matched_idx] = record
    else:
        records.append(record)

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_tracker_markdown(records), encoding="utf-8")
    return record


def sync_to_career_ops_tracker(record: dict[str, Any]) -> dict[str, Any]:
    """Write a headed TSV row into batch/tracker-additions and run merge-tracker.mjs."""
    status_map = {
        "Discovered": "Evaluated",
        "Evaluated": "Evaluated",
        "Shortlisted": "Evaluated",
        "Tailoring": "Evaluated",
        "Ready": "Evaluated",
        "Awaiting User Input": "Evaluated",
        "Failed": "Evaluated",
        "Submitted": "Applied",
        "Rejected": "Rejected",
        "Interview": "Interview",
        "Offer": "Offer",
    }
    co_status = status_map.get(record["status"], "Evaluated")
    date_part = (record.get("submission_timestamp") or record.get("discovery_timestamp") or get_ist_now().isoformat())[:10]
    score_5 = f"{round(record['match_score'] / 20.0, 1)}/5"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{record['company']}-{record['role']}".lower()).strip("-")[:48]
    add_dir = CAREER_OPS_ROOT / "batch" / "tracker-additions"
    add_dir.mkdir(parents=True, exist_ok=True)

    # Reserve canonical sequential tracker number
    res_proc = subprocess.run(
        ["node", str(CAREER_OPS_ROOT / "reserve-report-num.mjs")],
        cwd=CAREER_OPS_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    num = res_proc.stdout.strip() if res_proc.returncode == 0 and res_proc.stdout.strip().isdigit() else "1"

    tsv_path = add_dir / f"{num}-{slug}.tsv"
    pdf_flag = "✅" if record.get("resume_file") else "❌"
    notes = f"{record['application_id']} | {record['eligibility_decision']} | {record['status']}"
    tsv_content = (
        "num\tdate\tcompany\trole\tstatus\tscore\tpdf\treport\tnotes\turl\n"
        f"{num}\t{date_part}\t{record['company']}\t{record['role']}\t{co_status}\t{score_5}\t{pdf_flag}\t—\t{notes}\t{record['official_job_url']}\n"
    )
    tsv_path.write_text(tsv_content, encoding="utf-8")

    proc = subprocess.run(
        ["node", str(CAREER_OPS_ROOT / "merge-tracker.mjs")],
        cwd=CAREER_OPS_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {"num": num, "tsv_path": str(tsv_path), "merge_rc": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def generate_cover_letter(
    company: str,
    role: str,
    why_company: str,
    highlighted_projects: list[str],
    profile: dict[str, Any],
    out_dir: Path = COVER_LETTERS_DIR,
) -> Path:
    """Generate a factual, role-tailored cover letter grounded strictly in MASTER_PROFILE."""
    personal = profile["personal"]
    edu = profile["education"][0]
    proj_map = {p["id"]: p for p in profile.get("projects", [])}

    proj_lines = []
    for pid in highlighted_projects[:2]:
        if pid in proj_map:
            p = proj_map[pid]
            proj_lines.append(f"- {p['short_name']} ({p['url']}): {p['bullets'][0]}")

    grad_text = str(edu.get("dates") or edu.get("graduation_year") or "upcoming graduation")
    cgpa_part = f", CGPA {edu['cgpa']}" if edu.get("cgpa") else ""
    exp_intro = (
        "Although I am a fresher with no prior corporate internships, I have built and tested engineering systems "
        "using Python, backend APIs, SQL, and modern AI/web tooling."
        if not profile.get("experience")
        else "I bring practical hands-on software and AI/ML engineering experience delivering tested systems."
    )
    links_line = " | ".join(
        x for x in [
            f"GitHub: {personal['github_url']}" if personal.get("github_url") else "",
            f"Portfolio: {personal['portfolio_url']}" if personal.get("portfolio_url") else "",
        ] if x
    )

    body = (
        f"Dear {company} Hiring Team,\n\n"
        f"I am {personal['full_name']}, a {edu['degree']} student at "
        f"{edu['institution']} ({grad_text}{cgpa_part}). I am writing to apply "
        f"for the {role} opportunity at {company}.\n\n"
        f"{why_company.strip()}\n\n"
        f"{exp_intro} Two projects most relevant to this role are:\n"
        + "\n".join(proj_lines)
        + "\n\n"
        f"Thank you for considering my application. I would welcome the chance to contribute and "
        f"learn with the {company} engineering team.\n\n"
        f"Sincerely,\n"
        f"{personal['full_name']}\n"
        f"{personal['email']} | {personal.get('phone', '')}\n"
        + (f"{links_line}\n" if links_line else "")
    )

    for forbidden in profile.get("forbidden_unverified_patterns", []):
        if forbidden.lower() in body.lower():
            raise ValueError(f"Integrity check failed in cover letter: forbidden '{forbidden}'")

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = resume_engine.build_resume_stem(company, role).replace("_Resume", "_CoverLetter")
    path = out_dir / f"{stem}.md"
    path.write_text(body, encoding="utf-8")
    return path


def stage_resume_for_browser_upload(pdf_path: Path, artifact_dir: Path) -> Path:
    """Copy a verified PDF to <Artifact Directory Path>/browser_sandbox/uploads/<filename>."""
    if not pdf_path.exists():
        raise FileNotFoundError(f"Resume PDF not found: {pdf_path}")
    uploads_dir = artifact_dir / "browser_sandbox" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / pdf_path.name
    shutil.copy2(pdf_path, dest)
    return dest


def analyze_outcomes_and_learnings(
    tracker_path: Path = TRACKER_JSON_PATH,
    out_path: Path = LEARNING_REPORT_PATH,
) -> Path:
    """Analyze recorded application statuses while separating verified outcomes from hypotheses."""
    rows = load_tracker(tracker_path)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    submitted_rows = [
        r for r in rows
        if r["status"] in {"Submitted", "Interview", "Offer"}
        or (r["status"] == "Rejected" and bool(str(r.get("submission_timestamp", "")).strip()))
    ]
    interview_rows = [r for r in rows if r["status"] in {"Interview", "Offer"}]

    lines = [
        "# Continuous Learning & Outcomes Report",
        f"*Generated: {get_ist_now().isoformat()}*",
        "",
        "## 1. Verified Outcomes Summary",
        f"- Total Tracked Applications: {len(rows)}",
        f"- Status Breakdown: {json.dumps(counts) if counts else 'None recorded'}",
        f"- Verified Submissions (with confirmation evidence): {len(submitted_rows)}",
        f"- Verified Shortlists / Interviews / Offers: {len(interview_rows)}",
        "",
        "## 2. Observed Evidence vs Hypotheses",
    ]
    if not interview_rows:
        lines.append("- **Observed Evidence:** Zero interview/offer outcomes recorded yet; sample size is insufficient to infer resume conversion trends.")
        lines.append("- **Hypotheses (Unverified):** Student-eligible internships matching Python/FastAPI/PyTorch projects should be prioritized over full-time postings requiring immediate graduation.")
    else:
        for r in interview_rows:
            lines.append(f"- **Interview/Offer Observed:** {r['company']} — {r['role']} (Score: {r['match_score']}/100, Resume: `{Path(r['resume_file']).name}`)")

    lines.extend([
        "",
        "## 3. Master Profile Integrity",
        "- `MASTER_PROFILE.md` and `master_profile.json` remain unmodified by outcome analysis.",
    ])

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def write_daily_report(
    discovered: list[dict[str, Any]],
    verified_24h: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    tailored: list[str],
    submitted: list[str],
    awaiting_user: list[str],
    blockers: list[str],
    out_dir: Path = DAILY_REPORTS_DIR,
    portal_stats: list[dict[str, Any]] | None = None,
    duplicates_removed: int = 0,
    expired_postings: int = 0,
    country_ineligible: int = 0,
    relevant_count: int | None = None,
) -> Path:
    """Write a dated Asia/Kolkata daily autopilot report with per-portal & exclusion breakdown."""
    now = get_ist_now()
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{now.date().isoformat()}_daily_report.md"
    rel_count = relevant_count if relevant_count is not None else len(eligible)

    lines = [
        f"# Daily Remote Internship Autopilot Report — {now.date().isoformat()} (Asia/Kolkata)",
        f"*Generated: {now.isoformat()}*",
        "",
        "## Metrics",
        f"- **Genuine Opportunities Discovered:** {len(discovered)}",
        f"- **Newly Published (<=24h Verified):** {len(verified_24h)}",
        f"- **Duplicates Removed:** {duplicates_removed}",
        f"- **Expired / Closed Postings:** {expired_postings}",
        f"- **Country-Ineligible Opportunities:** {country_ineligible}",
        f"- **Relevant Technical Internships:** {rel_count}",
        f"- **Eligible & Scored Opportunities:** {len(eligible)}",
        f"- **Tailored LaTeX/PDF Resumes Generated:** {len(tailored)}",
        f"- **Applications Submitted (Verified):** {len(submitted)}",
        f"- **Forms Ready / Awaiting User Input:** {len(awaiting_user)}",
        "",
    ]

    if portal_stats:
        lines.extend([
            "## Portal-by-Portal Discovery Status (10 Primary Portals)",
            "| Group | Portal | Access Status | Listings Found | Verified <=24h | Notes |",
            "|---|---|---|---:|---:|---|",
        ])
        for ps in portal_stats:
            lines.append(
                f"| {ps.get('group', '—')} | {ps.get('portal', '—')} | {ps.get('status', 'unverified')} | "
                f"{ps.get('listings_found', 0)} | {ps.get('verified_24h', 0)} | {str(ps.get('notes', '')).replace('|', '/')} |"
            )
        lines.append("")

    lines.extend([
        "## Tailored Resumes",
        *([f"- `{item}`" for item in tailored] or ["- None"]),
        "",
        "## Forms Ready / Awaiting User Action",
        *([f"- {item}" for item in awaiting_user] or ["- None"]),
        "",
        "## Verified Submissions",
        *([f"- {item}" for item in submitted] or ["- None"]),
        "",
        "## Blockers & Architectural Notes",
        *([f"- {b}" for b in blockers] or ["- None"]),
        "",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


@contextlib.contextmanager
def acquire_autopilot_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[Path]:
    """Acquire an exclusive lock file to prevent overlapping daily automation sessions."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Autopilot lock already held at {lock_path}; preventing overlapping run.") from exc
    try:
        os.write(fd, f"{os.getpid()}|{get_ist_now().isoformat()}".encode("utf-8"))
        os.close(fd)
        yield lock_path
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Job Application Autopilot CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("now-ist", help="Print current Asia/Kolkata timestamp")
    sub.add_parser("learn", help="Generate continuous learning report from tracker")
    p_stage = sub.add_parser("stage-upload", help="Stage verified PDF into browser_sandbox/uploads")
    p_stage.add_argument("--company", required=True)
    p_stage.add_argument("--role", required=True)
    p_stage.add_argument("--artifact-dir", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "now-ist":
        print(get_ist_now().isoformat())
    elif args.command == "learn":
        out = analyze_outcomes_and_learnings()
        print(str(out))
    elif args.command == "stage-upload":
        selected = resume_engine.select_resume_for_application(args.company, args.role)
        staged = stage_resume_for_browser_upload(Path(selected["pdf_path"]), args.artifact_dir)
        print(json.dumps({"staged_path": str(staged), "bare_filename": staged.name}, indent=2))


if __name__ == "__main__":
    main()
