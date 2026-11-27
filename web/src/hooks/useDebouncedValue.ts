import { useEffect, useState } from 'react'

/**
 * 防抖值：源值停稳 `delayMs` 后才跟随。
 * 用在"自由文本输入 → 触发请求"的场景（如提交页 profile 输入 → 模型探针），
 * 逐键触发会打出一串请求。
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState<T>(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return debounced
}
