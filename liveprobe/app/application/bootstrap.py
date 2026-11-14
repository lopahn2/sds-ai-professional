"""독립 앱 스키마 초기화: python -B -m app.application.bootstrap.

기존 LiveProbe Base·DB·alembic을 불러오지 않는다. 접속 설정과 예외 원문은 출력하지 않는다.
"""
import argparse
import json
from .settings import ApplicationSettings
from .storage import Store


def main(argv=None, *, settings=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    store = Store(settings or ApplicationSettings.from_env())
    try:
        ok = store.bootstrap()
        print(json.dumps({'ok': ok, 'code': 'database_initialized' if ok else store.bootstrap_error}))
        return 0 if ok else 1
    finally:
        if store.engine is not None:
            store.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
