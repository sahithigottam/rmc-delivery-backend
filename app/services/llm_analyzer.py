"""
LLM Analyzer — Natural language risk assessment via Ollama (qwen2.5:3b).

DESIGN RULE:
  The LLM only reasons and explains. It NEVER calculates.
  All numbers (ETA, buffer, probability) are pre-computed by DeliveryPredictor.
  The LLM receives those numbers as facts and explains them in plain English.
  This prevents hallucinated numbers and keeps the system fully auditable.

Fallback:
  If Ollama is unreachable or returns bad JSON, a deterministic template
  produces the same JSON schema — the API response shape never changes.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from app.rmc.predictor import DeliveryPrediction

logger = logging.getLogger(__name__)


class LLMAnalyzer:
    """
    Generates structured delivery risk assessments via a local Ollama model.

    Usage:
        analyzer = LLMAnalyzer()          # one instance, reuse the httpx client
        result   = await analyzer.analyze(prediction, route_desc="Plant → Site")

    Output schema (always — whether LLM or template fallback):
        {
          "source":            "llm:qwen2.5:3b" | "template",
          "risk_summary":      str,   # 1-2 sentence plain English risk summary
          "key_concern":       str,   # single most important risk factor
          "recommendation":    str,   # specific dispatcher action
          "confidence":        str,   # "HIGH" | "MEDIUM" | "LOW"
          "confidence_reason": str,   # why this confidence level
        }
    """

    MODEL       = "qwen2.5:3b"   # pinned — change this one line to swap model
    OLLAMA_URL  = "http://localhost:11434/api/generate"
    OLLAMA_TAGS = "http://localhost:11434/api/tags"

    def __init__(self):
        self._client         = httpx.AsyncClient(timeout=90.0)
        self.available_model: Optional[str] = self.MODEL
        self._ollama_checked  = False

    async def _ensure_checked(self) -> bool:
        """Lazy-check Ollama on first use."""
        if self._ollama_checked:
            return self.available_model is not None
        self._ollama_checked = True
        try:
            resp = await self._client.get(self.OLLAMA_TAGS, timeout=5.0)
            if resp.status_code != 200:
                self.available_model = None
                return False
            installed = [m["name"] for m in resp.json().get("models", [])]
            match = next((m for m in installed if self.MODEL in m), None)
            if match:
                self.available_model = match
                logger.info(f"LLMAnalyzer: {match} ready")
                return True
            else:
                logger.warning(
                    f"LLMAnalyzer: {self.MODEL} not found. "
                    f"Run: ollama pull {self.MODEL}. "
                    f"Template fallback active."
                )
                self.available_model = None
                return False
        except Exception as e:
            logger.warning(f"LLMAnalyzer: Ollama unreachable ({e}). Template fallback active.")
            self.available_model = None
            return False

    def _build_prompt(self, pred: DeliveryPrediction, route_desc: str) -> str:
        """
        Build a focused prompt that gives the LLM pre-computed facts only.
        The LLM must ONLY reason and explain — never recalculate.
        """
        risk_lines = "\n".join(
            f"  {i+1}. [{r['impact'].upper()}] {r['name']}: {r['detail']}"
            for i, r in enumerate(pred.risk_factors)
        ) or "  None identified"

        return f"""You are an RMC (Ready-Mix Concrete) delivery logistics expert in New Zealand.
All numbers below have been pre-calculated by a trusted system. Do NOT recalculate them.
Your job is to reason over these facts and explain the risk in plain English to a dispatcher.

--- DELIVERY FACTS ---
Route          : {route_desc or pred.route_name or "Unknown route"}
Google ETA     : {pred.google_eta_minutes:.0f} min  (live traffic, pessimistic model)
Adjusted ETA   : {pred.adjusted_eta_minutes:.0f} min  (NZTA correction applied)
Concrete limit : {pred.setting_time_minutes:.0f} min  (NZS 3109:1997)
Time buffer    : {pred.time_buffer_minutes:.0f} min
Success prob   : {pred.success_probability:.0%}
Risk level     : {pred.risk_level.upper()}
Weather        : {pred.weather_condition}  (speed factor {pred.weather_impact:.2f}x)
P50/P75/P90/P95: {pred.p50_duration_minutes:.0f} / {pred.p75_duration_minutes:.0f} / {pred.p90_duration_minutes:.0f} / {pred.p95_duration_minutes:.0f} min
Historical     : {pred.route_success_rate:.0%} success over {pred.route_trips} past trips

Risk factors identified:
{risk_lines}
--- END FACTS ---

Reply with ONLY valid JSON — no extra text, no markdown, no code fences:
{{
  "risk_summary": "1-2 sentence plain English summary of the risk situation",
  "key_concern": "The single most important risk factor in one sentence",
  "recommendation": "Specific actionable advice for the dispatcher — dispatch now / delay X min / use retarder / source from closer plant",
  "confidence": "HIGH or MEDIUM or LOW",
  "confidence_reason": "Brief reason for confidence level"
}}"""

    def _parse_json_response(self, raw: str) -> Optional[dict]:
        """
        Robustly parse JSON from LLM output.
        Handles markdown fences and surrounding text.
        """
        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?", "", raw).strip()

        # Direct parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Extract first {...} block
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None

    async def analyze(
        self,
        pred: DeliveryPrediction,
        route_desc: str = "",
    ) -> dict:
        """
        Generate a structured risk assessment for the dispatcher.

        Always returns a dict with keys:
            source, risk_summary, key_concern, recommendation,
            confidence, confidence_reason
        """
        await self._ensure_checked()

        if self.available_model:
            prompt = self._build_prompt(pred, route_desc)
            for attempt in range(2):
                try:
                    resp = await self._client.post(
                        self.OLLAMA_URL,
                        json={
                            "model": self.available_model,
                            "prompt": prompt,
                            "stream": False,
                            "format": "json",          # forces valid JSON at token level
                            "options": {
                                "temperature": 0.2,
                                "top_p": 0.9,
                                "num_predict": 400,
                                "num_ctx": 131072,
                            },
                        },
                        timeout=90.0,
                    )
                    if resp.status_code == 200:
                        raw    = resp.json().get("response", "")
                        parsed = self._parse_json_response(raw)
                        if parsed:
                            return {"source": f"llm:{self.available_model}", **parsed}
                        logger.warning(f"LLMAnalyzer: attempt {attempt+1} non-JSON response")
                except Exception as e:
                    logger.warning(f"LLMAnalyzer: attempt {attempt+1} failed: {e}")

            logger.info("LLMAnalyzer: falling back to template")

        return {"source": "template", **self._template_analysis(pred)}

    def _template_analysis(self, pred: DeliveryPrediction) -> dict:
        """
        Rule-based fallback. Returns identical JSON schema as LLM output.
        Active when Ollama is down or model is missing.
        """
        summaries = {
            "low":      (
                f"Low risk — {pred.success_probability:.0%} success probability with "
                f"{pred.time_buffer_minutes:.0f} min buffer. Safe to dispatch."
            ),
            "moderate": (
                f"Moderate risk — {pred.success_probability:.0%} success probability. "
                f"{pred.time_buffer_minutes:.0f} min buffer requires active monitoring."
            ),
            "high":     (
                f"HIGH RISK — {pred.success_probability:.0%} success probability. "
                f"{pred.time_buffer_minutes:.0f} min buffer is dangerously thin."
            ),
            "critical": (
                f"CRITICAL — {pred.success_probability:.0%} success probability. "
                f"Concrete is very likely to set before arrival."
            ),
        }
        recommendations = {
            "low":      "Dispatch now. Conditions are favourable.",
            "moderate": "Dispatch with caution. Monitor traffic en route. Have reroute plan ready.",
            "high":     "Consider delaying dispatch, using retarder admixture, or sourcing from closer plant.",
            "critical": "Do NOT dispatch. Use retarder mix, source from closer plant, or reschedule.",
        }
        key = pred.risk_level if pred.risk_level in summaries else "moderate"
        top_risk = pred.risk_factors[0] if pred.risk_factors else {
            "name": "General conditions", "detail": "No specific critical concerns."
        }
        conf_reason = (
            f"Based on {pred.route_trips} historical trips — statistically reliable."
            if pred.route_trips >= 30
            else (
                f"NZTA baseline used — {pred.route_trips} real trips logged so far. "
                f"Accuracy improves with more data."
            )
        )
        return {
            "risk_summary":      summaries[key],
            "key_concern":       f"{top_risk['name']}: {top_risk['detail']}",
            "recommendation":    recommendations[key],
            "confidence":        pred.confidence.upper(),
            "confidence_reason": conf_reason,
        }

    async def compare_plants(
        self,
        plants_data: list[dict],
        job_site: str,
        concrete_mix: str,
    ) -> dict:
        """
        Single LLM call that compares all plant prediction results
        and recommends the best plant/route for the dispatcher.
        """
        await self._ensure_checked()

        if not self.available_model or not plants_data:
            return self._template_comparison(plants_data)

        prompt = self._build_comparison_prompt(plants_data, job_site, concrete_mix)
        for attempt in range(2):
            try:
                resp = await self._client.post(
                    self.OLLAMA_URL,
                    json={
                        "model": self.available_model,
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                        "options": {
                            "temperature": 0.2,
                            "top_p": 0.9,
                            "num_predict": 600,
                            "num_ctx": 131072,
                        },
                    },
                    timeout=120.0,
                )
                if resp.status_code == 200:
                    raw = resp.json().get("response", "")
                    parsed = self._parse_json_response(raw)
                    if parsed:
                        return {"source": f"llm:{self.available_model}", **parsed}
                    logger.warning("LLMAnalyzer compare: attempt %d non-JSON", attempt + 1)
            except Exception as e:
                logger.warning("LLMAnalyzer compare: attempt %d failed: %s", attempt + 1, e)

        logger.info("LLMAnalyzer compare: falling back to template")
        return self._template_comparison(plants_data)

    def _build_comparison_prompt(
        self,
        plants_data: list[dict],
        job_site: str,
        concrete_mix: str,
    ) -> str:
        rows = []
        for i, p in enumerate(plants_data, 1):
            rows.append(
                f"  {i}. {p['plant_name']} ({p['plant_address']})\n"
                f"     Google ETA: {p['google_eta']:.0f} min | "
                f"Adjusted ETA: {p['adjusted_eta']:.0f} min | "
                f"Remaining life: {p['remaining_life']:.0f} min\n"
                f"     Risk: {p['risk_level']} | "
                f"Success prob: {p['success_prob']:.0%} | "
                f"Buffer depletion: {p['buffer_depletion']:.0%}"
            )
        plant_block = "\n".join(rows)

        return f"""You are an RMC (Ready-Mix Concrete) delivery logistics expert in New Zealand.
All numbers below have been pre-calculated by a trusted system. Do NOT recalculate them.
Your job is to compare these plant options and recommend the BEST plant for this delivery.

--- DELIVERY REQUEST ---
Job site     : {job_site}
Concrete mix : {concrete_mix}

--- PLANT OPTIONS (pre-computed) ---
{plant_block}
--- END ---

Reply with ONLY valid JSON — no extra text, no markdown, no code fences:
{{
  "best_plant": "Name of the recommended plant",
  "reason": "2-3 sentence explanation of why this plant is the best choice",
  "risk_summary": "Overall risk assessment for the recommended route",
  "alternative": "Name of the next-best plant if the recommended one is unavailable",
  "alternative_reason": "Why this is the backup option",
  "dispatch_advice": "Specific advice for the dispatcher (e.g. dispatch now, use retarder, delay)"
}}"""

    @staticmethod
    def _template_comparison(plants_data: list[dict]) -> dict:
        if not plants_data:
            return {
                "source": "template",
                "best_plant": "Unknown",
                "reason": "No plant data available for comparison.",
                "risk_summary": "Unable to assess.",
                "alternative": "N/A",
                "alternative_reason": "N/A",
                "dispatch_advice": "Check plant availability manually.",
            }
        best = plants_data[0]
        alt = plants_data[1] if len(plants_data) > 1 else None
        return {
            "source": "template",
            "best_plant": best["plant_name"],
            "reason": (
                f"Highest remaining concrete life ({best['remaining_life']:.0f} min) "
                f"with {best['success_prob']:.0%} success probability."
            ),
            "risk_summary": f"Risk level: {best['risk_level']}.",
            "alternative": alt["plant_name"] if alt else "N/A",
            "alternative_reason": (
                f"Second-best remaining life ({alt['remaining_life']:.0f} min)."
                if alt else "No alternative available."
            ),
            "dispatch_advice": (
                "Dispatch now." if best["risk_level"] == "low"
                else "Dispatch with caution — monitor traffic."
            ),
        }

    async def close(self):
        """Close the HTTP client. Called on app shutdown."""
        await self._client.aclose()
