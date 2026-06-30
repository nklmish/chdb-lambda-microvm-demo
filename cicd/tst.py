"""cicd/tst.py — Run evaluation against the TST-deployed agent with autoevals Factuality.

Flow:
  1. Load agent_arn from cicd/hp_config.json (written by cicd/deploy_agent.sh TST)
  2. Load golden dataset from evaluation/dataset.json
  3. Resolve OpenAI API key: SSM /langfuse/OPENAI_API_KEY with OPENAI_API_KEY env fallback
  4. Initialize Langfuse client (for trace-context + score write-back)
  5. For each dataset item:
     - Open Langfuse observation → extract trace_id + parent_span_id
     - invoke_agent(agent_arn, prompt, trace_id, parent_span_id) → response
     - autoevals.Factuality()(output=response, expected=facts, input=question) → score
     - langfuse.create_score(trace_id=..., name="factuality", value=..., comment=...)
  6. Aggregate mean score → write cicd/../factuality_results.json (consumed by check_factuality.py)

-compliant: invoked as `python3 cicd/tst.py` (CI) or `python3 /abs/cicd/tst.py` (local).
Langfuse v4 key is `parent_span_id` (NOT v2/v3 `parent_observation_id`).
Langfuse v4 score write is `create_score(...)` (NOT v2/v3 bare `score(...)`) — see D-D15-TST-LANGFUSE-V4-INIT-AND-SCORE.
Langfuse client init requires LANGFUSE_* env vars — resolved via utils.langfuse_utils.get_langfuse_client()
which pulls SSM /langfuse/* params and exports them before calling langfuse.get_client().
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure repo root on sys.path so evaluation/ and utils/ import cleanly
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation.invoke import invoke_agent
from utils.aws import get_ssm_parameter


def _resolve_openai_key() -> str:
    """SSM → env-var dual-path per Phase A decision (e)."""
    # SSM takes precedence if present
    try:
        key = get_ssm_parameter("/langfuse/OPENAI_API_KEY")
        if key:
            return key
    except Exception:
        pass
    # Env var fallback (CI injects via GH secrets; local dev sets manually)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print(
            "FAIL: OpenAI API key not found.\n"
            "  Expected EITHER SSM parameter /langfuse/OPENAI_API_KEY "
            "OR OPENAI_API_KEY env var.\n"
            "  In CI: set repo secret OPENAI_API_KEY.\n"
            "  Locally: export OPENAI_API_KEY=... or populate SSM.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _load_hp_config() -> dict:
    hp_path = REPO_ROOT / "cicd" / "hp_config.json"
    config = json.loads(hp_path.read_text())
    tst = config.get("tst")
    if not tst or not tst.get("agent_arn"):
        print(
            f"FAIL: cicd/hp_config.json has no TST entry. "
            f"Run `bash cicd/deploy_agent.sh TST` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return tst


def _load_dataset() -> list[dict]:
    dataset_path = REPO_ROOT / "evaluation" / "dataset.json"
    data = json.loads(dataset_path.read_text())
    return data["items"]


def main() -> int:
    # Resolve OpenAI key FIRST and export — autoevals reads it at import time.
    os.environ["OPENAI_API_KEY"] = _resolve_openai_key()

    from autoevals import Factuality
    from utils.langfuse_utils import get_langfuse_client

    tst = _load_hp_config()
    agent_arn = tst["agent_arn"]
    items = _load_dataset()

    # Langfuse v4 client: get_langfuse_client() resolves LANGFUSE_* from SSM and
    # exports them as env vars before calling langfuse.get_client() — v4 init
    # requires those env vars to be set at client-construction time or it falls
    # back to disabled-no-key mode.
    lf = get_langfuse_client()
    factuality = Factuality()  # default judge: gpt-4o-mini (autoevals 0.0.20+)

    per_item_scores: list[dict] = []
    aggregate = 0.0
    n = 0

    for idx, item in enumerate(items):
        question = item["input"]["question"]
        expected_facts = "\n".join(
            f"- {fact}" for fact in item["expected_output"]["response_facts"]
        )

        # Open Langfuse observation for this eval item → extract v4 trace context
        with lf.start_as_current_observation(
            name=f"eval-item-{idx}",
            as_type="span",
        ):
            trace_id = lf.get_current_trace_id()
            parent_span_id = lf.get_current_observation_id()  # v4: returns OTel span_id

            result = invoke_agent(
                agent_arn=agent_arn,
                prompt=question,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
            )

            if "error" in result:
                print(f"Item {idx}: invoke error: {result['error']}", file=sys.stderr)
                per_item_scores.append({
                    "index": idx,
                    "question": question,
                    "score": 0.0,
                    "error": result["error"],
                })
                continue

            output_text = result.get("response", "")

            # Autoevals Factuality: scores output against expected facts given input question
            score_result = factuality(
                output=output_text,
                expected=expected_facts,
                input=question,
            )
            score_value = float(score_result.score) if score_result.score is not None else 0.0
            rationale = getattr(score_result, "metadata", None) or {}
            comment = json.dumps(rationale) if rationale else ""

            # Write score back to Langfuse on this trace (v4 API: create_score, NOT score).
            lf.create_score(
                trace_id=trace_id,
                name="factuality",
                value=score_value,
                comment=comment[:1000] if comment else None,
            )

            per_item_scores.append({
                "index": idx,
                "question": question,
                "score": score_value,
                "response": output_text[:500],
            })
            aggregate += score_value
            n += 1

    mean_score = (aggregate / n) if n > 0 else 0.0

    results = {
        "average_factuality_score": mean_score,
        "total_items": len(items),
        "scored_items": n,
        "per_item": per_item_scores,
    }

    out_path = REPO_ROOT / "factuality_results.json"
    out_path.write_text(json.dumps(results, indent=2))

    print(f"Wrote {out_path}: {n}/{len(items)} items scored, mean={mean_score:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
