"""환경·서비스 바인딩과 mounted 파일 입력. 비밀은 repr에 포함하지 않는다."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .store import SYSTEMS

SERVICES = {'business': 'business-api', 'trade': 'trade-member', 'delivery': 'delivery-member'}
DISPLAY_NAMES = {'business': '삼성 비즈니스 시스템', 'trade': '삼성 트레이드 시스템', 'delivery': '삼성 딜리버리 시스템'}
DEFAULT_PATHS = {
    'DATABASE_DSN_FILE': '/var/run/samsung/db/dsn',
    'KAFKA_CONFIG_FILE': '/var/run/samsung/kafka/client.json',
    'KAFKA_CA_FILE': '/var/run/samsung/kafka/ca.crt',
    'API_CREDENTIALS_FILE': '/var/run/samsung/api/credentials.json',
    'BUSINESS_TOKEN_FILE': '/var/run/samsung/api/reader-token',
    'BUSINESS_CA_FILE': '/var/run/samsung/tls/ca.crt',
    'TLS_CERT_FILE': '/var/run/samsung/tls/tls.crt',
    'TLS_KEY_FILE': '/var/run/samsung/tls/tls.key',
}


@dataclass
class Settings:
    mode: str = 'business'
    environment: str = 'dev'
    version: str = 'local'
    read_only: bool = True
    principals: list = field(default_factory=list, repr=False)
    dsn: str = field(default='', repr=False)
    kafka: dict = field(default_factory=dict, repr=False)
    paths: dict = field(default_factory=lambda: dict(DEFAULT_PATHS))
    business_url: str = ''
    http_port: int = 8080
    tls_port: int = 8443

    def __post_init__(self):
        if self.mode not in SYSTEMS or self.environment not in ('dev', 'stg', 'prd'):
            raise ValueError('허용되지 않은 서비스 모드 또는 환경입니다')
        seen = set()
        for principal in self.principals:
            role = principal.get('role')
            expected_target = {'business-admin': None, 'trade-reader': 'samsung-trade', 'delivery-reader': 'samsung-delivery'}.get(role, 'invalid')
            token = principal.get('token')
            if not isinstance(token, str) or not token or token in seen or expected_target == 'invalid' or principal.get('target_system') != expected_target or principal.get('environment') not in ('dev', 'stg', 'prd'):
                raise ValueError('주체 설정 형식이 올바르지 않습니다')
            seen.add(token)

    @property
    def system(self):
        return SYSTEMS[self.mode]

    @property
    def service(self):
        return SERVICES[self.mode]

    @property
    def topic(self):
        return f'{self.environment}.business.{self.mode}.master.v1'

    @property
    def group(self):
        return f'{self.environment}.{self.system}.master.v1'

    @classmethod
    def from_env(cls):
        paths = {key: os.environ.get(key, value) for key, value in DEFAULT_PATHS.items()}
        try:
            credentials = json.loads(Path(paths['API_CREDENTIALS_FILE']).read_text())
            if set(credentials) != {'principals'} or not isinstance(credentials['principals'], list):
                raise ValueError('주체 설정 오류')
            mode = os.environ.get('DOMAIN_MODE', 'business')
            environment = os.environ.get('APP_ENV', 'dev')
            read_only = os.environ.get('READ_ONLY_MODE', '1')
            if read_only not in ('0', '1'):
                raise ValueError('읽기 전용 설정 오류')
            cfg = cls(mode=mode, environment=environment, version=os.environ.get('APP_VERSION', 'local'),
                read_only=read_only == '1', principals=credentials['principals'], paths=paths,
                dsn=Path(paths['DATABASE_DSN_FILE']).read_text().strip(),
                kafka=json.loads(Path(paths['KAFKA_CONFIG_FILE']).read_text()),
                business_url=os.environ.get('BUSINESS_API_URL', f'https://business-api.{environment}.svc:8443'),
                http_port=int(os.environ.get('HTTP_PORT', '8080')), tls_port=int(os.environ.get('TLS_PORT', '8443')))
            if os.environ.get('SYSTEM_NAME', cfg.system) != cfg.system or os.environ.get('SERVICE_NAME', cfg.service) != cfg.service:
                raise ValueError('서비스 소유권 오류')
            if cfg.http_port == cfg.tls_port or not all(0 < p < 65536 for p in (cfg.http_port, cfg.tls_port)):
                raise ValueError('포트 설정 오류')
            if cfg.kafka.get('security.protocol') != 'SASL_SSL' or cfg.kafka.get('sasl.mechanism', cfg.kafka.get('sasl.mechanisms')) != 'PLAIN':
                raise ValueError('보호된 Kafka 설정 필수')
            for key in ('bootstrap.servers', 'sasl.username', 'sasl.password'):
                if not cfg.kafka.get(key):
                    raise ValueError('Kafka 설정 누락')
            cfg.kafka['ssl.ca.location'] = paths['KAFKA_CA_FILE']
            cfg.kafka['enable.ssl.certificate.verification'] = True
            cfg.kafka['ssl.endpoint.identification.algorithm'] = 'https'
            if urlparse(cfg.business_url).scheme != 'https' or urlparse(cfg.business_url).username or urlparse(cfg.business_url).password:
                raise ValueError('TLS 원장 주소 필수')
            for key in ('KAFKA_CA_FILE', 'TLS_CERT_FILE', 'TLS_KEY_FILE') + (() if mode == 'business' else ('BUSINESS_TOKEN_FILE', 'BUSINESS_CA_FILE')):
                if not Path(paths[key]).is_file():
                    raise ValueError('마운트 파일 누락')
            return cfg
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError('런타임 설정 또는 보호된 마운트 파일을 확인해 주세요') from None
