from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Document(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str


class OpportunityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    title: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=10, max_length=10000)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    issue_number: int = Field(gt=0)
    reward: float = Field(ge=0, le=1000000)
    currency: Literal["USD"] = "USD"
    language: str = Field(default="Python", max_length=40)
    estimated_hours: float = Field(default=2, gt=0, le=10000)
    probability_of_success: float = Field(default=0.5, ge=0, le=1)
    probability_of_payment: float = Field(default=0.25, ge=0, le=1)
    estimated_compute_cost: float = Field(default=0, ge=0, le=100000)
    estimated_tool_cost: float = Field(default=0, ge=0, le=100000)
    estimated_difficulty: float = Field(default=5, ge=1, le=10)
    competition: float = Field(default=0.5, ge=0, le=1)
    repository_health: float = Field(default=0.5, ge=0, le=1)
    agent_compatibility: float = Field(default=0.5, ge=0, le=1)
    risk_score: float = Field(default=30, ge=0, le=100)
    payment_terms: str = Field(default="", max_length=2000)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["OBSERVE", "ASSISTED"] = "ASSISTED"
    weights: dict[str, float] = Field(default_factory=lambda: {
        "reward": 25, "acceptance": 25, "difficulty": 15, "competition": 10,
        "repository_health": 10, "payment_reliability": 10, "compatibility": 5,
    })
    min_reward: float = Field(default=10, ge=0, le=1000000)
    max_estimated_hours: float = Field(default=8, gt=0, le=1000)
    min_success_probability: float = Field(default=0.5, ge=0, le=1)
    min_expected_profit: float = Field(default=5, ge=0, le=100000)
    max_compute_cost: float = Field(default=0, ge=0, le=10000)
    max_risk_score: float = Field(default=25, ge=0, le=100)
    max_jobs_per_day: int = Field(default=5, ge=1, le=100)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=4)
    max_model_cost_per_day: float = Field(default=0, ge=0, le=10000)
    max_model_cost_per_job: float = Field(default=0, ge=0, le=1000)
    max_submissions_per_day: int = Field(default=3, ge=0, le=30)
    max_fix_iterations: int = Field(default=3, ge=0, le=3)
    max_repository_size: int = Field(default=50000, ge=1, le=100000)
    max_execution_time: int = Field(default=120, ge=10, le=600)
    authorized_budget: float = Field(default=0, ge=0, le=100000)
    reserve_percent: int = Field(default=70, ge=0, le=100)
    reinvestment_percent: int = Field(default=20, ge=0, le=100)
    experimentation_percent: int = Field(default=10, ge=0, le=100)
    primary_model: str = Field(default="NOT CONFIGURED", max_length=100)
    coding_model: str = Field(default="NOT CONFIGURED", max_length=100)
    review_model: str = Field(default="NOT CONFIGURED", max_length=100)
    cheap_model: str = Field(default="NOT CONFIGURED", max_length=100)
    research_model: str = Field(default="NOT CONFIGURED", max_length=100)

    @model_validator(mode="after")
    def validate_allocations(self):
        expected = {"reward", "acceptance", "difficulty", "competition", "repository_health", "payment_reliability", "compatibility"}
        if set(self.weights) != expected or any(v < 0 or v > 100 for v in self.weights.values()) or abs(sum(self.weights.values()) - 100) > 0.01:
            raise ValueError("Scoring weights must contain all seven factors and total 100%")
        if self.reserve_percent + self.reinvestment_percent + self.experimentation_percent != 100:
            raise ValueError("Financial allocations must total 100%")
        return self


class OpportunityAction(BaseModel):
    action: Literal["WATCH", "IGNORE", "MANUAL_REVIEW", "EVALUATE"]


class ApprovalAction(BaseModel):
    action: Literal["APPROVE", "REJECT", "REQUEST_CHANGES", "ABORT"]
    reason: str = Field(default="", max_length=1000)


class ControlAction(BaseModel):
    action: Literal["GLOBAL_STOP", "STOP_AGENTS", "STOP_SPENDING", "DISABLE_SUBMISSIONS", "DISABLE_NEW_JOBS", "REVOKE_INTEGRATIONS", "RESUME"]


class PaymentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    job_id: str
    amount: float = Field(ge=0.01, le=1000000, multiple_of=0.01)
    reference: str = Field(min_length=5, max_length=200)
    method: str = Field(default="Manual evidence", max_length=100)
    verified: bool = False
    attestation: bool = False


class ExpenseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    amount: float = Field(ge=0.01, le=100000, multiple_of=0.01)
    category: Literal["compute", "api", "tool", "other"]
    description: str = Field(min_length=5, max_length=200)
    reference: str = Field(min_length=5, max_length=200)
    simulated: bool = True


class GithubImport(BaseModel):
    url: str = Field(pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[0-9]+/?$")


class RepoPolicy(BaseModel):
    allowed: bool
    push_authorized: bool = False


class PaymentVerification(BaseModel):
    attestation: bool


class SourcePolicy(BaseModel):
    allowed: bool