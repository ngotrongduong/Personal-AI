# Local model stack (v0.4 research + foundation)

This document records the first local-model shortlist for Personal Game AI.

## Important: GitHub Models itself is retired

GitHub's hosted **GitHub Models** playground/catalog/inference API was fully
retired on 2026-07-30. We therefore use model projects published on GitHub and
run their weights through local runtimes instead of depending on the retired
GitHub Models service.

Primary runtime direction:

1. **Ollama** for the easiest Windows model lifecycle and local HTTP API.
2. **llama.cpp / llama-server** as the lower-level GGUF/OpenAI-compatible
   fallback when we need more control.
3. Specialist engines such as PaddleOCR and whisper.cpp stay separate from the
   fast game-reaction loop.

Model weights are **not committed to this repository**.

## Target machine

Current development target: Windows 11, NVIDIA RTX 4070 Ti with 12GB VRAM and
32GB system RAM.

The advertised model context maximum is not a sensible default for this machine:
KV cache also consumes memory. The catalog therefore starts local chat/VLM
models at an 8K runtime context and lets later benchmarks raise it deliberately.

## Recommended roles

| Role | Primary | Why |
| --- | --- | --- |
| planner | `qwen3.5:9b` | Newer unified multimodal Qwen generation; 9B Q4_K_M Ollama artifact is about 6.6GB and supports text + image. |
| planner fallback | `qwen3.5:4b` | Lower latency/footprint for simple strategic decisions. |
| visual reasoner | `qwen3.5:9b` | Reuse the already-loaded planner before paying the cost of a second model. |
| optional visual specialist | `qwen3-vl:4b` | About 3.3GB in Ollama; useful when a smaller dedicated VLM is preferable. |
| semantic memory | `qwen3-embedding:0.6b` | About 639MB; 32K model context; suitable for local retrieval/search. |
| heavy reasoner | `gpt-oss:20b` | Strong optional agentic reasoning; roughly 14GB Ollama artifact / ~16GB official memory target, so not default on 12GB VRAM. |
| OCR specialist | PP-OCRv6 tiny/small | PaddleOCR v3.7 adds PP-OCRv6, including tiny/small tiers and improvements for digital displays / industrial text. |
| speech-to-text | whisper.cpp small | Mature offline ASR runtime with Windows/NVIDIA support. |

## Source repositories reviewed

- `QwenLM/Qwen3.8` — official Qwen3.5/Qwen3.6/Qwen3.8 family repository.
- `QwenLM/Qwen3-VL` — official Qwen vision-language family.
- `QwenLM/Qwen3-Embedding` — embedding/reranking family.
- `openai/gpt-oss` — gpt-oss-20b/120b.
- `PaddlePaddle/PaddleOCR` — PP-OCRv6 and document/OCR stack.
- `ollama/ollama` — primary local runtime.
- `ggml-org/llama.cpp` — GGUF/CUDA/OpenAI-compatible local runtime fallback.
- `ggml-org/whisper.cpp` — local speech recognition runtime.

The Qwen3.5/Qwen3-VL, gpt-oss, and PaddleOCR source repositories use Apache
2.0; Ollama/llama.cpp/whisper.cpp use MIT. Qwen3 Embedding is described by Qwen
as an Apache-2.0 open-weight release, but its GitHub repo still lacks a root
LICENSE file as of this review; if the project ever becomes commercial, verify
the exact weight/model-card license again before distribution.

## Why Qwen3.5-9B is the default planner

Qwen3.5 is multimodal, so one resident model can handle both strategic text and
occasional screenshots. This is more memory-efficient than keeping separate
planner and VLM models loaded at the same time.

Do **not** invoke it every frame. A practical first cadence is event-driven or
every few seconds:

```text
30 FPS capture
  -> deterministic detectors / GameState / rules
  -> immediate safe ActionDispatcher input

on important state change or slow planning interval:
  -> compact state summary (+ screenshot only when useful)
  -> local planner/VLM
  -> proposed high-level goal
  -> deterministic rules/actions still enforce execution safety
```

## Model catalog and router

`configs/models.v1.json` is the versioned catalog.

`model_runtime/` adds:

- provider-neutral message/response types
- validated role/model catalog
- Ollama `list/chat/embed` provider
- role router that chooses the first **installed** compatible model

There is intentionally **no automatic pull** in application code. If no
candidate is installed, the router raises a clear `ModelNotAvailableError`.

## Windows setup

Check current state:

```powershell
.\scripts\models.ps1 status
```

Pull only the recommended initial pair:

```powershell
.\scripts\models.ps1 pull-recommended
```

That pulls:

- `qwen3.5:9b`
- `qwen3-embedding:0.6b`

Optional:

```powershell
.\scripts\models.ps1 pull-vision
.\scripts\models.ps1 pull-heavy
```

`pull-heavy` warns before downloading gpt-oss-20b because it is not the default
fit for the current 12GB-VRAM target.

## Specialist models not yet wired

### PaddleOCR PP-OCRv6

Keep the v0.3 Tesseract OCR path as the baseline. A future PaddleOCR provider can
be added when real game UI samples show Tesseract is insufficient. PP-OCRv6 is
especially interesting because its 2026 release explicitly improved digital
display and industrial text recognition.

### whisper.cpp

Voice is a later milestone. whisper.cpp is cataloged now so the eventual voice
layer has an agreed local/offline direction rather than another provider choice
being made ad hoc.

## gpt-oss-20b

Keep this optional. It is attractive for deeper reasoning/tool use, but its
memory footprint is a poor default match for a 12GB GPU. It may still be useful
through CPU/system-RAM offload for occasional slow analysis.

## Non-goals of this foundation

- no model call in the frame-by-frame reaction path
- no model-generated mouse/keyboard event that bypasses ActionDispatcher
- no implicit model download
- no cloud API key requirement
- no committed model weights/cache
- no claim that a catalog entry is installed until the provider reports it
