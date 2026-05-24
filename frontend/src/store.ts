import { create } from 'zustand'

// Placeholder store — full implementation in Phase 9
interface Store {
  _placeholder: null
}

export const useStore = create<Store>(() => ({
  _placeholder: null,
}))
