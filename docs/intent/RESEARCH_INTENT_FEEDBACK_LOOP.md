# Research: Intent Feedback Loop (Exemplar RAG → Clarify → Wash → Reindex)

Date: 2026-10-16 (CST)  
Scope: Public practices from conversational AI / creative tools / search assistants (Google Dialogflow, Microsoft Copilot Studio, Amazon REIC & AWS RAG blogs, Meta BlenderBot, OpenAI accuracy guides, Rasa CDD, ACL HITL/active-learning papers).  
**Not claimed:** Jimeng / Kling / ByteDance internal pipelines (no public sources).

---

## 1. Common production architecture (online vs offline)

Large teams almost always split the loop into **two clocks**:

### Online (latency-critical, per utterance)

1. **Feasibility / capability gates** — what the product *can* do given context (selected image? video feature on?).
2. **Intent / route** — classifier, trigger phrases, or **retrieval over exemplars (RAG-ICL)**; often hierarchical L1→L2.
3. **Confidence / ambiguity check** — score gap, top-1 vs top-2 conflict, capability mismatch.
4. **Clarify or proceed** — ask user; or disambiguation topic; or escalate.
5. **Slot fill / plan** — fill params *without* flipping the locked intent.
6. **Execute + log** — store utterance, predicted label, retrieval top-K, clarify outcome, task success.

Public anchors:

- **Amazon REIC**: index of `(query, intent)` → retrieve top-K → constrained intent probability; new intents via **index update**, not full retrain. ([REIC](https://arxiv.org/html/2506.00210v1))
- **Microsoft Copilot Studio**: generative orchestration + classic trigger phrases; **disambiguation / fallback** when multiple topics match or confidence is low. ([Language understanding](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/language-understanding))
- **AWS RAG clarification**: agent tools include retrieval + **AskHuman** when context is insufficient. ([AWS ML blog](https://aws.amazon.com/blogs/machine-learning/improve-llm-responses-in-rag-use-cases-by-interacting-with-the-user/))
- **OpenAI**: when intent is unclear, prefer clarify / handoff over irreversible wrong action; “prompt baking” logs → curated train set. ([Optimizing LLM accuracy](https://developers.openai.com/api/docs/guides/optimizing-llm-accuracy))

### Offline (batch / HITL / MLOps)

1. **Mine candidates** — low confidence, clarify sessions, model disagreement, fallback / no-match, random QA sample.
2. **Human verify / annotate** (prefer verify-over-annotate when model already guessed).
3. **Clean** — dedupe, desensitize, reject noise / misclicks, hard negatives → fallback.
4. **Promote** — into exemplar index and/or fine-tune / train phrases.
5. **Shadow eval → canary → full** — holdout set + regression (C01/C10 style).
6. **Reindex / retrain** on a schedule (hours–days), not per click.

Public anchors:

- **Dialogflow Training Tool**: review logged utterances → **Approve** queued adds to intents / fallback negatives → **then train**. ([Dialogflow ES Training](https://docs.cloud.google.com/dialogflow/es/docs/training))
- **Rasa Conversation Review**: filter by predicted intent / tags → mark reviewed → annotate / promote patterns. ([Rasa Studio](https://rasa.com/docs/studio/analyze/conversation-review/))
- **ACL “better to Verify”**: model proposes label → human **verifies**; only wrong ones go to full annotation (−97% annotate volume in their setup). ([Weber et al. 2021](https://aclanthology.org/2021.dash-1.2/))
- **ACL Active Learning NLU errors**: production classifier + 2 LLMs disagreement → cluster problematic utterances → prioritize annotation (100% perf @ 36% labels in paper). ([Pascual et al. 2023](https://aclanthology.org/2023.law-1.6/))
- **AWS ML Lens HITL**: route low-confidence / high-risk / random samples to humans; feed labels back into retrain. ([MLPERF06-BP01](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/mlperf06-bp01.html))
- **Meta BlenderBot 3**: deploy → collect thumbs / free-form feedback → **batch** continual learning (not online weight update); filter trolls / adversarial feedback. ([Meta blog](https://ai.meta.com/blog/blenderbot-3-a-175b-parameter-publicly-available-chatbot-that-improves-its-skills-and-safety-over-time/); [arXiv](https://arxiv.org/html/2208.03188v3))

```
[Online]  utter → gates → exemplar RAG → strong? → slots → run
                              └─ weak? → clarify (click|text) → lock L2 → slots → run
                              └─ always log: utter, topK, scores, clarify, outcome

[Offline] log → priority queue (AL / clarify / success) → HITL wash
       → approve exemplars / hard negatives → reindex (+ optional FT)
       → shadow eval → canary → promote
```

---

## 2. How clarifications / user corrections become labels

| Signal | Typical treatment | Promote to exemplars? |
|--------|-------------------|------------------------|
| Clicked clarify option | Strong **candidate** label for *original* utterance | After light HITL / auto-rules |
| Typed clarify reply that parses to one L2 | Candidate label | Same |
| Thumbs-down / “wrong intent” | Error flag for AL queue | Only after re-label |
| Task completed + not undone soon | Weak success prior | Needs review if used as label |
| Free-form correction | Needs NLU parse or human | Never auto-ingest raw |
| Adversarial / spam feedback | Filter (user-level trust) | Never |

Industry patterns:

- **Dialogflow**: human explicitly queues “add this User Says to intent X” or to **fallback as negative**; Approve batch; then train. Not auto from every chat turn.
- **OpenAI**: pilot logs pruned into representative, format-consistent train sets; quality ≫ quantity; ask for more info when unsure.
- **BlenderBot**: binary + modular feedback useful; **modular / supervised module labels > raw binary**; distrust noisy users; batch retrain.
- **Verify workflow**: treat model (or user click) as **hypothesis**; human confirms → cheap label.

**For canvas Agent (edit-image vs make-video):**  
Clarify answer locks L2 for *this* session. Offline: `(original_utterance → chosen_L2)` enters **candidate** table with provenance (`clarify_click` | `clarify_text` | `task_success`). Promotion only after wash.

---

## 3. Cleaning / quality gates before exemplars enter the index

Common gates (compose several):

1. **Length / language / empty / emoji-only** reject.
2. **Dedup / near-dup** (embedding cosine) — keep diverse centroids.
3. **Label consistency** — same phrase shouldn’t map to conflicting L2s; resolve via majority or human.
4. **Feasibility consistency** — “改成水彩” cannot be video-only exemplar if product requires image context.
5. **Hard negatives** — “不要视频 / 只要静图” → fallback or explicit **not_video** exemplars (Dialogflow fallback pattern).
6. **PII / URL / internal id scrub**.
7. **Source trust** — prefer HITL-verified > clarify-click > success-heuristic; drop troll / misclick clusters.
8. **Holdout leakage check** — never promote eval-set utterances into index without tagging.
9. **Shadow retrieval** — new exemplars must improve or not hurt fixed regression suite before canary.

REIC explicitly cleaned train so gold intent was in retrieved list; inference still uses noisy retrieval — i.e. **index hygiene matters**.

---

## 4. Named patterns (cheat sheet)

| Pattern | Role |
|---------|------|
| **Exemplar / RAG intent (REIC)** | Scale intents by retrieving similar labeled queries; update index instead of always retraining |
| **Confidence thresholds** | High → auto; mid → clarify; low → fallback / human |
| **Ambiguity / top-2 conflict** | When edit≈video scores, force clarify (don’t guess) |
| **Disambiguation topic** | Copilot Studio classic pattern |
| **AskHuman / clarify tool** | Agent decides to ask when retrieval/context insufficient |
| **Active learning** | Prioritize disagreement / uncertainty / clusters for labels |
| **HITL verify queue** | Confirm model hypothesis; full annotate only on reject |
| **Hard negatives / fallback phrases** | Teach “what is *not* this intent” |
| **Shadow eval** | New index/model vs frozen gold set offline |
| **Canary** | Small % traffic on new exemplars/model |
| **Prompt baking** | Log pilot I/O → prune → FT or few-shot store |
| **Continual learning (batch)** | Collect online, train offline in batches; filter adversarial feedback |
| **User-level trust / troll filter** | Don’t let one bad actor poison exemplars |

---

## 5. What is typically **NOT** done

- **Auto-ingest every clarify click / every successful task into the live index** with no review.
- **Online weight updates** per conversation (even BB3 trains in successive batches).
- **Trust raw free-form chat as gold labels** without parse + review.
- **Promote ambiguous utterances** that still look like both edit and video.
- **Skip regression** after reindex (silent intent flips are common failure mode).
- **Rely only on automated metrics** without human sampling (AWS anti-pattern).
- Claim product-specific internal Jimeng/Kling loops without public evidence — **don’t**.

---

## 6. Citations (fetched or search-confirmed)

| # | Title | URL | Status |
|---|-------|-----|--------|
| 1 | REIC: RAG-Enhanced Intent Classification at Scale (Amazon) | https://arxiv.org/html/2506.00210v1 | Fetched |
| 2 | Design effective language understanding — Microsoft Copilot Studio | https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/language-understanding | Fetched |
| 3 | Improve LLM responses in RAG by interacting with the user — AWS | https://aws.amazon.com/blogs/machine-learning/improve-llm-responses-in-rag-use-cases-by-interacting-with-the-user/ | Fetched |
| 4 | Training (Dialogflow ES Training Tool) — Google Cloud | https://docs.cloud.google.com/dialogflow/es/docs/training | Fetched |
| 5 | Optimizing LLM Accuracy — OpenAI | https://developers.openai.com/api/docs/guides/optimizing-llm-accuracy | Fetched |
| 6 | It is better to Verify… (Weber et al., ACL DaSH 2021) | https://aclanthology.org/2021.dash-1.2/ | Fetched |
| 7 | An Active Learning Pipeline for NLU Error Detection (Pascual et al., LAW 2023) | https://aclanthology.org/2023.law-1.6/ | Fetched |
| 8 | MLPERF06-BP01 Include human-in-the-loop monitoring — AWS | https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/mlperf06-bp01.html | Fetched |
| 9 | BlenderBot 3 Meta AI blog | https://ai.meta.com/blog/blenderbot-3-a-175b-parameter-publicly-available-chatbot-that-improves-its-skills-and-safety-over-time/ | Fetched |
| 10 | BlenderBot 3 technical report | https://arxiv.org/html/2208.03188v3 | Fetched |
| 11 | Rasa Studio — Conversation Review | https://rasa.com/docs/studio/analyze/conversation-review/ | Fetched |

(Optional related, search-hit / not fully re-fetched body): RAC clarification paper https://arxiv.org/html/2601.11722 — treat as unchecked beyond search snippet.)

---

## 7. Practical recommendation — small team, canvas Agent (image edit vs video)

**Goal:** Exemplar RAG + clarify + wash + reindex, without a 20-person labeling org.

### Online (ship first)

1. **possibleL2** from context (has image? video capability?).
2. Embed utterance → retrieve top-K exemplars **restricted to possibleL2**.
3. Thresholds (tune on fixed eval):  
   - `score ≥ T_strong` and `gap ≥ G` → lock L2.  
   - else if both edit & video in top → **clarify**.  
   - else → clarify or safe default (prefer ask over wrong video).
4. Regex/gates only for **hard conflict** (negation of video, entry-point conflicts).
5. After lock: slot fill; **never** change L2 in slots.
6. Log always: `utterance, topK[], scores, decision, clarify_choice?, task_id, success`.

### Offline (weekly is enough at first)

1. Candidate sources (priority order):  
   - clarify sessions (click/text → L2)  
   - AL: low score / small gap / embedding disagreement  
   - successes without undo (lower priority)
2. **HITL queue UI** (even a spreadsheet): verify label; mark hard-negative; reject.
3. Wash gates §3 → write `exemplars.jsonl` + rebuild FAISS/pgvector.
4. **Shadow**: run frozen C01/C10 + watercolor-edit / “动起来” clarify / negation cases.
5. **Canary** 5–10% traffic → watch clarify rate & wrong-video rate → full roll.

### What small teams should copy from big teams

| Copy | Skip (for now) |
|------|----------------|
| Exemplar index + reindex | Full LLM disagreement AL farm |
| Clarify on ambiguity | Online continual FT |
| Verify-HITL before promote | Auto-ingest all clicks |
| Hard negatives / fallback | Multi-vertical taxonomy |
| Shadow + canary | Heavy Director / RLHF |

### Minimal schema

```text
candidates(id, utterance, proposed_l2, source, confidence, created_at)
exemplars(id, utterance, l2, embedding, approved_by, approved_at, version)
hard_negatives(utterance, not_l2)
```

### One-sentence closed loop (user-facing)

**拿不准就问 → 定死意图并记候选 → 人工清洗后进例句库 → 重建向量 → 下次更准。**

---

## 8. Mapping to prior v1.2 intent design

Aligns with existing TECH_INTENT_RAG_IMPL / layered intent:

- Online RAG = REIC-style candidate retrieval.
- Clarify = Copilot disambiguation + AWS AskHuman.
- Candidate log = Dialogflow history / Rasa review queue.
- Wash + approve = Dialogflow Approve + Weber verify.
- Reindex = REIC index construction; optional later FT = OpenAI prompt baking.

