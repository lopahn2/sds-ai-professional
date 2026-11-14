#!/usr/bin/env python3
"""Application 전용 runner CLI. 기존 runner를 실행/수정하지 않는다."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from liveprobe.application_runner.cli import main

if __name__=='__main__':
    sys.exit(main())
