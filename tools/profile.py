"""
Candidate profile definition and job relevance matching logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass(frozen=True)
class CandidateProfile:
    """
    Default candidate profile — DevOps / AI-ML Engineer template.
    Users override these values via ~/.job-apply-mcp/config.json
    """
    title: str = "AI / Computer Vision / GenAI Engineer"
    experience_years: int = 2
    location: str = "India"
    preferred_locations: tuple[str, ...] = (
        "Hyderabad",
        "Remote",
        "Bangalore",
        "Bengaluru",
        "Pune",
    )
    skills: tuple[str, ...] = (
        "Python",
        "PyTorch",
        "Computer Vision",
        "OpenCV",
        "Deep Learning",
        "Machine Learning",
        "YOLOv8",
        "YOLOv9",
        "YOLOv10",
        "SegFormer",
        "DeepLabV3",
        "FastAPI",
        "Streamlit",
        "LangChain",
        "Agentic AI",
        "RAG",
        "LLMs",
        "Generative AI",
        "Prompt Engineering",
        "LoRA",
        "PEFT",
        "Fine-tuning",
        "Hugging Face",
        "Transformers",
        "TensorRT",
        "ONNX",
        "NVIDIA Jetson",
        "Docker",
        "AWS",
        "Bedrock",
        "SageMaker",
        "FAISS",
        "ChromaDB",
        "Pinecone",
        "Linux",
        "Git",
        "GitHub",
        "REST API",
        "NumPy",
        "Pandas",
        "Scikit-learn",
    )
    target_roles: tuple[str, ...] = (
        "AI Engineer",
        "Computer Vision Engineer",
        "Machine Learning Engineer",
        "ML Engineer",
        "GenAI Engineer",
        "LLM Engineer",
        "Agentic AI Engineer",
        "Forward Deployed Engineer",
        "Deep Learning Engineer",
        "Edge AI Engineer",
        "AI/ML Engineer",
    )
    default_search_keywords: tuple[str, ...] = (
        "AI Engineer",
        "Computer Vision Engineer",
        "Machine Learning Engineer",
        "GenAI Engineer",
        "LLM Engineer",
        "Deep Learning Engineer",
        "Forward Deployed Engineer",
    )
    # Hard title gate: a job's title must contain at least one of these
    # substrings to be considered at all.
    title_must_contain: tuple[str, ...] = (
        "ai",
        "artificial intelligence",
        "computer vision",
        "vision",
        "machine learning",
        "ml",
        "deep learning",
        "genai",
        "generative ai",
        "llm",
        "agentic",
        "forward deployed",
        "edge ai",
        "nlp",
        "pytorch",
        "data science",
    )
    avoid_keywords: tuple[str, ...] = (
        "senior",
        "sr.",
        "sr ",
        "lead",
        "staff",
        "principal",
        "architect",
        "director",
        "manager",
        "mentor",
        "trainer",
        "tutor",
        "teacher",
        "instructor",
        "teleoperator",
        "artist",
        "editor",
        "content creator",
        "graphic designer",
        "frontend",
        "react developer",
        "angular developer",
        "vue developer",
        "android",
        "ios",
        "swift",
        "kotlin",
        "flutter",
        "end-user support",
        "end user support",
        "helpdesk",
        "help desk",
        "desktop support",
        "service desk",
        "noc technician",
        "l1 support",
        "l2 support",
        "telecaller",
        "bpo",
        "sales executive",
    )


PROFILE = CandidateProfile()


def _normalize(text: str) -> str:
    """Lower-case, collapse whitespace, strip punctuation."""
    return re.sub(r"[^a-z0-9 /+#]", " ", text.lower()).strip()


def _fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _token_overlap(tokens_a: set[str], tokens_b: set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a)


def compute_match_score(
    job_title: str,
    job_description: str,
    job_location: str,
    required_skills: list[str] | None = None,
    profile: CandidateProfile = PROFILE,
) -> float:
    """
    Return a 0.0-1.0 relevance score for a job against the candidate profile.

    Scoring weights:
        - Role match    : 35%
        - Skill overlap : 40%
        - Location match: 15%
        - Avoid penalty : 10% (subtracted)
    """
    norm_title = _normalize(job_title)
    norm_desc = _normalize(job_description)
    combined_text = f"{norm_title} {norm_desc}"

    # --- Role match (35%) ---
    role_scores: list[float] = []
    for role in profile.target_roles:
        norm_role = _normalize(role)
        # direct substring check
        if norm_role in norm_title:
            role_scores.append(1.0)
        else:
            role_scores.append(_fuzzy_ratio(norm_role, norm_title))
    role_score = max(role_scores) if role_scores else 0.0

    # --- Skill overlap (40%) ---
    profile_skills_norm = {_normalize(s) for s in profile.skills}
    if required_skills:
        job_skills_norm = {_normalize(s) for s in required_skills}
        skill_score = _token_overlap(job_skills_norm, profile_skills_norm)
    else:
        # fall back to checking how many profile skills appear in description
        matched = sum(1 for s in profile_skills_norm if s in combined_text)
        skill_score = min(matched / max(len(profile_skills_norm) * 0.3, 1), 1.0)

    # --- Location match (15%) ---
    norm_location = _normalize(job_location)
    location_score = 0.0
    if "remote" in norm_location:
        location_score = 1.0
    else:
        for loc in profile.preferred_locations:
            if _normalize(loc) in norm_location:
                location_score = 1.0
                break
        if location_score == 0.0 and "india" in norm_location:
            location_score = 1.0

    # --- Avoid penalty (10%) ---
    avoid_penalty = 0.0
    for kw in profile.avoid_keywords:
        if _normalize(kw) in combined_text:
            avoid_penalty = 1.0
            break

    score = (
        0.35 * role_score
        + 0.40 * skill_score
        + 0.15 * location_score
        - 0.10 * avoid_penalty
    )
    return round(max(0.0, min(score, 1.0)), 3)


def is_experience_too_high(title: str, desc: str) -> tuple[bool, str]:
    """Return True if the job requires 3+ years experience or senior/lead level."""
    t = (title or "").lower()
    d = (desc or "").lower()

    # 1. Senior / Lead / Principal titles
    if re.search(r'\b(sr\.?|senior|lead|principal|staff|architect|director|manager|head\s+of|vp)\b', t):
        return True, "Senior/Lead title"
    if re.search(r'\b([3-9]|\d{2,})\s*\+?\s*(?:to|-)?\s*\d*\s*years?\b', t):
        return True, "Title requires 3+ years experience"

    # 2. Check Description for explicitly high experience requirements (>= 3 years)
    high_exp_patterns = [
        r'\b([3-9]|\d{2,})\s*\+?\s*(?:to|-)\s*\d+\s*(?:years?|yrs?)(?:\s+of)?\s+(?:relevant\s+|industry\s+|work\s+)?exp',
        r'\b([3-9]|\d{2,})\s*(?:\+|plus)\s*(?:years?|yrs?)(?:\s+of)?\s+(?:relevant\s+|industry\s+|work\s+)?exp',
        r'(?:minimum|at least|min\.?)\s*([3-9]|\d{2,})\s*(?:years?|yrs?)',
        r'\bexp(?:erience)?\s*[:\-]?\s*([3-9]|\d{2,})\s*(?:\+|plus)?\s*(?:to|-)?\s*\d*\s*(?:years?|yrs?)',
        r'\b([3-9]|\d{2,})\s*(?:years?|yrs?)(?:\s+of)?\s+(?:relevant\s+|proven\s+|hands\-on\s+)?experience\b',
    ]
    for pat in high_exp_patterns:
        m = re.search(pat, d)
        if m:
            # Check if this is an acceptable entry range like '0-3 years' or '1-3 years'
            span = d[max(0, m.start()-12):min(len(d), m.end()+12)]
            if re.search(r'\b(?:0|1|2)\s*(?:to|-)\s*3\b', span):
                continue
            return True, f"High experience required: {m.group(0)}"

    return False, "OK"


def should_exclude(
    job_title: str,
    job_description: str,
    required_skills: list[str] | None = None,
    profile: CandidateProfile = PROFILE,
) -> bool:
    """Return True if the job is in an excluded category or requires >2 years exp."""
    too_high, _ = is_experience_too_high(job_title, job_description)
    if too_high:
        return True

    norm_title = _normalize(job_title)
    # Check title specifically for avoid keywords
    for kw in profile.avoid_keywords:
        kw_norm = _normalize(kw)
        if re.search(r'\b' + re.escape(kw_norm) + r'\b', norm_title):
            return True

    return False


def title_is_relevant(job_title: str, profile: CandidateProfile = PROFILE) -> bool:
    """
    Hard gate: the job title must contain at least one of the candidate's
    target-role substrings. Applied before scoring so loosely-related roles
    can't pass just by scoring well on skill overlap.
    """
    norm = _normalize(job_title)
    # Must match at least one relevant keyword
    return any(kw in norm for kw in profile.title_must_contain)

