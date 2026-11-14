"""Application 전용 설정. dotenv와 기존 AppConfig는 읽지 않는다."""
from dataclasses import dataclass, field
import os
from pathlib import Path

ROLE_DEFAULTS = {
    'classifier': ('gpt-6-luna', 'low'),
    'analyst': ('gpt-6.1-sol', 'medium'),
    'specialist': ('gpt-6.1-sol', 'medium'),
    'critic': ('gpt-6-astra', 'high'),
    'verifier': ('gpt-6.1-sol', 'medium'),
    'patch': ('gpt-6.1-sol', 'medium'),
}


@dataclass(frozen=True)
class ApplicationSettings:
    database_url: str | None = field(default=None, repr=False)
    database_url_file: str | None = None
    isolated_database: bool = False
    testing: bool = False
    identity_file: str | None = None
    machine_identity_file: str | None = None
    profiles_file: str | None = None
    catalog_file: str | None = None
    codex_home: str | None = None
    model: str = 'gpt-6.1-sol'
    classifier_model: str | None = 'gpt-6-luna'
    analyst_model: str | None = None
    specialist_model: str | None = None
    critic_model: str | None = 'gpt-6-astra'
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

    def model_for(self, role):
        return getattr(self, role + '_model') or self.model

    def effort_for(self, role):
        return getattr(self, role + '_effort')

    @classmethod
    def from_env(cls):
        common = os.getenv('APPLICATION_MODEL')
        roles = {}
        for role, (model, effort) in ROLE_DEFAULTS.items():
            roles[role + '_model'] = os.getenv('APPLICATION_' + role.upper() + '_MODEL', common or model)
            roles[role + '_effort'] = os.getenv('APPLICATION_' + role.upper() + '_EFFORT', effort)
        return cls(database_url=os.getenv('APPLICATION_DATABASE_URL'),
                   database_url_file=os.getenv('APPLICATION_DATABASE_URL_FILE'),
                   isolated_database=os.getenv('APPLICATION_DATABASE_ISOLATED') == 'true',
                   identity_file=os.getenv('APPLICATION_IDENTITY_FILE'),
                   machine_identity_file=os.getenv('APPLICATION_MACHINE_IDENTITY_FILE'),
                   profiles_file=os.getenv('APPLICATION_PROFILES_FILE'),
                   catalog_file=os.getenv('APPLICATION_CATALOG_FILE'),
                   codex_home=os.getenv('APPLICATION_CODEX_HOME'),
                   model=common or 'gpt-6.1-sol',
                   worker_enabled=os.getenv('APPLICATION_WORKER_ENABLED') == 'true', **roles)

    def resolved_database_url(self):
        if self.database_url_file:
            try:
                return Path(self.database_url_file).read_text().strip()
            except OSError:
                return None
        return self.database_url
