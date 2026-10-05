import { useSyncExternalStore } from 'react';

// The local human control token (execution approvals, service connections). Held in
// memory only and shared by the panels that need it; agents never receive it.
let token = '';
const listeners = new Set<() => void>();

export function setControlToken(value: string) {
  if (value === token) return;
  token = value;
  listeners.forEach(listener => listener());
}

export function useControlToken() {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => token);
}
