import { useEffect, useRef, useState } from 'react'
import { chatApi } from '../api/chatApi.js'
import { formatMegabytes } from '../shared/format.js'
import { prepareAttachment } from './attachmentUpload.js'

function isAcceptedFile(file, config) {
  const extension = `.${file.name.split('.').pop().toLowerCase()}`
  return file.size > 0 && file.size <= config.maxFileBytes && config.extensions.includes(extension)
}

export function useAttachmentDraft() {
  const [items, setItems] = useState([])
  const [fileConfig, setFileConfig] = useState(null)
  const [fileError, setFileError] = useState('')
  const previewsRef = useRef(new Set())

  useEffect(() => {
    let active = true
    chatApi.attachmentConfig()
      .then((config) => {
        if (active) setFileConfig(config)
      })
      .catch(() => {
        if (active) setFileError('파일 첨부 설정을 불러오지 못했습니다. 새로고침해 주세요.')
      })
    const previews = previewsRef.current
    return () => {
      active = false
      previews.forEach((url) => URL.revokeObjectURL(url))
    }
  }, [])

  function patchItem(key, changes) {
    setItems((current) => current.map((item) => item.key === key ? { ...item, ...changes } : item))
  }

  function chooseFiles(files) {
    if (files.length + items.length > fileConfig.maxFiles) {
      setFileError(`한 번에 파일 ${fileConfig.maxFiles}개까지 첨부할 수 있습니다.`)
      return
    }
    if (!files.every((file) => isAcceptedFile(file, fileConfig))) {
      setFileError(`지원 형식과 파일 크기를 확인해 주세요. 파일당 최대 ${formatMegabytes(fileConfig.maxFileBytes)}입니다.`)
      return
    }
    setFileError('')
    setItems((current) => [...current, ...files.map((file) => {
      const preview = /\.(png|jpe?g)$/i.test(file.name) ? URL.createObjectURL(file) : null
      if (preview) previewsRef.current.add(preview)
      return { key: crypto.randomUUID(), file, preview }
    })])
  }

  function releasePreview(item) {
    if (!item.preview) return
    URL.revokeObjectURL(item.preview)
    previewsRef.current.delete(item.preview)
  }

  function removeItem(item) {
    setItems((current) => current.filter((entry) => entry.key !== item.key))
    releasePreview(item)
    if (item.attachment) void chatApi.discardAttachment(item.attachment.id).catch(() => {})
    setFileError('')
  }

  async function prepareAttachments() {
    const attachments = []
    for (const item of items) {
      try {
        attachments.push(await prepareAttachment(item, (changes) => patchItem(item.key, changes)))
      } catch (error) {
        patchItem(item.key, { progress: '준비 실패 · 다시 전송하거나 제거해 주세요' })
        throw error
      }
    }
    return attachments
  }

  function clearAttachments() {
    setItems([])
    items.forEach(releasePreview)
  }

  return {
    items, fileConfig, fileError, setFileError,
    chooseFiles, removeItem, prepareAttachments, clearAttachments,
  }
}
