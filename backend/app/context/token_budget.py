"""Token budgets for the rendered contexts, counted in the generator's OWN tokens.

The budget used to be counted with tiktoken's cl100k_base for every model. That is a
close proxy for Qwen2.5's tokenizer but not for CodeLlama's 32k-entry Llama-2 vocabulary,
which spends ~33% more tokens on the same source text. Contexts sized to fit under
cl100k therefore overflowed CodeLlama's 8,192-token window on 14 of its 53 successful
prompts, and Ollama, faced with an over-long prompt, keeps only about half the window
-- every one of those rows was recorded at exactly 4,098 input tokens, with the task
instruction at the front of the prompt discarded
(evaluation_results/REVISION_ANALYSIS.txt).

So: when the generator is known, count with its tokenizer. `model=None` keeps the
cl100k proxy for the live API path, which serves whatever model the user picks.
"""

import logging
import os
from typing import Optional, Protocol

from app.providers.prompts import SUMMARY_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)

# The Ollama tag a battery uses -> the Hugging Face repo holding that model's tokenizer.
# Only tokenizer.json is fetched (~2-7 MB), never weights. qwen2.5-coder 14B and 32B
# share one tokenizer.
MODEL_TOKENIZERS = {
    "codellama:13b-instruct": "codellama/CodeLlama-13b-Instruct-hf",
    "qwen2.5-coder:14b": "Qwen/Qwen2.5-Coder-14B-Instruct",
    "qwen2.5-coder:32b": "Qwen/Qwen2.5-Coder-32B-Instruct",
}

SYSTEM_PROMPT = "You are a repository analysis assistant. Respond only with valid JSON."
# Ollama wraps system + user turns in the model's chat template, which the prompt text
# alone does not contain. Measured at 13-17 tokens against recorded prompt_eval_count;
# 64 leaves headroom for templates we have not measured.
CHAT_TEMPLATE_MARGIN = 64
# Renderers append an uncounted "... N more modules omitted" marker after the last line
# that fits, and token counts are not exactly additive across the "\n" joins. Measured
# overshoot on the 18 study repos was at most 8 tokens; 32 covers it with room.
RENDER_SLACK = 32


class Encoder(Protocol):
    def encode(self, text: str) -> list[int]: ...
    def decode(self, ids: list[int]) -> str: ...


class _HFEncoder:
    """Adapts a `tokenizers.Tokenizer` to the encode()/decode() shape of tiktoken's
    Encoding, which is what every renderer calls. Special tokens are not added: the
    renderers count context text, and the chat template is covered by the margin."""

    def __init__(self, tokenizer):
        self._tok = tokenizer

    def encode(self, text: str) -> list[int]:
        return self._tok.encode(text, add_special_tokens=False).ids

    def decode(self, ids: list[int]) -> str:
        return self._tok.decode(ids)


_encoder_cache: dict[Optional[str], Encoder] = {}
_budget_cache: dict[tuple[Optional[str], int], int] = {}


def get_encoder(model: Optional[str] = None) -> Encoder:
    if model not in _encoder_cache:
        if model is None:
            import tiktoken

            _encoder_cache[None] = tiktoken.get_encoding("cl100k_base")
        else:
            if model not in MODEL_TOKENIZERS:
                raise KeyError(
                    f"No tokenizer registered for {model!r}. Add its Hugging Face repo to "
                    "MODEL_TOKENIZERS -- counting it with another model's tokenizer is the "
                    "exact bug this module exists to prevent."
                )
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            path = hf_hub_download(MODEL_TOKENIZERS[model], "tokenizer.json")
            _encoder_cache[model] = _HFEncoder(Tokenizer.from_file(path))
    return _encoder_cache[model]


def compute_context_token_budget(reserved_output_tokens: int = 1200, model: Optional[str] = None) -> int:
    key = (model, reserved_output_tokens)
    if key in _budget_cache:
        return _budget_cache[key]

    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
    enc = get_encoder(model)
    if model is None:
        # Legacy proxy accounting, unchanged so the live API renders as before.
        overhead = 50 + len(enc.encode(SUMMARY_PROMPT_TEMPLATE.format(context="")))
    else:
        overhead = (
            len(enc.encode(SYSTEM_PROMPT))
            + len(enc.encode(SUMMARY_PROMPT_TEMPLATE.format(context="")))
            + CHAT_TEMPLATE_MARGIN
            + RENDER_SLACK
        )

    budget = num_ctx - overhead - reserved_output_tokens
    _budget_cache[key] = budget
    logger.info(
        "Context token budget for %s: %d tokens (OLLAMA_NUM_CTX=%d)",
        model or "cl100k proxy", budget, num_ctx,
    )
    return budget


def prompt_tokens(context: str, model: str) -> int:
    """Full prompt length in `model`'s tokens, chat-template margin included -- what
    Ollama will see. Used to verify a rendered context fits before it is packed."""
    enc = get_encoder(model)
    return (
        len(enc.encode(SYSTEM_PROMPT))
        + len(enc.encode(SUMMARY_PROMPT_TEMPLATE.format(context=context)))
        + CHAT_TEMPLATE_MARGIN
    )
