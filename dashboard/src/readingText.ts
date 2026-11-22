// 단계 카드의 한글 정리 표시 규칙. 해석 상태를 본문 문장처럼 보이지 않는다.
// - 표시 가능한 한국어만 본문에 둔다.
// - 정리 중인 값은 '정리 중', 새 해석 도입 이전 기록은 '과거 데이터 없음'으로 표시한다.
// - 새 데이터의 해석 실패·필터 제외는 생략한다(실패라고 쓰지 않는다).
import { summaryKo } from './presentation.ts';

export const PENDING_KO = '정리 중';
export const LEGACY_KO = '과거 데이터 없음';
export type ReadingEra = 'legacy' | 'current' | null | undefined;
export type ReadingView = { text: string } | { note: string } | null;
export type ReadingOptions = { state?: string | null; era?: ReadingEra; preparing?: boolean };

// Sentence-opening connectives that refer back to the previous sentence.
const CONNECTIVE = /^(?:따라서|그러므로|그래서|그러나|하지만|다만|또한|그리고|게다가|아울러|한편|결국|즉|이에 따라|이에|이로 인해|이 때문에|그 결과|그런데|반면)\s*,?\s+/u;

/** Stored model conclusions may append a quoted log or diff to a readable explanation. Keep the readable head. */
export function salvage(value: unknown, max = 1800): string | null {
  if (typeof value !== 'string') return null;
  const head = value.split(/(?:\s근거\s*[:：]\s*(?:source:|[\x22`{])|\n(?:Traceback|at |\s*[{]|diff --git|--- a\/))/u, 1)[0];
  const safe = summaryKo(head, '', max);
  if (safe) return safe;
  const sentences = head.split(/(?<=[.!?])\s+|\n+/);
  const beginning: string[] = [];
  for (const sentence of sentences.slice(0, 3)) {
    const text = summaryKo(sentence, '', 700);
    if (!text) break;
    beginning.push(text);
  }
  if (beginning.length) return beginning.join(' ');
  // A sentence that names an exception class or quotes code stays hidden; the readable sentences around it remain.
  const readable = sentences.map(sentence => summaryKo(sentence, '', 700)).filter(Boolean).slice(0, 2);
  if (!readable.length) return null;
  // The first kept sentence followed the hidden one, so a connective that pointed back at it ('따라서 …') is dropped.
  readable[0] = readable[0].replace(CONNECTIVE, '');
  return readable.join(' ');
}

/** The one display rule for a model-written passage.
 *  state: the stored reading state (ready, pending, running, failed, missing, unobserved, legacy).
 *  era: 'legacy' when the record predates the summary pipeline; absent era and state means an older server (legacy). */
export function readingOf(value: unknown, { state, era, preparing }: ReadingOptions = {}): ReadingView {
  const text = salvage(value);
  if (text) return { text };
  if (state === 'pending' || state === 'running' || preparing) return { note: PENDING_KO };
  if (state === 'legacy' || era === 'legacy' || (state == null && era == null)) return { note: LEGACY_KO };
  return null;
}

/** Plain-text form for places that need one string; null when the passage is omitted. */
export const readingString = (view: ReadingView) => view == null ? null : 'text' in view ? view.text : view.note;

/** A state label for a passage whose text is not shown. Failures and gaps on current data are omitted (null). */
export function readingNote(state: unknown, era?: ReadingEra): string | null {
  if (state === 'pending' || state === 'running') return PENDING_KO;
  if (state === 'legacy' || era === 'legacy' || state == null) return LEGACY_KO;
  return null;
}
