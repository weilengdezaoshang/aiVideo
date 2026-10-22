"""Offline admission evaluation adapter. Executes real gates, never media/model providers."""
import json
import re
from pathlib import Path

from backend.agent.admission.policy import IntentContract, Proposal, review
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig


def execute_admission_case(raw: dict, out_dir: Path) -> dict:
    catalog = Catalog(CatalogConfig.model_validate(raw["catalog"]))
    result = review(Proposal.model_validate(raw["proposal"]), IntentContract.model_validate(raw["intent"]),
        catalog, catalog.config.admission)
    observation = {"planReview": result.model_dump(mode="json"), "events": [], "turns": [], "tracePaths": []}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "observation.json").write_text(json.dumps(observation, ensure_ascii=False), encoding="utf-8")
    return observation


def review_observations(result: dict) -> dict:
    return {"admissionVerdict": result.get("verdict"),
        **{f"admission{gate.title()}": result.get("checks", {}).get(gate) for gate in ("legal", "types", "intent")}}


def admission_check_id(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
