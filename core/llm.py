from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse
from typing import Any

import requests
from core.config import OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS

logger = logging.getLogger("soloearn.llm")


class OllamaError(RuntimeError):
    """Base exception for Ollama client errors."""
    pass


class OllamaOfflineError(OllamaError):
    """Raised when the local Ollama daemon is unreachable or offline."""
    pass


class OllamaTimeoutError(OllamaError):
    """Raised when Ollama inference exceeds the configured latency timeout."""
    pass


class OllamaCircuitBreakerOpenError(OllamaOfflineError):
    """Raised when consecutive failures have tripped the circuit breaker to prevent blocking calls."""
    pass


class OllamaCircuitBreaker:
    """
    Prevents cascading stalls when local Ollama is offline or experiencing severe latency.
    Trips to OPEN state after failure_threshold consecutive errors, allowing the agent
    to immediately bypass network calls and engage deterministic heuristics without delay.
    Tracks latency metrics and detects slow-running local inference.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        slow_threshold_ms: float = 8000.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.slow_threshold_ms = slow_threshold_ms
        self.consecutive_failures = 0
        self.state = "CLOSED"  # "CLOSED", "OPEN", "HALF_OPEN"
        self.last_failure_time = 0.0
        self.last_latency_ms = 0.0
        self.latency_samples: list[float] = []
        self.total_requests = 0
        self.total_failures = 0

    def can_attempt(self) -> bool:
        """Returns True if a request should be attempted; False if the breaker is OPEN."""
        if self.state == "CLOSED":
            return True
        now = time.monotonic()
        if self.state == "OPEN":
            if (now - self.last_failure_time) >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                logger.info("[OLLAMA CIRCUIT BREAKER] Recovery timeout elapsed; probe request permitted in HALF_OPEN state.")
                return True
            return False
        # HALF_OPEN: allow a single probe request
        return True

    def record_success(self, latency_ms: float = 0.0) -> None:
        """Records a successful request, closing the circuit."""
        self.consecutive_failures = 0
        self.state = "CLOSED"
        self.last_latency_ms = latency_ms
        self.latency_samples.append(latency_ms)
        if len(self.latency_samples) > 20:
            self.latency_samples.pop(0)
        self.total_requests += 1

    def record_failure(self, is_timeout: bool = False) -> None:
        """Records a request failure and trips the breaker if threshold is exceeded."""
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_requests += 1
        self.last_failure_time = time.monotonic()
        if self.consecutive_failures >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(
                f"[OLLAMA CIRCUIT BREAKER TRIPPED] State set to OPEN after {self.consecutive_failures} "
                f"consecutive failures (timeout={is_timeout}). Fast heuristic fallback engaged for {self.recovery_timeout}s."
            )

    def avg_latency_ms(self) -> float:
        """Returns rolling average latency of recent successful requests in milliseconds."""
        if not self.latency_samples:
            return round(self.last_latency_ms, 2)
        return round(sum(self.latency_samples) / len(self.latency_samples), 2)

    def is_slow(self) -> bool:
        """Returns True if recent average or last latency exceeds the configured slow threshold."""
        return (self.last_latency_ms > self.slow_threshold_ms) or (self.avg_latency_ms() > self.slow_threshold_ms)

    def reset(self) -> None:
        """Resets the circuit breaker state to CLOSED."""
        self.consecutive_failures = 0
        self.state = "CLOSED"
        self.last_failure_time = 0.0
        self.latency_samples.clear()
        self.last_latency_ms = 0.0


class OllamaClient:
    """
    Local-only Ollama client with circuit breaker resilience, latency tracking,
    and fast offline/slow heuristics. It never sends prompts to any cloud endpoint.
    """

    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        model: str = OLLAMA_MODEL,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        slow_threshold_ms: float = 8000.0,
        max_retries: int = 1,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path not in {"", "/"}:
            raise ValueError("For this zero-cost build, Ollama must use http://127.0.0.1 or http://localhost")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.slow_threshold_ms = slow_threshold_ms
        self.circuit_breaker = OllamaCircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            slow_threshold_ms=slow_threshold_ms,
        )

    def status(self) -> tuple[bool, list[str], str]:
        """Checks if the local Ollama daemon is reachable and lists loaded models."""
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
            models = [m.get("name", "") for m in response.json().get("models", [])]
            return True, models, "Ollama is reachable on this computer."
        except requests.RequestException as exc:
            return False, [], f"Ollama is not reachable: {exc.__class__.__name__}"
        except (ValueError, KeyError):
            return False, [], "Ollama returned an unexpected response."

    def quick_ping(self, timeout: float = 1.0) -> bool:
        """Fast non-blocking check to determine if the local daemon port is responding."""
        if not self.circuit_breaker.can_attempt():
            return False
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def is_slow(self) -> bool:
        """Indicates whether local inference latency is exceeding acceptable thresholds."""
        return self.circuit_breaker.is_slow()

    def chat(
        self,
        system: str,
        user: str,
        timeout: int = OLLAMA_TIMEOUT_SECONDS,
        format: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> str:
        """
        Executes a prompt completion against the local Ollama server.
        Raises OllamaCircuitBreakerOpenError immediately if breaker is open.
        Includes single retry with exponential backoff on transient connection errors.
        """
        if not self.circuit_breaker.can_attempt():
            raise OllamaCircuitBreakerOpenError(
                f"Ollama circuit breaker is OPEN ({self.circuit_breaker.consecutive_failures} consecutive failures). "
                "Bypassing network call; fallback engaged."
            )

        opt = {"temperature": 0.25, "num_ctx": 4096}
        if options:
            opt.update(options)

        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "think": False,
            "options": opt,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if format:
            payload["format"] = format

        attempts = 0
        max_attempts = 1 + max(0, self.max_retries)

        while attempts < max_attempts:
            attempts += 1
            start_time = time.perf_counter()
            try:
                response = requests.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=timeout,
                )
                response.raise_for_status()
                content = response.json().get("message", {}).get("content", "").strip()
                latency_ms = (time.perf_counter() - start_time) * 1000
                self.circuit_breaker.record_success(latency_ms)
                if latency_ms > self.slow_threshold_ms:
                    logger.warning(
                        f"[OLLAMA LATENCY WARNING] Inference completed in {latency_ms:.1f}ms (threshold: {self.slow_threshold_ms}ms)."
                    )
                return content
            except requests.Timeout as exc:
                self.circuit_breaker.record_failure(is_timeout=True)
                raise OllamaTimeoutError(
                    f"Ollama inference timed out after {timeout} seconds on model '{self.model}'"
                ) from exc
            except (requests.ConnectionError, requests.RequestException) as exc:
                if attempts < max_attempts:
                    logger.info(f"Ollama transient connection hiccup ({exc.__class__.__name__}); retrying in 200ms...")
                    time.sleep(0.2)
                    continue
                self.circuit_breaker.record_failure(is_timeout=False)
                raise OllamaOfflineError(
                    f"Ollama daemon unreachable at {self.base_url}: {exc.__class__.__name__}"
                ) from exc
            except Exception as exc:
                self.circuit_breaker.record_failure(is_timeout=False)
                raise OllamaError(f"Unexpected Ollama error: {exc}") from exc

    def chat_safe(
        self,
        system: str,
        user: str,
        timeout: int = OLLAMA_TIMEOUT_SECONDS,
        format: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        """
        Safe wrapper around chat that intercepts offline, timeout, and breaker errors
        and returns (content, error_code). Error code is None on success.
        Error codes: 'circuit_breaker_open', 'timeout', 'offline', 'error'.
        """
        try:
            res = self.chat(system, user, timeout=timeout, format=format, options=options)
            return res, None
        except OllamaCircuitBreakerOpenError:
            return None, "circuit_breaker_open"
        except OllamaTimeoutError:
            return None, "timeout"
        except OllamaOfflineError:
            return None, "offline"
        except Exception as exc:
            logger.debug(f"Ollama chat error: {exc}")
            return None, "error"

    def get_health_metrics(self) -> dict[str, Any]:
        """Returns diagnostic health metrics for telemetry and observability."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "circuit_state": self.circuit_breaker.state,
            "consecutive_failures": self.circuit_breaker.consecutive_failures,
            "last_latency_ms": round(self.circuit_breaker.last_latency_ms, 2),
            "avg_latency_ms": self.circuit_breaker.avg_latency_ms(),
            "is_slow": self.circuit_breaker.is_slow(),
            "slow_threshold_ms": self.slow_threshold_ms,
            "total_requests": self.circuit_breaker.total_requests,
            "total_failures": self.circuit_breaker.total_failures,
        }


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
