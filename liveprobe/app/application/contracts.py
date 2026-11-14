"""버전 1 공개 API 입력 및 runner 결과의 검증 가능한 JSON 계약."""
from typing import Any, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from .safety import text_sha

KINDS = ('collect', 'source', 'test_build', 'deploy', 'verify', 'reconcile')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Signal(Strict):
    source: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    sample: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def matching_hash(self):
        if text_sha(self.sample) != self.sha256:
            raise ValueError('signal_hash_invalid')
        return self


class CreateIncident(Strict):
    environment: Literal['dev', 'prd']
    service: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=4000)
    reproduction: str | None = None
    signal: Signal | None = None


class Claim(Strict):
    worker_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9._-]+$')
    capabilities: list[Literal['collect', 'source', 'test_build', 'deploy', 'verify', 'reconcile']] = Field(min_length=1, max_length=6)


class Lease(Strict):
    lease_token: str = Field(min_length=16, max_length=128)
    fencing_token: int = Field(ge=1)


class Report(Lease):
    result: dict[str, Any]


class Target(Strict):
    environment: Literal['dev', 'stg', 'prd']
    service: str
    system: str
    uid: str = Field(min_length=1, max_length=128)
    resource_version: str = Field(min_length=1, max_length=128)
    container: str = Field(min_length=1, max_length=128)
    image: str = Field(pattern=r'^\S+@sha256:[a-f0-9]{64}$')
    template_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class UserTest(Strict):
    release_id: str
    artifact_sha256: str
    plan_sha256: str
    image: str
    passed: bool = Field(strict=True)
    notes_ko: str = Field(min_length=1, max_length=4000)


class Approval(Strict):
    user_test_id: str
    release_id: str
    artifact_sha256: str
    plan_sha256: str
    image: str


class ErrorResponse(Strict):
    code: str
    message_ko: str


class IncidentResponse(Strict):
    id: str
    state: str
    created_at: float
    environment: str
    service: str
    system: str
    reason: str
    handoff_classification: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = []
    error: dict[str, Any] | None = None
    data: dict[str, Any] = {}


class JobResponse(Strict):
    id: str
    kind: str
    incident_id: str
    payload: dict[str, Any]
    lease_token: str
    fencing_token: int
    idempotency_key: str
    lease_expires_at: float


class ClaimResponse(Strict):
    job: JobResponse | None


class ReadyChecks(Strict):
    database: bool
    human_identity: bool
    machine_identity: bool
    profiles: bool
    model: bool
    runner: bool


class ModelStatus(Strict):
    state: Literal['unconfigured', 'configured_unverified', 'recent_success', 'recent_failure', 'unknown']
    configured: bool
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_failure_code: str | None = None


class WaitingPeople(Strict):
    awaiting_user_test: int
    awaiting_prd_approval: int


class ReadyResponse(Strict):
    ready: bool
    mode: Literal['isolated_test', 'runtime']
    checks: ReadyChecks
    code: str | None = None
    message_ko: str | None = None
    model_status: ModelStatus | None = None
    waiting_people: WaitingPeople | None = None


class CatalogResponse(Strict):
    historical_environments: list[str] = []
    systems: list[str]
    services: list[dict[str, Any]]
    environments: list[str]
    catalog_sha256: str
    profiles_configured: bool


class EventsResponse(Strict):
    events: list[dict[str, Any]]


class ReleaseResponse(Strict):
    incident_id: str
    state: str
    release: dict[str, Any] | None
    # 실패하여 release가 생성되지 않은 시험도 정제 원장으로 조회한다.
    test_build: dict[str, Any] | None = None


class Receipt(Strict):
    job_id: str
    incident_id: str
    result_sha256: str
    state: str
    accepted_at: float
