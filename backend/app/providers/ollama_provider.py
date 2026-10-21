"""
OllamaProvider: the single provider implementation for this project (Capstone_Roadmap
Section 1 -- 3 free local LLMs via Ollama, no paid APIs). Qwen2.5-Coder, Llama 3.1,
and gpt-oss/Mistral are all called through this one class; which model runs is just
the `model` argument, so the same runModel-style call is identical across all three
(roadmap Week 1, "Ollama orchestration layer" task).
"""

from __future__ import annotations

import os
from typing import Optional

import ollama

from app.providers.base import BaseLLMProvider, ProviderCallError
from app.providers.pricing import ollama_cost_usd
from app.schemas.llm_result import ProviderName

DEFAULT_HOST = "http://localhost:11434"


class OllamaProvider(BaseLLMProvider):
    provider_name = ProviderName.OLLAMA
    default_model = "qwen2.5-coder:7b"

    def __init__(
        self,
        host: Optional[str] = None,
        num_ctx: Optional[int] = None,
        temperature: float = 0.0,
        seed: int = 42,
        timeout: float = 900.0,
    ):
        # Request timeout. Without one, a genuinely wedged call (as opposed to a
        # slow-but-finishing one) blocks forever and freezes the whole batch -- the
        # failure mode that cost three multi-hour battery attempts. A generation that
        # completes within the window is untouched; only a hung call is killed, and
        # the harness records it as run_status=failed instead of hanging. Read from
        # OLLAMA_TIMEOUT_S, same pattern as num_ctx below.
        env_timeout = os.environ.get("OLLAMA_TIMEOUT_S")
        self._timeout = float(env_timeout) if env_timeout else timeout
        self._client = ollama.Client(
            host=host or os.environ.get("OLLAMA_HOST", DEFAULT_HOST),
            timeout=self._timeout,
        )
        # Context window. Ollama defaults these models to ~2-4K tokens, which silently
        # truncates the larger raw-code context; set OLLAMA_NUM_CTX (or pass num_ctx) to
        # widen it. Left as None -> use the model's own default. When raising this, the
        # raw-source cap in pipeline.DEFAULT_MAX_RAW_CHARS can be raised to match.
        env_ctx = os.environ.get("OLLAMA_NUM_CTX")
        self._num_ctx = num_ctx if num_ctx is not None else (int(env_ctx) if env_ctx else None)
        # Without these, each model ran at its own Modelfile-default sampling
        # temperature (which differs per model) with no fixed seed -- so results
        # weren't reproducible run-to-run, and cross-model comparisons carried an
        # extra, undisclosed source of variance beyond the model itself. Pinned to
        # deterministic decoding: this is structured JSON extraction from a fixed
        # context, not creative generation, so temperature=0 is the methodologically
        # correct choice here, not just a convenience.
        self._temperature = temperature
        self._seed = seed

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def temperature(self) -> float:
        return self._temperature

    def _call_model(
        self, model: str, system_prompt: str, user_prompt: str
    ) -> tuple[str, int, int]:
        options: dict = {"temperature": self._temperature, "seed": self._seed}
        if self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx
        try:
            response = self._client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                options=options,
            )
        except Exception as e:
            # Covers ollama.ResponseError (e.g. model not pulled) and connection
            # failures (e.g. Ollama daemon not running) -- both unrecoverable here,
            # unlike the rate-limit/timeout cases BaseLLMProvider retries internally.
            raise ProviderCallError(f"Ollama call failed for model '{model}': {e}") from e

        return (
            response.message.content or "",
            response.prompt_eval_count or 0,
            response.eval_count or 0,
        )

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return ollama_cost_usd(model, input_tokens, output_tokens)
