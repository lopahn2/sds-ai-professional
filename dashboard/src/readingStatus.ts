import { readingNote, type ReadingEra } from './readingText';

/** The label shown in place of a passage whose Korean summary is not displayed:
 *  '정리 중' while it is being summarised, '과거 데이터 없음' for records that predate the summary pipeline,
 *  and nothing ('') when a current summary failed or was filtered (a failure is never shown as text). */
export function readingStatus(state: unknown, era?: ReadingEra): string {
  return readingNote(state, era) ?? '';
}
