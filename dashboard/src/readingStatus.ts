const descriptions:Record<string,string> = {
  pending:'한글 해석 대기', missing:'한글 해석 미기록', running:'한글 해석 중',
  failed:'한글 해석 실패', unobserved:'한글 해석 미관측',
};

export function readingStatus(state:unknown, fallback='한글 해석 미관측') {
  return typeof state==='string' ? descriptions[state] || fallback : fallback;
}
