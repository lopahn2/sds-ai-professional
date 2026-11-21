"""독립 SQLAlchemy metadata/연결. import는 무접속이며 명시 초기화·lifespan만 독립 스키마를 만든다."""
from contextlib import contextmanager
import logging
import time
from sqlalchemy import Text, JSON, Float, Integer, String, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


class ApplicationBase(DeclarativeBase):
    pass


class Incident(ApplicationBase):
    __tablename__ = 'application_incidents'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    document: Mapped[dict] = mapped_column(JSON)


class Store:
    base = ApplicationBase

    def __init__(self, settings):
        self.settings = settings
        self.engine = None
        self.factory = None
        self.bootstrap_error = None
        self.retry_at = None
        url = settings.resolved_database_url()
        if not url:
            return
        try:
            parsed = make_url(url)
            if parsed.get_backend_name() == 'sqlite':
                if not settings.testing:
                    return
                options = {'connect_args': {'check_same_thread': False, 'timeout': 15}}
                if parsed.database in (None, '', ':memory:'):
                    options['poolclass'] = StaticPool
            elif parsed.get_backend_name() == 'postgresql' and settings.isolated_database:
                # 기존 운영 DB의 우발적 재사용을 이름에서도 거부한다.
                if not (parsed.database or '').startswith('liveprobe_application'):
                    return
                parsed = parsed.set(drivername='postgresql+psycopg')
                options = {'pool_pre_ping': True, 'connect_args': {'connect_timeout': 3}}
            else:
                return
            self.engine = create_engine(parsed, hide_parameters=True, **options)
            self.factory = sessionmaker(self.engine, expire_on_commit=False)
        except Exception:
            self.engine = None

    def initialize(self):
        if self.engine is None:
            raise RuntimeError('독립 저장소가 구성되지 않았다')
        # ApplicationBase의 application_* 표만 만든다. 기존 LiveProbe Base·alembic은 import하지 않는다.
        self.base.metadata.create_all(self.engine)
        with self.factory() as session:
            if session.get(Mutex, 1) is None:
                session.add(Mutex(id=1))
                session.commit()

    def bootstrap(self):
        """lifespan·명시 명령용 멱등 초기화. 실패 원문(연결 문자열 포함 가능)은 보존·출력하지 않는다."""
        self.retry_at = None
        if self.engine is None:
            self.bootstrap_error = 'database_unconfigured'
            return False
        try:
            self.initialize()
        except Exception:
            self.bootstrap_error = 'database_initialize_failed'
            self.retry_at = time.monotonic() + max(0, int(self.settings.database_init_retry_seconds))
            logging.getLogger(__name__).warning('application_database_initialize_failed')
            return False
        self.bootstrap_error = None
        return True

    def ensure_ready(self):
        """시작 시 초기화가 실패했으면 제한 간격으로만 다시 시도한다. 처음부터 지연 초기화하지 않는다.

        정돈-02: HTTP 조회(readyz 포함)는 이 함수를 부르지 않는다. lifespan의 재시도 스레드와 명시 명령만 쓴다."""
        if self.ready():
            return True
        if self.retry_at is not None and time.monotonic() >= self.retry_at:
            self.bootstrap()
            return self.ready()
        return False

    def retry_delay(self):
        """다음 재시도까지 남은 초. 재시도 예약이 없으면 None(초기화 성공·미구성)."""
        if self.retry_at is None:
            return None
        return max(0.0, self.retry_at - time.monotonic())

    def ready(self):
        """읽기 전용 확인: 표가 있고 공용 mutex 행이 있으면 준비됨. 스키마를 만들거나 행을 쓰지 않는다."""
        if self.factory is None or self.bootstrap_error is not None:
            return False
        try:
            with self.factory() as session:
                session.execute(select(Incident.id).limit(1))
                ok = session.get(Mutex, 1) is not None
                session.rollback()
                return ok
        except Exception:
            return False

    @contextmanager
    def read(self):
        """정돈-02: 조회 전용 세션. 공용 쓰기 잠금(BEGIN IMMEDIATE/FOR UPDATE)을 잡지 않고 끝에 항상 rollback한다.

        진행 중인 실행기 보고·승인 쓰기를 조회가 막지 않는다. 같은 사건의 문서는 한 행이므로 한 번의 SELECT가
        커밋된 한 시점을 돌려준다."""
        if self.factory is None:
            raise RuntimeError('독립 저장소가 구성되지 않았다')
        with self.factory() as session:
            try:
                yield session
            finally:
                session.rollback()

    @contextmanager
    def transaction(self):
        if self.factory is None:
            raise RuntimeError('독립 저장소가 구성되지 않았다')
        with self.factory() as session:
            try:
                if self.engine.dialect.name == 'sqlite':
                    session.execute(text('BEGIN IMMEDIATE'))
                else:
                    session.execute(select(Mutex).where(Mutex.id == 1).with_for_update()).scalar_one()
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise


class Mutex(ApplicationBase):
    __tablename__ = 'application_mutex'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Job(ApplicationBase):
    __tablename__ = 'application_jobs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    created_at: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class RunnerContact(ApplicationBase):
    """One bounded record per accepted job attempt. No token or worker identity enters display storage."""
    __tablename__ = 'application_runner_contacts'
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    claimed_at: Mapped[float | None] = mapped_column(Float)
    last_heartbeat_at: Mapped[float | None] = mapped_column(Float)
    last_contact_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)


class RunnerObservation(ApplicationBase):
    __tablename__ = 'application_runner_observation'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_claim_at: Mapped[float] = mapped_column(Float)


class Event(ApplicationBase):
    __tablename__ = 'application_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSON)


class SourceInquiry(ApplicationBase):
    """Read-only source investigation for a core incident; no Application release is created."""
    __tablename__='application_source_inquiries'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    incident_id: Mapped[int]=mapped_column(index=True)
    created_at: Mapped[float]=mapped_column(Float)
    expires_at: Mapped[float]=mapped_column(Float)
    payload: Mapped[dict]=mapped_column(JSON)
    state: Mapped[str]=mapped_column(String(16),index=True)
    worker: Mapped[str|None]=mapped_column(String(160))
    token_hash: Mapped[str|None]=mapped_column(String(64))
    fence: Mapped[int]=mapped_column(default=0)
    lease_until: Mapped[float|None]=mapped_column(Float)
    result: Mapped[dict|None]=mapped_column(JSON)
    receipt_sha: Mapped[str|None]=mapped_column(String(64))
    receipt: Mapped[dict|None]=mapped_column(JSON)


class DisplayReading(ApplicationBase):
    """One sanitized public passage per incident/version. Independent of deployment leases."""
    __tablename__ = "application_display_readings"
    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    passage: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[float] = mapped_column(Float)
    available_at: Mapped[float] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease: Mapped[str | None] = mapped_column(String(64))
    ko: Mapped[str | None] = mapped_column(Text)
