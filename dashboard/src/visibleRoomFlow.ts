import type { Narrative } from './api.ts';
import { stepNeighborhood } from './roomWorkflow.ts';

/** Keep the operator's selection and any independently running work visible together. */
export function visibleRoomFlow(flow: Narrative['flow'], selection: string | undefined, showAll: boolean, live: boolean) {
  if (showAll || !selection) return flow;
  const local = stepNeighborhood(flow, selection);
  if (!live) return local;
  const ids = new Set(local.nodes.map(node => node.id));
  for (const node of flow.nodes) if (node.status === 'running') ids.add(node.id);
  return {
    nodes: flow.nodes.filter(node => ids.has(node.id)),
    edges: flow.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)),
  };
}
