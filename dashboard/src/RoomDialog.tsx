import { useEffect, useId, useRef, type ReactNode } from 'react';

/** Native modality keeps keyboard focus inside the expanded work area and
 * restores it to the invoking control when the operator returns. */
export function RoomDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const heading = useId();
  useEffect(() => {
    const dialog = ref.current!;
    const opener = document.activeElement;
    dialog.showModal();
    return () => {
      dialog.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);
  return <dialog ref={ref} className="room-wide" aria-labelledby={heading} onCancel={e => { e.preventDefault(); onClose(); }}>
    <header><h2 id={heading}>{title}</h2><button className="btn sm" type="button" autoFocus onClick={onClose}>닫기</button></header>
    <div className="room-wide-body">{children}</div>
  </dialog>;
}
