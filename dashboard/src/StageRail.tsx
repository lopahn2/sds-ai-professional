import { useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { ChevronLeftIcon, ChevronRightIcon } from '@radix-ui/react-icons';
import type { FlowNode, Narrative } from './api';
import { DiagramIcon } from './DiagramIcon';
import { flowStatusKo, flowTitle } from './narrative';
import { isSemanticNode } from './semanticFlow';
import { useStageTravel } from './StageMotion';
import './stageRail.css';

type RailProps = {
  flow: Narrative['flow'];
  selected: string | null;
  onSelect: (id: string) => void;
  snapshot?: boolean;
  archived?: boolean;
  previousCore?: boolean;
};

function cardButtons(view: HTMLElement) {
  return Array.from(view.querySelectorAll<HTMLButtonElement>('button[data-stage-id]'));
}

function centerPosition(view: HTMLElement, card: HTMLElement) {
  const rect = card.getBoundingClientRect();
  const left = view.scrollLeft + rect.left - view.getBoundingClientRect().left + (rect.width - view.clientWidth) / 2;
  return Math.max(0, Math.min(left, view.scrollWidth - view.clientWidth));
}

function browseTo(view: HTMLElement, left: number, animate = true) {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  view.scrollTo({ left, behavior: animate && !reduce ? 'smooth' : 'instant' });
}

function scrollOne(view: HTMLElement, forward: boolean) {
  const positions = cardButtons(view).map(card => centerPosition(view, card));
  const next = forward ? positions.find(left => left > view.scrollLeft + 2)
    : positions.reverse().find(left => left < view.scrollLeft - 2);
  if (next !== undefined) browseTo(view, next);
}

function browseKeyboard(event: KeyboardEvent<HTMLButtonElement>) {
  const steps = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
  if (!steps.includes(event.key) || event.altKey || event.metaKey || event.ctrlKey) return;
  const view = event.currentTarget.closest<HTMLElement>('.stage-rail-viewport');
  if (!view) return;
  const cards = cardButtons(view);
  const index = cards.indexOf(event.currentTarget);
  const next = { ArrowLeft: Math.max(0, index - 1), ArrowRight: Math.min(cards.length - 1, index + 1),
    Home: 0, End: cards.length - 1 }[event.key];
  if (next === undefined) return;
  event.preventDefault();
  cards[next].focus({ preventScroll: true });
  browseTo(view, centerPosition(view, cards[next]));
}

function storedStatus(node: FlowNode, previousCore: boolean) {
  if (!previousCore || node.type === 'code_action' || 'app' in node) return false;
  return !isSemanticNode(node) || !node.semantic.members.some(member => 'app' in member || member.type === 'code_action');
}

export function StageRail({ flow, selected, onSelect, snapshot = false, archived = false, previousCore = false }: RailProps) {
  const viewport = useRef<HTMLDivElement>(null);
  const previous = useRef<string | null>(null);
  const [edges, setEdges] = useState({ before: false, after: false });
  const travel = useStageTravel();
  const count = flow.nodes.length;

  useLayoutEffect(() => {
    const view = viewport.current;
    if (!view) return;
    function updateEdges() {
      if (!view || !view.clientWidth) return;
      const before = view.scrollLeft > 1;
      const after = view.scrollWidth - view.clientWidth - view.scrollLeft > 1;
      setEdges(old => old.before === before && old.after === after ? old : { before, after });
    }
    // Keep native horizontal trackpad and touch momentum. A vertical mouse wheel
    // pans this strip only while it can travel in the requested direction.
    let wheelTimer: number | undefined;
    function wheel(event: WheelEvent) {
      if (!view || !event.cancelable || event.ctrlKey || event.shiftKey || event.deltaX || !event.deltaY) return;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? view.clientWidth : 1;
      const amount = event.deltaY * unit;
      const max = view.scrollWidth - view.clientWidth;
      if (amount > 0 ? view.scrollLeft >= max - 1 : view.scrollLeft <= 1) return;
      event.preventDefault();
      // Tiny wheel deltas must accumulate before the browser snaps to a card.
      view.dataset.wheeling = 'true';
      view.scrollBy({ left: amount, behavior: 'instant' });
      window.clearTimeout(wheelTimer);
      wheelTimer = window.setTimeout(() => { delete view.dataset.wheeling; }, 150);
    }
    const resize = new ResizeObserver(updateEdges);
    resize.observe(view);
    view.addEventListener('scroll', updateEdges, { passive: true });
    view.addEventListener('wheel', wheel, { passive: false });
    updateEdges();
    return () => {
      resize.disconnect();
      view.removeEventListener('scroll', updateEdges);
      view.removeEventListener('wheel', wheel);
      window.clearTimeout(wheelTimer);
      delete view.dataset.wheeling;
    };
  }, [count]);

  // A polling refresh may replace every node. Follow only an actual change of
  // selection, so browsing another part of the rail is stable between updates.
  useLayoutEffect(() => {
    const view = viewport.current;
    if (!view || !selected) { previous.current = null; return; }
    const card = cardButtons(view).find(item => item.dataset.stageId === selected);
    if (!card) return;
    const animate = previous.current !== null && previous.current !== selected;
    previous.current = selected;
    if (view.clientWidth) { browseTo(view, centerPosition(view, card), animate); return; }
    // In the mobile detail view the rail is hidden. Align once on returning.
    const resize = new ResizeObserver(() => {
      if (!view.clientWidth) return;
      browseTo(view, centerPosition(view, card), false);
      resize.disconnect();
    });
    resize.observe(view);
    return () => resize.disconnect();
  }, [selected]);

  if (!count) return <div className="stage-rail-empty" role="status">아직 기록된 흐름이 없습니다.</div>;
  return <div className="stage-rail">
    <div className="stage-rail-window" data-before={edges.before || undefined} data-after={edges.after || undefined}>
      <div ref={viewport} className="stage-rail-viewport" role="group" aria-label="사건 단계 카드">
        <ol className="stage-rail-track">{flow.nodes.map(node => {
          const active = selected === node.id;
          const stored = snapshot || (node.status === 'running' && (archived || storedStatus(node, previousCore)));
          const status = (stored ? '저장 당시 ' : '') + flowStatusKo(node.status);
          const title = flowTitle(node);
          return <li key={node.id}><button type="button" className="stage-rail-card"
            aria-pressed={active} aria-label={title + ' · ' + status + ' · 단계 보기'}
            data-stage-id={node.id} data-status={stored ? 'recorded' : node.status}
            data-travel={active && travel?.to === node.id ? travel.direction : undefined}
            onKeyDown={browseKeyboard} onClick={event => {
              const view = viewport.current;
              if (active && view) browseTo(view, centerPosition(view, event.currentTarget));
              onSelect(node.id);
            }}>
            <span className="stage-rail-icon"><DiagramIcon kind={node.type === 'outcome' && title.includes('인계') ? 'steer' : node.type} /></span>
            <strong>{title}</strong><span className="stage-rail-status">{status}</span>
          </button></li>;
        })}</ol>
      </div>
    </div>
    {(edges.before || edges.after) && <div className="stage-rail-controls" role="group" aria-label="카드 둘러보기">
      <button type="button" aria-label="이전 카드 둘러보기" disabled={!edges.before}
        onClick={() => viewport.current && scrollOne(viewport.current, false)}><ChevronLeftIcon /></button>
      <button type="button" aria-label="다음 카드 둘러보기" disabled={!edges.after}
        onClick={() => viewport.current && scrollOne(viewport.current, true)}><ChevronRightIcon /></button>
    </div>}
  </div>;
}
