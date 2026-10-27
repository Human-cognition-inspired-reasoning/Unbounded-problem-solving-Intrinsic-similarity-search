const THEME_KEY = 'co-scientist:theme:v1'

export function getInitialTheme() {
  const saved = window.localStorage.getItem(THEME_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function saveTheme(theme) {
  window.localStorage.setItem(THEME_KEY, theme)
}
