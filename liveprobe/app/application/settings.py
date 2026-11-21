"""Application 전용 설정. dotenv와 기존 AppConfig는 읽지 않는다."""
from dataclasses import dataclass, field
import os
from pathlib import Path

# 2026-11-18 모델 계층: 낮음 gpt-6-luna, 중간 gpt-6.2-sol, 높음 gpt-6.2-astra.
MODEL_LOW, MODEL_MID, MODEL_HIGH = 'gpt-6-luna', 'gpt-6.2-sol', 'gpt-6.2-astra'
# 이전 계층 ID가 든 배포 환경변수는 새 실행에서만 현재 ID로 대응한다. 기록된 역할 결과의 모델명은 바꾸지 않는다.
LEGACY_MODEL_IDS = {'gpt-6.1-sol': MODEL_MID, 'gpt-6-astra': MODEL_HIGH}

ROLE_DEFAULTS = {
    'classifier': (MODEL_LOW, 'low'),
    'analyst': (MODEL_MID, 'medium'),
    'specialist': (MODEL_MID, 'medium'),
    'critic': (MODEL_HIGH, 'high'),
    'verifier': (MODEL_MID, 'medium'),
    'patch': (MODEL_MID, 'medium'),
}


def current_model_id(model):
    return LEGACY_MODEL_IDS.get(model, model) if isinstance(model, str) else model


@dataclass(frozen=True)
class ApplicationSettings:
    database_url: str | None = field(default=None, repr=False)
    database_url_file: str | None = None
    isolated_database: bool = False
    testing: bool = False
    identity_file: str | None = None
    machine_identity_file: str | None = None
    local_read_key_file: str | None = None
    local_operator_key_file: str | None = None
    local_gateway_url: str | None = None
    profiles_file: str | None = None
    catalog_file: str | None = None
    codex_home: str | None = None
    model: str = MODEL_MID
    classifier_model: str | None = MODEL_LOW
    analyst_model: str | None = None
    specialist_model: str | None = None
    critic_model: str | None = MODEL_HIGH
    verifier_model: str | None = None
    patch_model: str | None = None
    classifier_effort: str = 'low'
    analyst_effort: str = 'medium'
    specialist_effort: str = 'medium'
    critic_effort: str = 'high'
    verifier_effort: str = 'medium'
    patch_effort: str = 'medium'
    lease_seconds: int = 120
    approval_seconds: int = 1800
    evidence_max_age_seconds: int = 3600
    database_init_retry_seconds: int = 30
    worker_enabled: bool = False
    external_model_worker: bool = False
    # Opt-in runtime job/identity separation; linked incidents require it independently.
    unified_mode: bool = False
    # New releases use dev-qa-prd/v3 (real dev UI QA receipt + prd risk picture) instead of a human dev test.
    qa_required: bool = False
    # Optional fixed local origins per service (e.g. a port-forward). The registered *.dev.localhost host is always allowed.
    qa_ui_origins: dict | None = None
    repositories_file: str | None = None
    github_read_token_file: str | None = None

    def model_for(self, role):
        return current_model_id(getattr(self, role + '_model') or self.model)

    def effort_for(self, role):
        return getattr(self, role + '_effort')

    @classmethod
    def from_env(cls):
        common = current_model_id(os.getenv('APPLICATION_MODEL'))
        roles = {}
        for role, (model, effort) in ROLE_DEFAULTS.items():
            roles[role + '_model'] = current_model_id(os.getenv('APPLICATION_' + role.upper() + '_MODEL', common or model))
            roles[role + '_effort'] = os.getenv('APPLICATION_' + role.upper() + '_EFFORT', effort)
        return cls(database_url=os.getenv('APPLICATION_DATABASE_URL'),
                   database_url_file=os.getenv('APPLICATION_DATABASE_URL_FILE'),
                   isolated_database=os.getenv('APPLICATION_DATABASE_ISOLATED') == 'true',
                   identity_file=os.getenv('APPLICATION_IDENTITY_FILE'),
                   local_read_key_file=os.getenv('APPLICATION_LOCAL_READ_KEY_FILE') or os.getenv('LIVEPROBE_LOCAL_READ_KEY_FILE'),
                   local_operator_key_file=os.getenv('LIVEPROBE_LOCAL_OPERATOR_KEY_FILE'),
                   local_gateway_url=os.getenv('APPLICATION_LOCAL_GATEWAY_URL'),
                   machine_identity_file=os.getenv('APPLICATION_MACHINE_IDENTITY_FILE'),
                   profiles_file=os.getenv('APPLICATION_PROFILES_FILE'),
                   catalog_file=os.getenv('APPLICATION_CATALOG_FILE'),
                   codex_home=os.getenv('APPLICATION_CODEX_HOME'),
                   model=common or MODEL_MID,
                   worker_enabled=os.getenv('APPLICATION_WORKER_ENABLED') == 'true',
                   external_model_worker=os.getenv('APPLICATION_EXTERNAL_MODEL_WORKER') == 'true',
                   qa_required=os.getenv('APPLICATION_QA_REQUIRED') == 'true',
                   repositories_file=os.getenv('APPLICATION_REPOSITORIES_FILE'),
                   github_read_token_file=os.getenv('APPLICATION_GITHUB_READ_TOKEN_FILE'),
                   unified_mode=(os.getenv('APPLICATION_UNIFIED_MODE') == 'true' or
                                 os.getenv('LIVEPROBE_UNIFIED_ENABLED') == 'true'), **roles)

    def resolved_database_url(self):
        if self.database_url_file:
            try:
                return Path(self.database_url_file).read_text().strip()
            except OSError:
                return None
        return self.database_url
