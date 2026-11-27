import type { Overview } from './api';
import { problemsOf } from './mapModel';
import { bytesKo, connectionsOf, cpuKo, errorRateKo, numberKo, overviewMap, resourceHealth } from './overviewModel';
import { placeKo } from './labels';
import { Disclosure } from './Disclosure';

/** Collected fields of one resource, shown in the side area. Unobserved never reads as healthy. */
export function ResourceDetail({ overview: o, id, onClose }: { overview: Overview; id: string; onClose: () => void }) {
  const m = overviewMap(o, '', { showPlatform: true }); const d = m.nodes.find(n => n.id === id);
  if (!d) return null;
  const links = connectionsOf(m, id);
  return <aside className="resource-detail" aria-label="자원 상세"><div className="section-heading"><div><span className="eyebrow">{d.node.kind} · {d.node.namespace ?? '외부'}</span><h3>{d.node.name}</h3></div><button className="btn sm" onClick={onClose}>자원 상세 닫기</button></div>
    <p>{resourceHealth(d.node, d.workload)}{d.workload?.ready != null && d.workload?.desired != null ? ` · 준비 ${d.workload.ready}/${d.workload.desired}` : ''}</p>
    <p>{placeKo(d.node.environment, d.node.system, d.node.name, d.node.namespace)}</p>
    {d.node.kind === 'ContractEndpoint' && <div className="notice">승인된 연계 경로의 끝점이며 실제 Kubernetes 자원이 아닙니다. 해당 자원의 배포가 관측되기 전까지 준비됨·정상으로 표시하지 않습니다.</div>}
    {problemsOf(d.node).length > 0 && <div className="notice"><b>관측 경고</b><ul>{problemsOf(d.node).map((p, i) => <li key={i}>{p.text}</li>)}</ul><small>수집된 관측 창의 값이며 자원 준비 상태와 별개입니다.</small></div>}
    <dl className="kv"><dt>요청</dt><dd>{numberKo(d.workload?.req_rate, 1)}{d.workload?.req_rate != null ? '/초' : ''}</dd><dt>오류율</dt><dd>{errorRateKo(d.workload?.err_rate)}</dd><dt>상위 95% 지연</dt><dd>{numberKo(d.workload?.p95_ms, 0)}{d.workload?.p95_ms != null ? 'ms' : ''}</dd><dt>CPU · 메모리</dt><dd>{cpuKo(d.workload?.cpu)} · {bytesKo(d.workload?.mem)}</dd><dt>재시작 · 1시간</dt><dd>{numberKo(d.workload?.restarts_1h)}</dd></dl>
    {(m.foldedIngresses.get(id) ?? []).map(ingressId => {
      const ingress = m.nodes.find(n => n.id === ingressId)?.node;
      return ingress && <Disclosure key={ingressId} label={`Ingress · ${ingress.name} 원문 보기`}>{() => <pre className="raw-text">{JSON.stringify(ingress, null, 2)}</pre>}</Disclosure>;
    })}
    {(d.node.services ?? []).map(s => <div className="service-detail" key={s.name}><b>Service · {s.name}</b><span>포트 {(s.ports ?? []).join(', ') || '미관측'}</span><span>준비된 엔드포인트 {numberKo(s.endpoints_ready)}</span></div>)}
    {links.length > 0 && <div className="service-detail"><b>연결 · {links.length}개</b>{links.map(l => <span key={l.id}>{l.direction === 'out' ? '→' : '←'} {l.peer} · {l.text}{l.state === 'blocked' ? ' · 차단' : l.state === 'degraded' ? ' · 저하' : ''}</span>)}</div>}
    <Disclosure label="수집 필드 원문 보기">{() => <pre className="raw-text">{JSON.stringify({ resource: d.node, workload: d.workload ?? null }, null, 2)}</pre>}</Disclosure><p className="muted">미관측은 정상을 뜻하지 않습니다.</p>
  </aside>;
}
