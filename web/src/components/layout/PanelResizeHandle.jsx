import React, { useCallback } from 'react';
import { cn } from '@/lib/utils';

/**
 * Вертикальная полоса между двумя flex-колонками: перетаскивание мышью влево/вправо
 * меняет ширину соседних панелей (курсор col-resize).
 *
 * @param {(deltaX: number) => void} onResize — приращение по X с прошлого события движения.
 */
export default function PanelResizeHandle({ onResize, className, disabled }) {
  const onPointerDown = useCallback(
    (e) => {
      if (disabled || e.button !== 0) return;
      e.preventDefault();
      let lastX = e.clientX;
      const onMove = (ev) => {
        const dx = ev.clientX - lastX;
        lastX = ev.clientX;
        if (dx !== 0) onResize(dx);
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('pointercancel', onUp);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onUp);
    },
    [disabled, onResize],
  );

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Изменить ширину панели"
      onPointerDown={onPointerDown}
      className={cn(
        'shrink-0 w-1.5 cursor-col-resize select-none z-10',
        'hover:bg-primary/25 active:bg-primary/35 transition-[background-color] duration-150',
        disabled && 'pointer-events-none opacity-0 w-0',
        className,
      )}
      style={{ touchAction: 'none' }}
    />
  );
}
