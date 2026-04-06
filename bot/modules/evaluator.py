from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bot.config import settings
from bot.modules.ingestion import fetch_readme_excerpt

logger = structlog.get_logger(__name__)

_RETRY_USER_MESSAGE = (
    "Your previous reply was not usable. Reply with ONLY a single JSON object. "
    "No markdown fences, no commentary, no text before or after the JSON.\n"
    "If REJECT: {\"status\":\"REJECT\",\"reason\":\"...\"}.\n"
    "If KEEP: {\"status\":\"KEEP\",\"summary\",\"hidden_capability\",\"business_mapping\","
    "\"target_user\",\"product_idea\",\"features\" (array of strings, max 5),\"monetization\","
    "\"scores\":{\"business_potential\":1-10,\"novelty\":1-10,\"ease_of_mvp\":1-10}} "
    "(overall inside scores is optional)."
)

SYSTEM_PROMPT = r"""You are an expert venture analyst and AI product strategist.

Your task is to evaluate open-source GitHub repositories and surface those with any credible path to becoming a real, monetizable software product — even if that path requires imagination, pivoting, or wrapping.

You are NOT summarizing. You are NOT describing technically. You are extracting business opportunity.

The user message contains structured INPUT (repo name, description, README excerpt, stars, last updated).

---

STEP 1: UNDERSTAND

Explain:
- what the repo actually does (concrete behavior)
- what capability it provides
- what type of system it is (tool / framework / demo / agent / UI app)

---

STEP 2: FILTER

REJECT if ANY of the following:
- purely academic / paper reproduction with no runnable product surface
- dataset, benchmark, or model weights only — no tooling around it
- no identifiable user who would pay for this, even indirectly
- personal config, dotfiles, homework, or course project
- abandoned (last commit > 6 months, no stars traction)

KEEP if there is a credible — not just conceivable — path to revenue. Someone real must be willing to pay for what this enables.

If REJECT:
Return:
{
  "status": "REJECT",
  "reason": "short reason"
}

STOP.

---

STEP 3: LATENT OPPORTUNITY (ONLY IF KEEP)

Identify:

1. Hidden Capability
What does this enable that is NOT packaged as a product?

2. Business Mapping
What real business workflow could this replace or improve?

3. Target User
Be specific (job role + industry)

4. What is missing commercially
What would need to be built or added to make this a sellable product?

---

STEP 4: PRODUCTIZATION

Provide:

- product_idea (1–2 lines)
- target_customer
- core_workflow
- key_features (max 5)
- monetization_logic (why they pay)

---

STEP 5: SCORING

Rate 1–10:

- business_potential (how strong the commercial opportunity is)
- novelty (how differentiated vs existing products)
- ease_of_mvp (how quickly an MVP could be shipped)

Compute:
overall_score = (0.5 * business_potential) + (0.3 * novelty) + (0.2 * ease_of_mvp)

Also rate:
- confidence: 1–10 (how certain you are this has real product potential; be honest when unsure)

---

OUTPUT FORMAT (STRICT JSON):

If KEEP:
{
  "status": "KEEP",
  "summary": "...",
  "hidden_capability": "...",
  "business_mapping": "...",
  "target_user": "...",
  "product_idea": "...",
  "target_customer": "...",
  "core_workflow": "...",
  "features": ["..."],
  "monetization": "...",
  "scores": {
    "business_potential": 0-10,
    "novelty": 0-10,
    "ease_of_mvp": 0-10,
    "overall": 0-10,
    "confidence": 0-10
  }
}

If REJECT:
{
  "status": "REJECT",
  "reason": "..."
}

---

RULES:
- Be selective. Only KEEP repos where you can name a specific paying customer.
- Wrappers around APIs are fine if the UX or workflow layer adds clear value.
- Early-stage is fine. Vague is not — there must be a specific business angle.
- Avoid generic "AI assistant" or "productivity tool" outputs. Be precise.
- Score honestly — a 6 means weak, a 9 means strong. Don't inflate.
- Respond with JSON only. No markdown fences.

END."""


def _coerce_score_1_10(value: Any) -> int:
    if value is None:
        return 5
    if isinstance(value, bool):
        return 5
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 5
    try:
        x = int(round(float(value)))
    except (TypeError, ValueError):
        return 5
    return max(1, min(10, x))


def _ensure_non_empty_str(value: Any, fallback: str, max_len: int) -> str:
    if value is None:
        text = fallback
    elif isinstance(value, list):
        text = "; ".join(str(x).strip() for x in value if str(x).strip()) or fallback
    else:
        text = str(value).strip() or fallback
    if len(text) > max_len:
        return text[:max_len]
    return text


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t[3:].lstrip()
    if t.lower().startswith("json"):
        t = t[4:].lstrip()
    if "\n" in t:
        t = t.split("\n", 1)[1]
    else:
        t = ""
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3].rstrip()
    return t


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if in_str:
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_json_lenient(raw: str) -> dict[str, Any] | None:
    text = _strip_markdown_fences(raw.strip())
    for candidate in (text, _extract_json_object(text) or ""):
        if not candidate:
            continue
        try:
            out = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(out, dict):
            return out
    return None


def _maybe_parse_nested_json(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            inner = json.loads(value)
            return inner
        except json.JSONDecodeError:
            pass
    return value


def _normalize_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip().upper()
    if s in ("KEEP", "REJECT"):
        return s
    low = value.strip().lower()
    if low == "keep":
        return "KEEP"
    if low == "reject":
        return "REJECT"
    return None


def _normalize_eval_dict(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    st = _normalize_status(out.get("status"))
    if st:
        out["status"] = st

    if "key_features" in out and "features" not in out:
        out["features"] = out["key_features"]
    if "monetization_logic" in out and "monetization" not in out:
        out["monetization"] = out["monetization_logic"]

    scores = _maybe_parse_nested_json(out.get("scores"))
    if isinstance(scores, dict):
        sc = dict(scores)
        for alt in ("ease_to_mvp", "easeOfMvp"):
            if "ease_of_mvp" not in sc and alt in sc:
                sc["ease_of_mvp"] = sc[alt]
                break
        out["scores"] = sc
    elif scores is not None:
        out["scores"] = scores

    return out


class RejectEvalSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["REJECT"]
    reason: str = Field(min_length=1, max_length=800)

    @field_validator("reason", mode="before")
    @classmethod
    def reason_to_str(cls, v: Any) -> str:
        if v is None:
            return "Rejected."
        if isinstance(v, list):
            s = "; ".join(str(x).strip() for x in v if str(x).strip())[:800]
            return s or "Rejected."
        s = str(v).strip()
        return s if s else "Rejected."


class ScoreBlockSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")
    business_potential: int = Field(ge=1, le=10)
    novelty: int = Field(ge=1, le=10)
    ease_of_mvp: int = Field(ge=1, le=10)
    overall: float | int | None = None
    confidence: int | None = None

    @field_validator("business_potential", "novelty", "ease_of_mvp", mode="before")
    @classmethod
    def coerce_scores(cls, v: Any) -> int:
        return _coerce_score_1_10(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> int | None:
        if v is None:
            return None
        return _coerce_score_1_10(v)


class KeepEvalSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["KEEP"]
    summary: str = Field(min_length=1, max_length=8000)
    hidden_capability: str = Field(min_length=1, max_length=8000)
    business_mapping: str = Field(min_length=1, max_length=8000)
    target_user: str = Field(min_length=1, max_length=2000)
    product_idea: str = Field(min_length=1, max_length=4000)
    target_customer: str = Field(default="", max_length=2000)
    core_workflow: str = Field(default="", max_length=4000)
    features: list[str] = Field(default_factory=list)
    monetization: str = Field(min_length=1, max_length=4000)
    scores: ScoreBlockSchema

    @model_validator(mode="before")
    @classmethod
    def alias_output_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "key_features" in out and "features" not in out:
            out["features"] = out["key_features"]
        if "monetization_logic" in out and "monetization" not in out:
            out["monetization"] = out["monetization_logic"]
        scores = out.get("scores")
        if isinstance(scores, dict):
            sc = dict(scores)
            for alt in ("ease_to_mvp", "easeOfMvp"):
                if "ease_of_mvp" not in sc and alt in sc:
                    sc["ease_of_mvp"] = sc[alt]
            out["scores"] = sc
        return out

    @field_validator("features", mode="before")
    @classmethod
    def normalize_features(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str) and v.strip():
            return [v.strip()[:500]]
        if not isinstance(v, list):
            return []
        out = [str(x).strip() for x in v if str(x).strip()]
        return out[:5]


@dataclass
class EvalResult:
    status: str
    reason: str | None = None
    business_potential: int = 0
    novelty: int = 0
    ease_to_mvp: int = 0
    summary: str = ""
    product_idea: str = ""
    monetization: str = ""
    features: list[str] = field(default_factory=list)
    hidden_capability: str = ""
    business_mapping: str = ""
    target_user: str = ""
    target_customer: str = ""
    core_workflow: str = ""

    def to_output_dict(self) -> dict[str, Any]:
        from bot.modules.ranker import compute_score

        if self.status == "REJECT":
            return {"status": "REJECT", "reason": self.reason or ""}

        overall = compute_score(self)
        return {
            "status": "KEEP",
            "summary": self.summary,
            "hidden_capability": self.hidden_capability,
            "business_mapping": self.business_mapping,
            "target_user": self.target_user,
            "product_idea": self.product_idea,
            "target_customer": self.target_customer,
            "core_workflow": self.core_workflow,
            "features": self.features,
            "monetization": self.monetization,
            "scores": {
                "business_potential": self.business_potential,
                "novelty": self.novelty,
                "ease_of_mvp": self.ease_to_mvp,
                "overall": overall,
            },
        }


def _build_user_message(repo: dict, readme_excerpt: str) -> str:
    name = repo.get("full_name", repo.get("name", "unknown"))
    return (
        "INPUT:\n"
        f"- Repo Name: {name}\n"
        f"- Description: {repo.get('description') or 'N/A'}\n"
        f"- README:\n{readme_excerpt}\n"
        f"- Stars: {repo.get('stars', 0)}\n"
        f"- Last Updated: {repo.get('last_updated', repo.get('updated_at', 'N/A'))}\n"
    )


def _score_dict_from_parsed(parsed: dict[str, Any]) -> dict[str, int]:
    raw_scores = parsed.get("scores")
    if not isinstance(raw_scores, dict):
        raw_scores = {}
    root = parsed

    def pick(name: str, *aliases: str) -> int:
        for d in (raw_scores, root):
            if not isinstance(d, dict):
                continue
            for k in (name, *aliases):
                if k in d and d[k] is not None:
                    return _coerce_score_1_10(d[k])
        return 5

    return {
        "business_potential": pick("business_potential"),
        "novelty": pick("novelty"),
        "ease_of_mvp": pick("ease_of_mvp", "ease_to_mvp", "easeOfMvp"),
    }


def _coerce_keep_dict(parsed: dict[str, Any], repo: dict) -> dict[str, Any]:
    desc = _ensure_non_empty_str(repo.get("description"), "No description provided.", 2000)
    scores = _score_dict_from_parsed(parsed)
    feats_raw = parsed.get("features") if "features" in parsed else parsed.get("key_features")
    if isinstance(feats_raw, list):
        feats = [str(x).strip() for x in feats_raw if str(x).strip()][:5]
    elif isinstance(feats_raw, str) and feats_raw.strip():
        feats = [feats_raw.strip()[:500]]
    else:
        feats = []

    summary_fb = desc[:800] if len(desc) >= 8 else "Summary not provided by model; see repo description."
    idea_fb = _ensure_non_empty_str(
        parsed.get("product_idea") or parsed.get("opportunity"),
        desc[:400],
        4000,
    )

    if not feats:
        feats = [_ensure_non_empty_str(idea_fb, "See summary for capability.", 200)]

    return {
        "status": "KEEP",
        "summary": _ensure_non_empty_str(parsed.get("summary"), summary_fb, 8000),
        "hidden_capability": _ensure_non_empty_str(
            parsed.get("hidden_capability") or parsed.get("hiddenCapability"),
            "Not specified in model output.",
            8000,
        ),
        "business_mapping": _ensure_non_empty_str(
            parsed.get("business_mapping") or parsed.get("businessMapping"),
            "Not specified in model output.",
            8000,
        ),
        "target_user": _ensure_non_empty_str(
            parsed.get("target_user") or parsed.get("targetUser"),
            "Technical and business users evaluating this repository.",
            2000,
        ),
        "product_idea": idea_fb,
        "target_customer": str(parsed.get("target_customer") or parsed.get("targetCustomer") or "")[:2000],
        "core_workflow": str(parsed.get("core_workflow") or parsed.get("coreWorkflow") or "")[:4000],
        "features": feats,
        "monetization": _ensure_non_empty_str(
            parsed.get("monetization") or parsed.get("monetization_logic"),
            "Monetization not detailed in model output.",
            4000,
        ),
        "scores": scores,
    }


def _parse_llm_json(parsed: dict[str, Any] | None, repo: dict) -> EvalResult | None:
    if not isinstance(parsed, dict):
        return None
    normalized = _normalize_eval_dict(parsed)
    status = normalized.get("status")

    if status == "REJECT":
        try:
            r = RejectEvalSchema(**normalized)
        except ValidationError as exc:
            logger.warning("llm_reject_parse_error", error=str(exc))
            reason = _ensure_non_empty_str(
                normalized.get("reason") or normalized.get("message"),
                "Rejected (model output did not match schema).",
                800,
            )
            return EvalResult(status="REJECT", reason=reason)
        return EvalResult(status="REJECT", reason=r.reason)

    if status == "KEEP":
        try:
            k = KeepEvalSchema(**normalized)
        except ValidationError as exc:
            logger.info("llm_keep_retry_coerce", error=str(exc)[:300])
            try:
                coerced = _coerce_keep_dict(normalized, repo)
                k = KeepEvalSchema(**coerced)
            except ValidationError as exc2:
                logger.warning("llm_keep_parse_error", error=str(exc2))
                return None
        s = k.scores
        return EvalResult(
            status="KEEP",
            business_potential=s.business_potential,
            novelty=s.novelty,
            ease_to_mvp=s.ease_of_mvp,
            summary=k.summary,
            product_idea=k.product_idea,
            monetization=k.monetization,
            features=k.features,
            hidden_capability=k.hidden_capability,
            business_mapping=k.business_mapping,
            target_user=k.target_user,
            target_customer=k.target_customer,
            core_workflow=k.core_workflow,
        )

    return None


def _reject_unparseable(note: str) -> EvalResult:
    return EvalResult(
        status="REJECT",
        reason=_ensure_non_empty_str(note, "Could not parse model response.", 800),
    )


async def _post_chat_completion(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    messages: list[dict[str, str]],
) -> httpx.Response:
    payload: dict[str, Any] = {
        "model": settings.llm,
        "messages": messages,
        "temperature": 0.25,
        "max_tokens": 4096,
    }
    if settings.llm_json_object_mode:
        payload["response_format"] = {"type": "json_object"}

    delays = (0.0, 2.0, 4.0)
    for attempt in range(settings.llm_http_retries + 1):
        if attempt > 0:
            await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 400 and settings.llm_json_object_mode and "response_format" in payload:
                logger.warning("llm_json_object_rejected_retrying_without")
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (429, 502, 503, 504) and attempt < settings.llm_http_retries:
                logger.warning("llm_http_retry", status=code, attempt=attempt + 1)
                continue
            raise


async def evaluate_repo(repo: dict) -> EvalResult | None:
    full_name = str(repo.get("full_name") or repo.get("name") or "")
    readme_excerpt = await fetch_readme_excerpt(full_name) if full_name else "(No repo name.)"
    user_msg = _build_user_message(repo, readme_excerpt)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    headers = {
        "Authorization": f"Bearer {settings.gemini_api_key}",
        "Content-Type": "application/json",
    }
    url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    last_raw = ""

    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(settings.llm_eval_max_attempts):
            try:
                resp = await _post_chat_completion(client, url, headers, messages)
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "llm_http_error",
                    status=exc.response.status_code,
                    body=exc.response.text[:500],
                )
                return None
            except httpx.RequestError as exc:
                logger.error("llm_request_error", error=str(exc))
                return None

            try:
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                logger.error("llm_response_shape_error", error=str(exc))
                return None

            last_raw = (content or "").strip()
            parsed = _parse_json_lenient(last_raw)

            if parsed is None:
                logger.warning(
                    "llm_json_extract_failed",
                    attempt=attempt + 1,
                    snippet=re.sub(r"\s+", " ", last_raw)[:240],
                )
                if attempt + 1 < settings.llm_eval_max_attempts:
                    messages.append({"role": "assistant", "content": last_raw[:8000]})
                    messages.append({"role": "user", "content": _RETRY_USER_MESSAGE})
                continue

            result = _parse_llm_json(parsed, repo)
            if result is not None:
                return result

            if attempt + 1 < settings.llm_eval_max_attempts:
                messages.append({"role": "assistant", "content": last_raw[:8000]})
                messages.append({"role": "user", "content": _RETRY_USER_MESSAGE})
                continue

    return _reject_unparseable(
        "Model returned JSON that could not be interpreted as KEEP or REJECT after retries."
    )


