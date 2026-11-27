"""Observe the product's own dev JDBC detection, test, and rollout receipts."""

from argparse import ArgumentParser
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
from subprocess import TimeoutExpired
from time import monotonic, sleep, time
from urllib.error import URLError

from evaluation import reservation_dev_runtime as runtime
from evaluation.isolated_recovery_agent import Unobserved
from evaluation.jdbc_dev_manifest import IMAGE, NAME
from evaluation.jdbc_product_source import source_proof
from evaluation.matched_jdbc_leak import sealed_variant, verify_healthy
from evaluation.matched_recovery_campaign import RECOVERY_BUDGET_S, dev_preflight, write_new
from evaluation.reservation_runtime_image import inspect, package
from evaluation.single_baseline import MODELS, estimate


cluster = runtime.cluster
TERMINAL = frozenset(('completed', 'failed', 'handoff'))
PRICE_KEYS = {value[0]: name for name, value in MODELS.items()}


def product_rows(after):
    sql = f"""
    BEGIN READ ONLY;
    SELECT coalesce(json_agg(row_to_json(q)), '[]'::json) FROM (
      SELECT i.id,i.state,
        i.document#>>'{{data,signal,source}}'='pod_log' AND
          coalesce(i.document#>>'{{data,signal,sample}}','') ~* 'hikari|connection is not available|cannotgetjdbcconnectionexception' AS native_signal,
        i.document#>>'{{data,collection,target,image}}' AS fault_image,
        i.document#>>'{{data,release,artifact,image}}' AS candidate,
        i.document#>'{{data,release,tests}}' AS regression,
        i.document#>'{{data,release,stage_results,dev,verified}}' AS dev_verified,
        i.document#>>'{{error,code}}' AS error_code,
        EXISTS (SELECT 1 FROM application_jobs j WHERE j.incident_id=i.id AND j.kind IN ('deploy','reconcile')
                  AND j.status IN ('queued','leased','reconciliation_required')) AS pending_write,
        (SELECT json_agg(json_build_object('role',r->>'role','model',r->>'model','usage',r->'usage'))
           FROM json_array_elements(coalesce(i.document#>'{{data,analysis,roles}}','[]'::json)) r) AS model_calls
        FROM application_incidents i WHERE i.created_at >= {float(after):.6f}
          AND i.document->>'environment'='dev' AND i.document->>'service'='{NAME}' ORDER BY i.created_at
    ) q;
    ROLLBACK;
    """
    return json.loads(cluster.command('exec', '-i', 'statefulset/liveprobe-application-db', '--', 'sh', '-ec',
        'exec psql -X -qAt -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"',
        namespace='liveprobe-system', stdin=sql))


def read_product_calls(row):
    calls = []
    for item in row['model_calls'] or []:
        role, model, usage = item['role'], item['model'], item['usage'] or {}
        price = PRICE_KEYS.get(model)
        calls.append({'role': role, 'model': model, 'input_tokens': usage.get('input_tokens'),
                      'output_tokens': usage.get('output_tokens'), 'estimated_usd': estimate(price, usage) if price else None})
    return {'id': row['id'], 'status': row['state'], 'error_code': row['error_code'], 'calls': calls}


def observe(result, baseline, broken, after, began):
    deadline = began+RECOVERY_BUDGET_S
    while monotonic() < deadline:
        rows = product_rows(after)
        matched = [row for row in rows if row['native_signal'] and row['fault_image'] in (None, broken['image'])]
        if len(matched) > 1:
            raise Unobserved('multiple product incidents observed the same reservation fault')
        if matched:
            row, = matched
            result.setdefault('detected_ms', round((monotonic()-began)*1000))
            result['incident'] = read_product_calls(row)
            proof = row['regression'] or {}
            candidate = row['candidate'] or ''
            tested_candidate = (row['fault_image'] == broken['image'] and IMAGE.fullmatch(candidate) is not None
                                and candidate != broken['image'] and all(proof.get(key) is True
                                for key in ('before_failed', 'after_passed', 'regression_executed')))
            if (tested_candidate and row['state'] == 'completed' and not row['pending_write']
                    and row['dev_verified'] is True):
                artifact = {'image': candidate, 'runtime_image_id': inspect(candidate)['Id']}
                verified = verify_healthy(baseline, artifact, min(deadline, monotonic()+40))
                result.update(status='recovered', recovery_ms=round((monotonic()-began)*1000),
                              verified_before_cleanup=verified, regression={'reproduced': True, 'passed': True, 'receipt': proof})
                return
            if row['state'] in TERMINAL and not row['pending_write']:
                current = runtime.deployment()
                if runtime.image_of(current) == broken['image']:
                    result.update(status='unrecovered', failed_before_cleanup=runtime.failure_persists(baseline, broken, monotonic()+12))
                    return
                if tested_candidate and row['state'] == 'failed':
                    crash = runtime.candidate_crash(baseline, current, candidate)
                    if crash:
                        result.update(status='unrecovered', failed_before_cleanup=crash,
                                      regression={'reproduced': True, 'passed': True, 'receipt': proof})
                        return
        sleep(4)
    raise Unobserved('native Application detection or a verified dev release did not finish within the budget')


def trial(args, healthy, broken, allocation, journal):
    gate = dev_preflight()
    prefix = '/api/application/v1'
    catalog = cluster.product_status(prefix+'/catalog')
    if (not any(row.get('service') == NAME for row in catalog.get('services', []))
            or not cluster.product_status(prefix+'/readyz').get('ready')):
        raise Unobserved('the native Application catalog and external workers are not ready')
    if any(row['state'] not in TERMINAL or row['pending_write'] for row in product_rows(0)):
        raise Unobserved('an older Application incident still has work for the reservation fixture')
    baseline = runtime.deployment()
    healthy_before = verify_healthy(baseline, healthy, monotonic()+70)
    uid = baseline['metadata']['uid']
    result = {'case': runtime.CASE.case_id, 'arm': 'liveprobe', 'headroom': gate, 'status': 'unobserved',
              'healthy_before': healthy_before, 'deployment_uid': uid, 'allocation_sha256': allocation,
              'variant_sha256': broken['jar_sha256'], 'neutral_release_sha256': healthy['jar_sha256']}
    write_new(journal, {'target': runtime.CASE.target, 'uid': uid, 'original': healthy['image'],
                        'faulty': broken['image'], 'receipt': str(args.output)})
    after = time()
    clean = False
    try:
        runtime.switch(baseline, healthy['image'], broken['image'])
        began = monotonic()
        result['injected_at'] = datetime.now(timezone.utc).isoformat()
        fault = runtime.read_workload(baseline, broken, began+75)
        if fault['status'] != 'leak_reproduced':
            raise Unobserved('the product variant did not reproduce the independent JDBC leak')
        result.update(fault=fault, first_error=runtime.first_error(fault['runtime']['pod']),
                      first_error_ms=round((monotonic()-began)*1000))
        observe(result, baseline, broken, after, began)
        result['trial_elapsed_ms'] = round((monotonic()-began)*1000)
    except (Unobserved, cluster.Refused, ValueError, OSError, TimeoutExpired) as error:
        result.update(status='unobserved', trial_error=str(error))
    finally:
        try:
            rows = product_rows(after)
            settle_by = monotonic()+90
            while any(row['pending_write'] for row in rows):
                if monotonic() >= settle_by:
                    raise Unobserved('an Application deploy or reconciliation is still in progress')
                sleep(4)
                rows = product_rows(after)
            current = runtime.deployment()
            image = runtime.image_of(current)
            expected = {healthy['image'], broken['image']} | {
                row['candidate'] for row in rows if row['native_signal'] and row['fault_image'] == broken['image']}
            if not runtime.same_resource(current, baseline) or image not in expected:
                raise Unobserved('the trial Deployment reached an unattributed image or resource')
            if image != healthy['image']:
                runtime.switch(baseline, image, healthy['image'])
            result['restored'] = verify_healthy(baseline, healthy, monotonic()+85)
            clean = True
        finally:
            if not clean:
                result.update(status='unobserved', cleanup_incomplete=True)
            write_new(args.output, result)
            if clean:
                journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    for name in ('healthy-receipt', 'compiled', 'assignments', 'commitment', 'provenance', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    args = parser.parse_args()
    healthy = json.loads(args.healthy_receipt.read_bytes())['artifact']
    compiled = json.loads(args.compiled.read_bytes())
    allocation = json.loads(args.commitment.read_bytes())['sha256']
    variant = sealed_variant('liveprobe', args.assignments, allocation)
    journal = cluster.EVIDENCE/'dev-k8s-fault-active.json'
    with (cluster.EVIDENCE/'dev-k8s-fault.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists() or not args.provenance.is_dir():
            parser.error('reconcile the previous trial or prepare the private provenance directory')
        broken = package(Path(compiled['root'])/variant/'target/reservation-1.0.0.jar')
        proof, _ = source_proof(compiled, variant, broken)
        destination = args.provenance/(NAME+'-'+broken['image'].rsplit(':',1)[1]+'.json')
        if destination.is_file():
            if json.loads(destination.read_bytes()) != proof:
                parser.error('existing provenance belongs to a different release')
        else:
            write_new(destination, proof)
        result = trial(args, healthy, broken, allocation, journal)
    print(json.dumps({'status': result['status']}))
    return 2 if result['status'] == 'unobserved' else 0


if __name__ == '__main__':
    raise SystemExit(main())
