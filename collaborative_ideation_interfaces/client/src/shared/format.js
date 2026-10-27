export function formatKilobytes(bytes) {
  return `${(bytes / 1024).toFixed(0)} KB`
}

export function formatMegabytes(bytes) {
  return `${bytes / 1024 ** 2} MB`
}
