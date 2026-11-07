from __future__ import annotations

import requests
from urllib.parse import urlparse
from core.config import OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS


class OllamaClient:
    """Local-only Ollama client. It never sends prompts to a cloud endpoint."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL, model: str = OLLAMA_MODEL):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path not in {"", "/"}:
            raise ValueError("For this zero-cost build, Ollama must use http://127.0.0.1 or http://localhost")
        self.base_url = base_url.rstrip("/")
        self.model = model

    def status(self) -> tuple[bool, list[str], str]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
            models = [m.get("name", "") for m in response.json().get("models", [])]
            return True, models, "Ollama is reachable on this computer."
        except requests.RequestException as exc:
            return False, [], f"Ollama is not reachable: {exc.__class__.__name__}"
        except (ValueError, KeyError):
            return False, [], "Ollama returned an unexpected response."

    def chat(self, system: str, user: str, timeout: int = OLLAMA_TIMEOUT_SECONDS) -> str:
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.25, "num_ctx": 4096},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "").strip()


def draft_proposal(opportunity: dict, client: OllamaClient | None = None) -> tuple[str, bool]:
    client = client or OllamaClient()
    system = (
        "You draft honest, concise freelance proposals. Never claim experience, credentials, "
        "portfolio results, or capabilities that were not supplied. If information is missing, "
        "use a clearly marked placeholder. Do not promise an unrealistic deadline."
    )
    user = f"""Draft a proposal for the opportunity below. Do not send it.
Title: {opportunity.get('title','')}
Description: {opportunity.get('description','')[:3000]}
Budget: INR {opportunity.get('amount_inr', 0)}
Estimated effort: {opportunity.get('estimated_hours', 0)} hours
Create: short opening, understanding of scope, proposed deliverables, clarification question, and close."""
    try:
        return client.chat(system, user), True
    except Exception:
        return (
            "DRAFT — REVIEW BEFORE USE\n\n"
            f"Hello,\n\nI reviewed your requirement for: {opportunity.get('title', 'this project')}. "
            "I can help by first confirming the input data, expected output format, acceptance criteria, "
            "and deadline.\n\n"
            "Proposed next step: please share a sample input and confirm the required deliverables. "
            "I will then confirm scope and timeline before starting.\n\n"
            "Regards,\n[Your name]\n\n"
            "Note: This is a template fallback. Personalize it and verify every claim before sending.",
            False,
        )
