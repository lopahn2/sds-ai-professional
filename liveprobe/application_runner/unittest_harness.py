"""컨테이너 내부에서만 사용하는 unittest 실행 증거 수집기."""
import inspect
import json
import os
from pathlib import Path
import sys
import unittest


def main():
    # 같은 Python 프로세스의 임의 악성 코드를 완전히 격리한다는 주장이 아니다.
    # 등록 시험 외 프레임워크 교체와 보고서 직접 쓰기를 감지하여 증거를 폐기한다.
    root=Path('/workspace').resolve()
    receipt=Path('/reports/unittest.json').resolve()
    phase=os.environ['APPLICATION_TEST_PHASE'];mode=os.environ.get('APPLICATION_TEST_MODE')
    sys.path[:0]=[str(root),str(root/'src'),str(root/'tests')]
    rows=[];violations=[];writing=[False]
    path_type=Path;getfile=inspect.getfile;encode=json.dumps
    open_fd=os.open;write_fd=os.write;close_fd=os.close
    realpath=os.path.realpath;fspath=os.fspath
    report_root=str(receipt.parent)

    def audit(event,args):
        paths=[]
        if event=='open':
            path,mode_arg,flags=args
            if (isinstance(mode_arg,str) and any(c in mode_arg for c in 'wax+')) or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):
                paths=[path]
        elif event in ('os.remove','os.rmdir','os.mkdir','os.rename','os.link','os.symlink'):
            paths=args[:2] if event in ('os.rename','os.link','os.symlink') else args[:1]
        for path in paths:
            if isinstance(path,(str,bytes,os.PathLike)):
                resolved=realpath(fspath(path))
                if isinstance(resolved,bytes):resolved=os.fsdecode(resolved)
                if (resolved==report_root or resolved.startswith(report_root+os.sep)) and not writing[0]:
                    violations.append('receipt_direct_write')
                    raise PermissionError('receipt_direct_write')
    sys.addaudithook(audit)

    base=unittest.TextTestResult
    calls={name:getattr(base,name) for name in ('startTest','addSuccess','addFailure','addError','addSkip','addSubTest','addUnexpectedSuccess')}
    class Result(base):
        def startTest(self,test):
            calls['startTest'](self,test)
            try:file=path_type(getfile(type(test))).resolve().relative_to(root).as_posix()
            except (TypeError,ValueError):file='unresolved'
            rows.append({'id':file+'::'+type(test).__name__+'::'+test._testMethodName,'file':file,'status':'started','phase':phase})
        def addSuccess(self,test):
            calls['addSuccess'](self,test);rows[-1]['status']='passed'
        def addFailure(self,test,err):
            calls['addFailure'](self,test,err);rows[-1]['status']='failed'
        def addError(self,test,err):
            calls['addError'](self,test,err);rows[-1]['status']='error'
        def addSkip(self,test,reason):
            calls['addSkip'](self,test,reason);rows[-1]['status']='skipped'
        def addSubTest(self,test,subtest,err):
            calls['addSubTest'](self,test,subtest,err)
            if err is not None:rows[-1]['status']='failed'
        def addUnexpectedSuccess(self,test):
            calls['addUnexpectedSuccess'](self,test);rows[-1]['status']='error'

    # discovery 전에 인스턴스와 핵심 호출을 잡고 모듈·클래스·함수 코드의 정체성을 고정한다.
    output=open(os.devnull,'w')
    runner=unittest.TextTestRunner(stream=output,resultclass=Result)
    run=runner.run;discover=unittest.TestLoader().discover
    subjects=[unittest,unittest.runner,unittest.result,unittest.suite,unittest.case,unittest.loader,Result]
    subjects.extend({value for module in subjects[:6] for value in vars(module).values()
                     if isinstance(value,type) and value.__module__.startswith('unittest')})
    snapshots=[(subject,dict(vars(subject))) for subject in subjects]
    functions=[(value,value.__code__) for _,attrs in snapshots for value in attrs.values() if inspect.isfunction(value)]
    case=unittest.TestCase;suite_type=unittest.TestSuite
    hooks={'setUp','tearDown','setUpClass','tearDownClass'}
    case_calls={name:getattr(case,name) for name,value in vars(case).items()
                if inspect.isfunction(value) and name not in hooks}
    def check():
        if violations:raise ValueError('harness_integrity_rejected')
        for subject,attrs in snapshots:
            current=vars(subject)
            if current.keys()!=attrs.keys() or any(current[k] is not v for k,v in attrs.items()):
                raise ValueError('harness_integrity_rejected')
        if any(fn.__code__ is not code for fn,code in functions):raise ValueError('harness_integrity_rejected')
    def validate(suite):
        if type(suite) is not suite_type:raise ValueError('unregistered_test_framework')
        for test in suite:
            if type(test) is suite_type:validate(test)
            elif not isinstance(test,case) or any(getattr(type(test),name) is not fn for name,fn in case_calls.items()):
                raise ValueError('unregistered_test_framework')
    try:
        suite=discover(str(root/'tests'),pattern='test_*.py')
        check();validate(suite)
        result=run(suite)
        check()
        if result.testsRun!=len(rows) or any(row['status'] not in ('passed','failed','error','skipped') for row in rows):
            raise ValueError('harness_integrity_rejected')
        report={'total':result.testsRun,'failed':len(result.failures),'errors':len(result.errors),
                'executed_tests':rows,'mode':mode,'execution_proof':'trusted_harness_observed',
                'semantic_adequacy':'not_assessed'}
        data=encode(report).encode()
        writing[0]=True
        fd=open_fd(str(receipt),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:write_fd(fd,data)
        finally:close_fd(fd);writing[0]=False
        return 0 if result.wasSuccessful() else 1
    except Exception:
        print('harness_integrity_or_execution_rejected',file=sys.stderr)
        return 2
    finally:output.close()


if __name__=='__main__':
    sys.exit(main())
