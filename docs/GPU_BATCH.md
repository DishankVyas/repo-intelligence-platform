# Running the battery on the SVKM AI/ML cluster

For the H100 reached through Altair Access at `https://10.126.1.10:4443`.

## What you actually get

One compute node: 2× Xeon Gold 6438Y+ (32 cores), 512 GB RAM, 2× H100 80 GB.

The cards are **MIG-partitioned into four ~40 GB slices**, and a job gets one slice —
not a whole H100. That is still ample: a 7B 4-bit model needs about 6 GB, so it sits
entirely in VRAM with no CPU offload, which is where nearly all the speedup over the
laptop comes from. But four slices serve the whole university, so expect to queue.

Scheduler is PBS Professional; you never write PBS directives, the portal form sets
resources for you.

## Two form fields that will kill the job

| Field | Default | Set it to |
|---|---|---|
| **Amount of Memory (MB)** | `10` | `32000` |
| **Number of Processors per Node** | `1` | `8` |

Ten megabytes is not a typo in this document — it is the portal's default, and the job
dies immediately. One CPU core survives generation (which is GPU-bound) but makes
everything around it crawl.

## Why the pipeline is split

Of the pipeline, only generation wants a GPU:

| Stage | Needs | Cost |
|---|---|---|
| clone, tree-sitter parse | network, CPU | seconds |
| Neo4j write, context render | a database, CPU | seconds |
| **generation + judging** | **GPU** | **hours** |
| oracle scoring | CPU only | under a minute |

There is no Neo4j on the cluster and the compute node may have no route to `github.com`,
so the first two stages cannot run there anyway. Instead you build a **context pack**
off-cluster — one file holding, per repository, the parse plus all three rendered
contexts — and the job reads that. A packed run is also exactly reproducible, which a
run from URLs is not: upstream repositories move.

The pack is tiny. Measured on the current 18 repositories it is ~1.5 MB; 50 repositories
would be ~4 MB.

## Models: who writes, who judges

| Role | Model | Why |
|---|---|---|
| Generator | `qwen2.5-coder:14b` | code-specialised; in every battery so far |
| Generator | `codellama:13b-instruct` | code-specialised; in every battery so far |
| Generator | `qwen2.5-coder:32b` | the roadmap's original third model, restored |
| **Judge** | `gemma2:9b` | **judge only, never a generator** |

`qwen2.5-coder:32b` was specified in the original plan and dropped because it needs ~13 GB of
weights and the dev laptop had 16 GB total. `gemma2:9b` was substituted as the third
generator — which quietly created a self-judging collision, since gemma2 is also the
judge. A 40 GB slice fixes both at once: the planned roster is restored, and gemma2 goes
back to judging only, so no arm grades its own output (`REPORT.md` §6.2).

Verify the tag resolves before a full run — `ollama pull qwen2.5-coder:32b`. If it does not,
find the exact tag on ollama.com and use that.

## Why Ollama, not PyTorch or vLLM

Ollama serves **4-bit quantized** weights, and every result so far came from those. vLLM
or HF in fp16 would use the H100 far better and produce *different text from the same
model* — a different experimental condition, under which none of the existing 157
summaries remain comparable.

That is a study-design decision, not a performance one. If you want native precision
later, add it as a separate arm and report the quantization gap as a finding. Do not
silently swap the engine.

## Getting the GPU actually working for you

Three settings, and one of them matters far more than the rest.

**Keep the generator and the judge loaded together** (`OLLAMA_MAX_LOADED_MODELS=2`).
The battery makes two judge calls per generation, so with one model resident every
single row pays for an evict-and-reload. On the 16 GB laptop that thrash moved ~15 GB
through a 9.7 GB budget per row and dominated the run. `qwen2.5-coder:32b` (~13 GB) plus
`gemma2:9b` (~6 GB) is under half a 40 GB slice, so it simply disappears. This is purely
a scheduling change — same weights, same context size, same deterministic decoding — so
outputs are unaffected.

**Leave `OLLAMA_NUM_PARALLEL=1`.** Raising it batches concurrent requests, which changes
the order of floating-point reductions and can therefore change generated text. You
already get throughput from running three generators as three parallel jobs; buying more
at the cost of reproducibility is a bad trade in a study.

**The real speedup is free.** On the laptop the model ran 58% on CPU because it did not
fit in 4 GB of VRAM. At 40 GB it is fully resident, and that alone accounts for most of
the 5–15× improvement.

## Which base image

Confirmed present on the cluster (`srv-svkmmastermum:5000/...`):

| Image | Size | Age | Use? |
|---|---|---|---|
| `pytorch_pbs:25.12-py3` | 19.9 GB | 7 mo | **start here** |
| `pytorch_pbs:25.10-py3` | 19.8 GB | 10 mo | equivalent alternative |
| `pytorch_pbs:23.06-py3` | 19.7 GB | 3 yr | fallback if 25.12 misbehaves |
| `tensorflow:25.03-tf2-py3` | 18 GB | 7 mo | no reason to prefer it |
| `nvidia/cuda:12.4.1-devel-ubuntu22.04` | 7.3 GB | 2 yr | lean, but no Jupyter — avoid |

We use the image for **Python and Jupyter, not for PyTorch.** Inference runs through
Ollama, which ships its own CUDA runtime and talks to the host driver directly. Nothing
in this project imports torch at run time: BERTScore is the only thing that would, it is
lazily imported (`text_overlap.default_bert_scorer`), and it is disabled for the battery.

Two consequences worth knowing:

- **The container's CUDA version barely matters.** The usual "new NGC image needs a new
  driver" problem applies to the container's torch, which we never call. If Ollama does
  see the GPU on 25.12, nothing else matters; if it does not, drop to `23.06-py3`, built
  against a CUDA that pairs with the driver in the manual (535.86.10 / CUDA 12.2).
- **Ignore `torch.cuda.is_available()`.** It can be False and the run will still be
  perfectly fine. The only check that counts is whether Ollama uses the GPU — the
  notebook tests exactly that.

The lean `nvidia/cuda` image is tempting at a third of the size, but the portal launches
Jupyter into the container and that image has neither Jupyter nor a guaranteed Python.
Not worth the debugging.

## First time: build your image

Altair does not take an image built on your laptop. You start from a stock image,
customise it in Jupyter, and save the result.

1. **Applications → Jupyter**, Container Image `pytorch_pbs:25.12-py3`, and the two
   corrected fields above.
2. Open the Jupyter URL, upload and run [`hpc/00_setup_gpu_image.ipynb`](../hpc/00_setup_gpu_image.ipynb).
   It diagnoses the environment (including whether this node has internet), installs
   Ollama, installs the Python dependencies, stages the models onto `/data`, and runs a
   smoke test.
3. **Custom Actions → Save Docker Container**, name it `repo-intel-gpu`, working
   directory `/data`.

Models and the repository live on `/data`, deliberately **not** inside the image: they
are ~25 GB with `qwen2.5-coder:32b` included, they change independently of the code, and every
job can see `/data` anyway.

## Every run after that

Build the pack off-cluster, where the network and Neo4j are:

```bash
cd backend
python -m scripts.build_context_pack $(cat ../18_repo_urls.txt) --out ../context_pack.json
```

Upload it to `/data/<you>/context_pack.json`, then submit **three jobs**, one per
generator, with Container Image `repo-intel-gpu`, job script
[`hpc/run_battery.sh`](../hpc/run_battery.sh), and the model as the script argument:

```
qwen2.5-coder:14b
codellama:13b-instruct
qwen2.5-coder:32b
```

The judge (`gemma2:9b`) is not in that list — it is not a generator, and the script
refuses to run if you make it one.

Three jobs on three slices finish in roughly the wall-clock of one. This is the single
biggest win over the laptop, which could only hold one model at a time. It also means
one model failing does not take the other two with it.

The script refuses to start if the judge equals the generator, if a model is not staged,
or if there is no GPU — each of which otherwise produces a plausible-looking wrong
result or wastes the slot.

Then score locally; the oracle needs no GPU:

```bash
python -m scripts.run_oracle && python -m scripts.stats_oracle
```

## Can it handle 50 repositories?

**The compute, comfortably. The annotations are the real question.**

Per generator, a battery is 3 generations and 6 judge calls per repository:

| Repositories | Calls per generator | All three generators |
|---|---:|---:|
| 18 | 162 | 486 |
| 50 | 450 | 1,350 |

Measured baseline on the RTX 3050 with CPU offload: **88 s mean per call**. Fully
resident on a 40 GB slice, expect **5–15× faster** — so roughly 30 minutes to 2 hours
per generator for 50 repositories, and all three run concurrently. Even the pessimistic
end fits in one sitting.

Nothing else strains: the pack is ~4 MB, ground-truth facts grow from ~1,184 to ~3,300,
and the oracle scores the lot in under a minute on a laptop.

**What does not scale is the ground truth.** Reference summaries are now dropped
entirely — they fed only BLEU/ROUGE/METEOR, measured at ~0.015 correlation with no
discriminative power, so per-repo prose writing is no longer part of the cost. What
remains is the structural annotation, which is the only independent check that the
parser is right. You have 18, and the audit in
`backend/annotations/README.md` found that **12 of those 18 endpoint lists are
byte-identical to the parser's own output** — one confirmed wrong, one confirmed right,
ten unknown. Going to 50 without fixing that process turns a known problem into a bigger
one, and "we evaluated 50 repositories" is worth nothing if a reviewer finds the ground
truth was seeded from the tool being evaluated.

Three watch-items if you do scale:

- **Verify the existing 18 first.** Fixing a broken process before multiplying it by
  three is cheaper than auditing 50 later.
- **Very large repositories break models.** `ack-nestjs-boilerplate` (601 modules) cost
  rows across four models at 8192 context. A 40 GB slice removes the memory pressure but
  not the context limit — expect a few failures and let the harness record them.
- **Two held-back repositories** are still unselected. Pick them before the run, not
  after, or they are no longer held back.

The honest recommendation: **30–35 well-verified repositories beats 50 half-verified
ones.** The compute is free now; annotator attention is not.

## Latency, and what not to pool

Timings from this cluster cannot be combined with the RTX 3050 figures in `REPORT.md`
§5.7.1 — different hardware, and MIG slices are shared, so a neighbouring job affects
your numbers. Token counts are hardware-independent and can be pooled.

If latency is going in the paper, re-run every arm here and report this machine, with
the MIG slice size stated.

## First-job checklist

- `nvidia-smi` output appears at the top of the log, showing a ~40 GB MIG slice.
- Both models listed as staged before generation starts.
- Smoke test is seconds, not a minute, and `nvidia-smi` shows GPU memory in use. If it
  is slow with idle GPU memory, the model is on CPU — fix that before a full battery.
- Wall clock from `time` roughly matches the summed per-row latencies. A large gap is
  scheduler and model-load overhead, not generation time.
