# Autonomous ATS Resume Engine & Remote Internship Autopilot

Modular extension layer inside `career-ops` providing:
- **Verified Master Profile Guard (`engine/resume_engine.py`)**: Compiles job-specific single-column LaTeX resumes (`modern-ats.tex`, `compact-ats.tex`), runs `pdftotext` + `pypdf` ATS validation, checks clickable links, enforces zero unverified claims/metrics, and generates JD + Keyword Coverage audits.
- **10-Portal Remote Internship Autopilot (`engine/autopilot.py`)**: Strict `<=24h` timestamp verification, remote-only + country-restriction checks (rejects hybrid/on-site, disguised office attendance, `"Worldwide"` traps excluding target country, paid training/commission spam), employer-source verification, persistent deduplicated tracker, cover letters, and overlap locking.
- **Cross-IDE Compatible**: Works seamlessly in **Claude Code**, **OpenAI Codex CLI**, **Google Antigravity**, **Cursor**, **Windsurf**, **Gemini CLI**, **OpenCode**, **Kimi**, **Grok**, and **Pi**.

---

## Quick Setup (Any IDE or OS)

### 1. Prerequisites
- **Node.js 18+** (`npm install` at repo root for `career-ops` scripts)
- **Python 3.10+** (`pip install -r career-documents/requirements.txt`)
- **LaTeX (`pdflatex`)** + `pdftotext` (Poppler) recommended for local PDF compilation and ATS checks (or use Overleaf manual upload when local TeX is absent)

### 2. Create Your Verified Personal Profile (Gitignored)
All personal files stay private on your machine (protected by `.gitignore`):

```bash
cp career-documents/master_profile.example.json career-documents/master_profile.json
cp career-documents/MASTER_PROFILE.example.md career-documents/MASTER_PROFILE.md
cp config/profile.example.yml config/profile.yml
cp templates/portals.example.yml portals.yml
```

Edit `career-documents/master_profile.json` and `career-documents/MASTER_PROFILE.md` with **only verified facts** (your name, email, phone, GitHub, education/CGPA, verified skills, and real projects).

### 3. Run Unit & Pipeline Checks

```bash
python -m unittest discover -s career-documents/engine -p "test_*.py" -v
node validate-portals.mjs
node verify-pipeline.mjs
```

### 4. Using in Different AI IDEs / CLIs
All IDEs route to the same source-of-truth rules in `AGENTS.md`, `modes/`, and `career-documents/PORTAL_STRATEGY.json`:
- **Claude Code:** Open the repo (`CLAUDE.md` imports `AGENTS.md`) and use `/career-ops`
- **OpenAI Codex CLI:** Run `codex` in the repo root (`CODEX.md` imports `AGENTS.md`)
- **Antigravity / Gemini CLI:** Open the workspace (`.agents/skills/career-ops/SKILL.md` + `AGENTS.md` + `.antigravitycli/`)
- **Cursor:** Automatically reads `.cursor/rules/career-ops.mdc` and `AGENTS.md`
- **Windsurf:** Automatically reads `.windsurfrules` and `AGENTS.md`
- **OpenCode / Kimi / Grok / Qwen:** Use `.opencode/`, `.kimi/`, `.grok/`, `.qwen/` skill entrypoints

---

## Directory Layout

```text
career-documents/
├── engine/
│   ├── resume_engine.py          # LaTeX renderer, compiler, PDF/ATS QA, keyword audit
│   ├── autopilot.py              # 24h freshness, remote/eligibility scoring, tracker, lock
│   ├── test_resume_engine.py     # Unit + end-to-end compilation & QA tests
│   └── test_autopilot.py         # Freshness, policy, tracker, & locking tests
├── templates/
│   ├── modern-ats.tex            # Clean single-column ATS LaTeX template
│   └── compact-ats.tex           # Dense single-page ATS LaTeX template
├── master_profile.example.json   # Safe template -> copy to master_profile.json
├── MASTER_PROFILE.example.md     # Safe template -> copy to MASTER_PROFILE.md
├── PORTAL_STRATEGY.json          # 10 primary portals, excluded portals, remote/quality rules
└── requirements.txt              # Python dependencies (pypdf)
```
